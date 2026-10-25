#!/usr/bin/env python3
"""
anilist_to_mal.py - turn your AniList data export (gdpr_data.json) into a
MyAnimeList-format XML file you can import into other anime sites.

Usage:
    python3 anilist_to_mal.py gdpr_data.json

Makes anilist_export.xml next to the JSON, and lists any entries that have
no MyAnimeList ID (those can't go in a MAL-format file - add them by hand).

Only uses Python's standard library. Looks up titles and MAL IDs through
AniList's free public API, 50 shows per request.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://graphql.anilist.co"

QUERY = """
query ($ids: [Int], $page: Int) {
  Page(page: $page, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id idMal format episodes
      title { romaji english }
    }
  }
}"""

# AniList export status codes
STATUS = {0: "Watching", 1: "Plan to Watch", 2: "Completed", 3: "Dropped",
          4: "On-Hold", 5: "Watching"}  # 5 = rewatching

FORMAT_TO_MAL = {"TV": "TV", "TV_SHORT": "TV", "MOVIE": "Movie", "SPECIAL": "Special",
                 "OVA": "OVA", "ONA": "ONA", "MUSIC": "Music"}


def post(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(6):
        req = urllib.request.Request(
            API, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "anilist-to-mal-script"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            wait = int(e.headers.get("Retry-After", "30")) if e.code == 429 else 5 * (attempt + 1)
            print(f"   AniList said {e.code}, waiting {wait}s...")
            time.sleep(wait + 1)
        except Exception as e:
            print(f"   network error ({e}), retrying...")
            time.sleep(5 * (attempt + 1))
    raise SystemExit("Couldn't reach AniList. Check your internet and try again.")


def lookup(ids):
    info = {}
    for k in range(0, len(ids), 50):
        chunk = ids[k:k + 50]
        print(f"   looking up {k + 1}-{k + len(chunk)} of {len(ids)}...")
        data = post(QUERY, {"ids": chunk, "page": 1})
        for m in ((data.get("data") or {}).get("Page") or {}).get("media") or []:
            info[m["id"]] = m
        time.sleep(2.2)
    return info


def date(n):
    """20250115 -> 2025-01-15; missing -> 0000-00-00"""
    s = str(n or 0)
    if len(s) == 8 and s != "00000000":
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return "0000-00-00"


def score10(s):
    s = s or 0
    return round(s / 10) if s > 10 else round(s)  # 100-point scores become 1-10


def cdata(t):
    return "<![CDATA[" + (t or "").replace("]]>", "]] >") + "]]>"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    out_path = os.path.join(os.path.dirname(os.path.abspath(src)), "anilist_export.xml")

    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("lists") or []
    anime = [e for e in entries if e.get("series_type") == 0]
    other = len(entries) - len(anime)
    print(f"{len(anime)} anime in your export" + (f" ({other} manga entry skipped)" if other else "") + ".")

    info = lookup(sorted({e["series_id"] for e in anime}))

    rows, no_mal = [], []
    for e in anime:
        m = info.get(e["series_id"])
        if not m or not m.get("idMal"):
            t = (m or {}).get("title") or {}
            no_mal.append(t.get("english") or t.get("romaji") or f"AniList id {e['series_id']}")
            continue
        rows.append((e, m))

    counts = {}
    for e, _ in rows:
        st = STATUS.get(e.get("status"), "Completed")
        counts[st] = counts.get(st, 0) + 1

    out = ['<?xml version="1.0" encoding="UTF-8" ?>', "<myanimelist>", "  <myinfo>",
           "    <user_export_type>1</user_export_type>",
           f"    <user_total_anime>{len(rows)}</user_total_anime>",
           f"    <user_total_watching>{counts.get('Watching', 0)}</user_total_watching>",
           f"    <user_total_completed>{counts.get('Completed', 0)}</user_total_completed>",
           f"    <user_total_onhold>{counts.get('On-Hold', 0)}</user_total_onhold>",
           f"    <user_total_dropped>{counts.get('Dropped', 0)}</user_total_dropped>",
           f"    <user_total_plantowatch>{counts.get('Plan to Watch', 0)}</user_total_plantowatch>",
           "  </myinfo>"]
    no_date = []
    for e, m in sorted(rows, key=lambda r: r[0].get("finished_on") or 0):
        t = m.get("title") or {}
        title = t.get("romaji") or t.get("english") or ""
        st = STATUS.get(e.get("status"), "Completed")
        eps = m.get("episodes") or 0
        watched = e.get("progress") or 0
        if st == "Completed" and eps:
            watched = eps
        if st == "Completed" and not e.get("finished_on"):
            no_date.append(t.get("english") or title)
        out += ["  <anime>",
                f"    <series_animedb_id>{m['idMal']}</series_animedb_id>",
                f"    <series_title>{cdata(title)}</series_title>",
                f"    <series_type>{FORMAT_TO_MAL.get(m.get('format') or '', 'Unknown')}</series_type>",
                f"    <series_episodes>{eps}</series_episodes>",
                "    <my_id>0</my_id>",
                f"    <my_watched_episodes>{watched}</my_watched_episodes>",
                f"    <my_start_date>{date(e.get('started_on'))}</my_start_date>",
                f"    <my_finish_date>{date(e.get('finished_on'))}</my_finish_date>",
                f"    <my_score>{score10(e.get('score'))}</my_score>",
                f"    <my_status>{st}</my_status>",
                f"    <my_rewatching>{1 if e.get('status') == 5 else 0}</my_rewatching>",
                f"    <my_times_watched>{e.get('repeat') or 0}</my_times_watched>",
                f"    <my_comments>{cdata(e.get('notes'))}</my_comments>",
                "    <update_on_import>1</update_on_import>",
                "  </anime>"]
    out.append("</myanimelist>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    print(f"\nDone. {len(rows)} shows written to {out_path}")
    print("  " + "   ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if no_date:
        print(f"\nCompleted but no finish date on AniList ({len(no_date)}):")
        for t in no_date:
            print("   -", t)
    if no_mal:
        print(f"\nNot on MyAnimeList, so left out - add these by hand ({len(no_mal)}):")
        for t in no_mal:
            print("   -", t)


if __name__ == "__main__":
    main()
