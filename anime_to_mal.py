#!/usr/bin/env python3
"""
anime_to_mal.py - turn your "Anime Watched" CSV into a MyAnimeList import XML.

Works for both MyAnimeList (myanimelist.net/import.php) and AniList
(Settings > Apps/Import > MyAnimeList XML).

Usage:
    python3 anime_to_mal.py "Anime Watched - Anime - Anime Watched.csv"

What it makes (next to your CSV):
    mal_import.xml    - the file you upload
    match_review.csv  - every row, what it matched, and how sure it is
                        (least-sure rows at the top)
    .anilist_cache.json - saved lookups, so re-runs are fast

Fixing a bad match:
    Open match_review.csv, put the right MAL ID in the "override" column
    (the number in a myanimelist.net/anime/<ID>/... link), or type "skip"
    to leave the row out. Save it as CSV, then run the script again.
    Your overrides are kept and the XML is rebuilt.

Only uses Python's standard library - nothing to install.
Titles are looked up through AniList's free public API (no account needed),
which also returns each show's MAL ID.
"""

import csv
import datetime
import difflib
import random
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

API = "https://graphql.anilist.co"
DELAY = 2.2
STARTED_WATCHING = "2024-01-01"  # shows that finished airing before this get a generated date  # seconds between API calls; AniList allows ~30-90 per minute

SEARCH_Q = """
query ($s: String) {
  Page(perPage: 10) {
    media(search: $s, type: ANIME) {
      id idMal format episodes seasonYear
      title { romaji english }
      synonyms
    }
  }
}"""

BY_MAL_Q = """
query ($id: Int) {
  Media(idMal: $id, type: ANIME) {
    id idMal format episodes seasonYear
    title { romaji english }
    synonyms
  }
}"""

END_Q = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) { id endDate { year month day } }
  }
}"""

FORMAT_TO_MAL = {
    "TV": "TV", "TV_SHORT": "TV", "MOVIE": "Movie", "SPECIAL": "Special",
    "OVA": "OVA", "ONA": "ONA", "MUSIC": "Music",
}

# Cyrillic letters that look Latin (one row in the sheet uses them)
LOOKALIKES = str.maketrans("МаоуАОЕеТСсРрКкНВХх", "MaoyAOEeTCcPpKkHBXx")


# ---------------------------------------------------------------- API + cache

class AniList:
    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.cache = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}
        self.last_call = 0.0

    def save(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)

    def _post(self, query, variables):
        body = json.dumps({"query": query, "variables": variables}).encode()
        for attempt in range(6):
            wait = DELAY - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()
            req = urllib.request.Request(
                API, data=body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": "anime-to-mal-script"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = int(e.headers.get("Retry-After", "60"))
                    print(f"   rate limited, waiting {retry}s...")
                    time.sleep(retry + 1)
                    continue
                if e.code == 404:
                    return {"data": {"Media": None}}
                print(f"   HTTP {e.code}, retrying...")
                time.sleep(5 * (attempt + 1))
            except Exception as e:
                print(f"   network error ({e}), retrying...")
                time.sleep(5 * (attempt + 1))
        raise SystemExit("Couldn't reach AniList after several tries. "
                         "Check your internet and run again (progress is cached).")

    def search(self, text):
        key = "s:" + text.lower()
        if key not in self.cache:
            data = self._post(SEARCH_Q, {"s": text})
            self.cache[key] = ((data.get("data") or {}).get("Page") or {}).get("media") or []
            self.save()
        return self.cache[key]

    def end_dates(self, ids):
        """Final-episode air date for each AniList id, as YYYY-MM-DD (00 for unknown parts)."""
        need = [i for i in ids if f"e:{i}" not in self.cache]
        for k in range(0, len(need), 50):
            chunk = need[k:k + 50]
            data = self._post(END_Q, {"ids": chunk})
            got = {m["id"]: m.get("endDate") or {} for m in
                   (((data.get("data") or {}).get("Page") or {}).get("media") or [])}
            for i in chunk:
                d = got.get(i) or {}
                self.cache[f"e:{i}"] = (f"{d['year']:04d}-{d.get('month') or 0:02d}-{d.get('day') or 0:02d}"
                                        if d.get("year") else "")
            self.save()
        return {i: self.cache.get(f"e:{i}", "") for i in ids}

    def by_mal(self, mal_id):
        key = f"m:{mal_id}"
        if key not in self.cache:
            data = self._post(BY_MAL_Q, {"id": int(mal_id)})
            self.cache[key] = (data.get("data") or {}).get("Media")
            self.save()
        return self.cache[key]


# ---------------------------------------------------------------- text helpers

def norm(s):
    s = (s or "").translate(LOOKALIKES)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'").replace("–", "-").replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "final": None}
ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5}


def season_of(text, trailing=True):
    """Season number written in a title, or None."""
    t = norm(text)
    m = re.search(r"\bseason (\d+)\b", t) or re.search(r"\b(\d+)(?:st|nd|rd|th) season\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(first|second|third|fourth|fifth) season\b", t)
    if m:
        return ORDINALS[m.group(1)]
    m = re.search(r"\b(ii|iii|iv)\b", t)
    if m:
        return ROMAN[m.group(1)]
    m = re.search(r"[a-z].* ([2-6])$", t) if trailing else None  # "overlord 3"
    if m and not re.search(r"\b(no|part|cour|vol) [2-6]$", t):
        return int(m.group(1))
    return None


def part_of(text):
    m = re.search(r"\bpart (\d+)\b", norm(text)) or re.search(r"\bcour (\d+)\b", norm(text))
    return int(m.group(1)) if m else None


def strip_markers(t):
    t = norm(t)
    t = re.sub(r"\b(season \d+|\d+(st|nd|rd|th) season|(first|second|third|fourth|fifth) season"
               r"|part \d+|cour \d+|ii|iii|iv)\b", " ", t)
    t = re.sub(r" \d$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) > 4 and len(b) > 4 and (a in b or b in a):
        r = max(r, 0.88)
    return r


# ---------------------------------------------------------------- reading the sheet

def parse_row(title, season_col):
    """Work out what kind of entry a sheet row is asking for."""
    raw_title = (title or "").strip()
    sc = (season_col or "").strip()
    scn = norm(sc)

    alt = None
    m = re.search(r"\(([^)]*)\)", raw_title)
    base = raw_title
    if m:
        inner = m.group(1)
        if season_of(inner):
            pass  # "(Season 2)" - season info, drop it
        elif norm(inner) not in ("original series", "special"):
            alt = inner.strip()  # "(Biaoren)" - another name for the show
        base = re.sub(r"\s*\([^)]*\)\s*", " ", raw_title).strip()
    base = base.rstrip(":. ").strip()

    kind = "tv"
    if "movie" in scn or "movie" in norm(raw_title) or "the movie" in norm(raw_title):
        kind = "movie"
    elif re.search(r"\b(ova|oad)\b", scn):
        kind = "ova"
    elif re.search(r"\b(special|specials|recap|prequel|extra)\b", scn):
        kind = "special"

    season = season_of(sc, trailing=False) if sc else None
    if season is None:
        season = season_of(raw_title, trailing=False)
    part = part_of(sc) or part_of(raw_title)

    # what to type into the search box
    q = base
    if kind == "tv" and season and season > 1 and not season_of(base):
        q += f" Season {season}"
    if part and part > 1 and not part_of(base):
        q += f" Part {part}"
    if kind == "movie" and "movie" not in norm(base):
        q += " Movie"
    if kind == "ova" and "ova" not in norm(base):
        q += " OVA"

    alt_q = None
    if alt:
        modifier = (not alt[0].isupper()) or alt.isdigit() or re.search(
            r"\b(arc|season|final|provisional|movie|part)\b", alt.lower())
        alt_q = f"{base} {alt}" if modifier else alt
    return {"base": base, "alt": alt, "alt_q": alt_q, "query": q, "kind": kind,
            "season": season, "part": part}


# ---------------------------------------------------------------- scoring

def titles_of(c):
    t = c.get("title") or {}
    out = [t.get("english"), t.get("romaji")] + list(c.get("synonyms") or [])
    return [x for x in out if x]


def score(want, c, rank):
    names = titles_of(c)
    if not names:
        return -999, "no title"
    sim = max(max(similarity(want["query"], n) for n in names),
              max(similarity(strip_markers(want["base"]), strip_markers(n)) for n in names) - 0.05)
    if want["alt"]:
        sim = max(sim, max(similarity(want["alt"], n) for n in names) - 0.03)
    s = sim * 100
    notes = []

    # does the match contain the words you typed? (catches arc-named seasons)
    stop = {"the", "a", "an", "of", "in", "to", "and", "is", "on", "no", "wo", "ga", "ni", "de", "season", "part"}
    mine = set(strip_markers(want["base"]).split()) - stop
    if mine:
        theirs = set()
        for n in names:
            theirs |= set(norm(n).split())
        cover = len(mine & theirs) / len(mine)
        s += 20 * (cover - 1)

    # season
    c_seasons = {season_of(n) for n in names[:2]} - {None}
    c_season = min(c_seasons) if c_seasons else None
    w_season = want["season"] or 1
    if c_season is None and "final season" in norm(" ".join(names[:2])) \
            and "final" in norm(want["base"] + " " + (want["alt"] or "")):
        c_season = w_season  # "The Final Season" counts as whichever season you meant
    if want["kind"] == "tv":
        if (c_season or 1) == w_season:
            s += 12 if (c_season or w_season == 1) else 0
        elif c_season is None:
            s -= 12  # unnumbered entry - could be a subtitled sequel, could be season 1
            notes.append(f"no season number (wanted {w_season})")
        else:
            s -= 30
            notes.append(f"season {c_season} vs {w_season}")

    # part
    c_parts = {part_of(n) for n in names[:2]} - {None}
    c_part = max(c_parts) if c_parts else 1
    w_part = want["part"] or 1
    if c_part != w_part:
        s -= 18
        notes.append(f"part {c_part} vs {w_part}")

    # format
    f = c.get("format") or ""
    k = want["kind"]
    if k == "movie":
        s += 10 if f == "MOVIE" else -20
    elif k in ("ova", "special"):
        s += 10 if f in ("OVA", "SPECIAL", "ONA", "TV_SHORT") else -12
    else:
        if f in ("TV", "ONA", "TV_SHORT"):
            s += 6
        elif f in ("MOVIE", "SPECIAL", "OVA", "MUSIC"):
            s -= 25
            notes.append(f"is a {f.lower()}")

    s += max(0, 6 - rank)  # AniList's own ranking counts a little
    if not c.get("idMal"):
        s -= 60
        notes.append("not on MAL")
    return s, ", ".join(notes)


def confidence(s):
    return "high" if s >= 100 else "check" if s >= 78 else "low"


def label(c):
    t = c.get("title") or {}
    return t.get("english") or t.get("romaji") or "?"


def best_match(api, want):
    queries = [want["query"]]
    if want["base"] != want["query"]:
        queries.append(want["base"])
    if want["alt_q"]:
        queries.append(want["alt_q"] + (f" {want['season']}" if (want["season"] or 1) > 1 else ""))

    # simpler spellings to try if AniList finds nothing useful
    def clean(t):
        t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').translate(LOOKALIKES)
        t = re.sub(r"\([^)]*\)", " ", t)
        t = re.sub(r"[~<>\"!?.,:;#]+", " ", t)
        t = re.sub(r"\b(movie|tv|classic series|original series)\b", " ", t, flags=re.I)
        t = re.sub(r"\s+\d+$", "", t.strip())
        return re.sub(r"\s+", " ", t).strip()
    base_clean = clean(want["base"])
    words = base_clean.split()
    extra = [clean(want["query"]), base_clean]
    if words and words[0].lower() == "the":
        extra.append(" ".join(words[1:]))
    if len(words) > 4:
        extra.append(" ".join(words[:4]))
    for e in extra:
        if e and e not in queries and len(e) > 2:
            queries.append(e)

    seen, scored = set(), []
    for i, q in enumerate(queries):
        for rank, c in enumerate(api.search(q)):
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            sc, notes = score(want, c, rank)
            scored.append((sc, notes, c))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= (100 if i == 0 else 90):
            break  # good enough - skip the extra searches
    return scored


# ---------------------------------------------------------------- dates + XML

def to_iso(d):
    d = (d or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", d)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", d)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return "0000-00-00"


def write_xml(path, entries):
    out = ['<?xml version="1.0" encoding="UTF-8" ?>', "<myanimelist>", "  <myinfo>",
           "    <user_export_type>1</user_export_type>",
           f"    <user_total_anime>{len(entries)}</user_total_anime>",
           "    <user_total_watching>0</user_total_watching>",
           f"    <user_total_completed>{len(entries)}</user_total_completed>",
           "    <user_total_onhold>0</user_total_onhold>",
           "    <user_total_dropped>0</user_total_dropped>",
           "    <user_total_plantowatch>0</user_total_plantowatch>",
           "  </myinfo>"]
    for e in entries:
        c = e["media"]
        eps = c.get("episodes") or 0
        title = (c.get("title") or {}).get("romaji") or label(c)
        out += ["  <anime>",
                f"    <series_animedb_id>{c['idMal']}</series_animedb_id>",
                f"    <series_title><![CDATA[{title.replace(']]>', ']] >')}]]></series_title>",
                f"    <series_type>{FORMAT_TO_MAL.get(c.get('format') or '', 'Unknown')}</series_type>",
                f"    <series_episodes>{eps}</series_episodes>",
                "    <my_id>0</my_id>",
                f"    <my_watched_episodes>{eps}</my_watched_episodes>",
                "    <my_start_date>0000-00-00</my_start_date>",
                f"    <my_finish_date>{escape(e['finished'])}</my_finish_date>",
                "    <my_score>0</my_score>",
                "    <my_status>Completed</my_status>",
                "    <my_rewatching>0</my_rewatching>",
                "    <my_times_watched>0</my_times_watched>",
                "    <update_on_import>1</update_on_import>",
                "  </anime>"]
    out.append("</myanimelist>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


# ---------------------------------------------------------------- made-up dates

def _d(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return datetime.date(y, max(m, 1), max(d, 1))


def generate_dates(results, sheet_dates):
    """Shows that ended before you started watching get a plausible date in your
    first stretch of watching: spread out, seeded so re-runs give the same dates,
    seasons of the same show kept in order and before any later season you have."""
    start = _d(STARTED_WATCHING)
    first_real = min(sheet_dates) if sheet_dates else "2025-12-01"
    end = _d(first_real) - datetime.timedelta(days=1)

    def franchise(res):
        words = strip_markers(res["want"]["base"]).split()
        words = [w for w in words if w not in ("the", "a", "an")]
        return " ".join(words[:2]) or res["row"]["title"]

    rng = random.Random(2024)
    today = datetime.date.today()
    # aired after you started: finished within a couple of weeks of the last episode
    for x in results:
        if x["date_from"] == "last air date" and x["aired"] >= STARTED_WATCHING:
            d = _d(x["aired"]) + datetime.timedelta(days=rng.randint(1, 14))
            x["finish"] = min(d, today).isoformat()
            x["date_from"] = "generated"

    todo = [x for x in results if x["date_from"] == "last air date"
            and x["aired"] and x["aired"] < STARTED_WATCHING]
    if not todo:
        return
    # later seasons that have a real/air date set the deadline for the older ones
    deadline = {}
    for x in results:
        if x in todo or not x.get("finish") or x["finish"] == "0000-00-00":
            continue
        f = franchise(x)
        d = _d(x["finish"])
        if start < d and (f not in deadline or d < deadline[f]):
            deadline[f] = d

    groups = {}
    for x in todo:
        groups.setdefault(franchise(x), []).append(x)
    for g in groups.values():
        g.sort(key=lambda x: x["aired"])

    capped = {f: g for f, g in groups.items() if f in deadline and deadline[f] <= end}
    free = [g for f, g in groups.items() if f not in capped]
    rng.shuffle(free)

    # free groups: spread evenly over the whole stretch, a little jitter
    flat = [x for g in free for x in g]
    span = (end - start).days
    for i, x in enumerate(flat):
        base = start + datetime.timedelta(days=round((i + 0.5) * span / len(flat)))
        jitter = rng.randint(-2, 2)
        x["finish"] = (base + datetime.timedelta(days=jitter)).isoformat()
        x["date_from"] = "generated"
    # keep seasons in order after jitter
    for g in free:
        for a, b in zip(g, g[1:]):
            if b["finish"] <= a["finish"]:
                b["finish"] = (_d(a["finish"]) + datetime.timedelta(days=3)).isoformat()

    # capped groups: in the weeks before the later season
    for f, g in capped.items():
        d = deadline[f]
        for x in reversed(g):
            d = d - datetime.timedelta(days=rng.randint(7, 21))
            x["finish"] = max(d, start).isoformat()
            x["date_from"] = "generated"


# ---------------------------------------------------------------- main

def read_sheet(path):
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = list(csv.reader(f))
    start = 0
    for i, r in enumerate(reader):
        if r and r[0].strip().lower() == "title":
            start = i + 1
            break
    for i, r in enumerate(reader[start:], start=start + 1):
        r = (r + ["", "", ""])[:3]
        if r[0].strip():
            rows.append({"line": i, "title": r[0].strip(), "season": r[1].strip(),
                         "finished": r[2].strip()})
    return rows


def load_review(path):
    """Your overrides and finish dates from the last match_review.csv."""
    ov, dates = {}, {}
    if not os.path.exists(path):
        return ov, dates
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            key = (r.get("your_title", ""), r.get("your_season", ""))
            v = (r.get("override") or "").strip()
            if v:
                ov[key] = v
            if "finish_date" in r:  # column exists: whatever you left there wins, blank included
                dates[key] = ((r.get("finish_date") or "").strip(), (r.get("date_from") or "").strip())
    return ov, dates


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    folder = os.path.dirname(os.path.abspath(src))
    xml_path = os.path.join(folder, "mal_import.xml")
    review_path = os.path.join(folder, "match_review.csv")
    api = AniList(os.path.join(folder, ".anilist_cache.json"))

    rows = read_sheet(src)
    overrides, saved_dates = load_review(review_path)
    print(f"{len(rows)} titles in your sheet. Looking them up "
          f"(first run takes ~{len(rows) * DELAY * 1.4 / 60:.0f} min; re-runs are quick)...\n")

    results = []
    for n, r in enumerate(rows, 1):
        want = parse_row(r["title"], r["season"])
        key = (r["title"], r["season"])
        ov = overrides.get(key, "")
        res = {"row": r, "want": want, "override": ov, "media": None,
               "score": 0, "notes": "", "others": []}

        if ov.lower() == "skip":
            res["conf"] = "skipped"
        elif ov:
            mal_id = re.search(r"\d+", ov)
            m = api.by_mal(mal_id.group()) if mal_id else None
            if m:
                res.update(media=m, conf="override")
            elif mal_id:
                # AniList doesn't list this one separately - MAL still takes the ID
                res.update(media={"idMal": int(mal_id.group()), "format": "", "episodes": 0,
                                  "title": {"romaji": r["title"]}, "synonyms": []},
                           conf="override", notes="not on AniList, MAL only")
            else:
                res.update(conf="low", notes=f"override '{ov}' isn't a MAL ID")
        else:
            scored = best_match(api, want)
            if scored:
                sc, notes, c = scored[0]
                res.update(media=c, score=round(sc), notes=notes, conf=confidence(sc),
                           others=[x[2] for x in scored[1:4]])
                if not c.get("idMal"):
                    res["conf"] = "low"
            else:
                res.update(conf="low", notes="no results")

        tag = {"high": "ok ", "check": "?  ", "low": "!! ", "override": "set", "skipped": "-- "}[res["conf"]]
        got = label(res["media"]) if res["media"] else "(nothing)"
        print(f"[{n:3}/{len(rows)}] {tag} {r['title']} {r['season']}".rstrip() + f"  ->  {got}")
        results.append(res)

    # finish dates: your edits > your sheet's Finished column > the show's last air date
    ids = sorted({x["media"]["id"] for x in results if x["media"] and x["media"].get("id")})
    if ids:
        print(f"\nLooking up air dates for {len(ids)} shows...")
    aired = api.end_dates(ids) if ids else {}
    sheet_dates = [to_iso(x["row"]["finished"]) for x in results]
    sheet_dates = [d for d in sheet_dates if d != "0000-00-00"]
    for res in results:
        r, c = res["row"], res["media"] or {}
        key = (r["title"], r["season"])
        sheet = to_iso(r["finished"])
        end = aired.get(c.get("id"), "")
        res["aired"] = end
        saved, saved_from = saved_dates.get(key, (None, ""))
        if saved is None:                      # never reviewed: start from sheet or air date
            if sheet != "0000-00-00":
                res["finish"], res["date_from"] = sheet, "your sheet"
            elif end:
                res["finish"], res["date_from"] = end, "last air date"
            else:
                res["finish"], res["date_from"] = "", ""
        elif not saved:                        # you cleared it: stays blank
            res["finish"], res["date_from"] = "", ""
        elif saved_from == "last air date" and to_iso(saved) == end:
            res["finish"], res["date_from"] = end, "last air date"   # untouched default
        elif saved_from == "generated":
            res["finish"], res["date_from"] = to_iso(saved), "generated"  # kept as-is once made
        elif saved_from == "your sheet" and to_iso(saved) == sheet:
            res["finish"], res["date_from"] = sheet, "your sheet"
        else:
            res["finish"], res["date_from"] = to_iso(saved), "you"
    generate_dates(results, sheet_dates)

    # review file: least sure first
    order = {"low": 0, "check": 1, "high": 2, "override": 3, "skipped": 4}
    with open(review_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["confidence", "your_title", "your_season", "finish_date", "date_from",
                    "last_aired", "matched_title",
                    "type", "episodes", "mal_id", "mal_link", "why_unsure",
                    "other_candidates", "override"])
        for res in sorted(results, key=lambda x: (order[x["conf"]], x["score"])):
            r, c = res["row"], res["media"] or {}
            mid = c.get("idMal") or ""
            w.writerow([res["conf"], r["title"], r["season"], res["finish"], res["date_from"],
                        res["aired"],
                        label(c) if c else "", FORMAT_TO_MAL.get(c.get("format") or "", ""),
                        c.get("episodes") or "", mid,
                        f"https://myanimelist.net/anime/{mid}" if mid else "",
                        res["notes"],
                        " | ".join(f"{label(o)} [{o.get('idMal') or 'not on MAL'}]"
                                   for o in res["others"]),
                        res["override"]])

    # XML: everything with a MAL ID, one entry per show (latest finish date wins)
    by_id = {}
    for res in results:
        c = res["media"]
        if res["conf"] in ("skipped", "low") or not c or not c.get("idMal"):
            continue  # unsure matches stay out until you confirm them with an override
        fin = res["finish"] or "0000-00-00"
        old = by_id.get(c["idMal"])
        if not old or fin > old["finished"]:
            by_id[c["idMal"]] = {"media": c, "finished": fin}
    write_xml(xml_path, list(by_id.values()))

    counts = {k: sum(1 for x in results if x["conf"] == k) for k in order}
    print(f"\nDone. {len(by_id)} shows written to {xml_path}")
    print(f"  confident: {counts['high']}   worth a glance: {counts['check']}   "
          f"likely wrong/missing: {counts['low']}   overridden: {counts['override']}   "
          f"skipped: {counts['skipped']}")
    print(f"  (rows that point to the same MAL entry were merged)")
    print("  ('likely wrong' rows are left out of the XML until you give them an override)")
    print(f"\nReview {review_path} - the rows at the top need you most.")
    print("Put a MAL ID or 'skip' in the override column, save, and run again.")
    print("Dates: edit or clear the finish_date column (date_from shows where each came from).")


if __name__ == "__main__":
    main()
