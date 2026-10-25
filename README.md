# anime-list-tools

Two small Python scripts for moving an anime watch list between a spreadsheet,
[AniList](https://anilist.co), [MyAnimeList](https://myanimelist.net), and any site
that imports MyAnimeList's XML format.

I built these to get ~400 shows out of a Google Sheet (kept because the streaming
sites I used kept shutting down) and onto AniList, then synced to my watch site.

Both scripts use only Python's standard library — nothing to install — and look
things up through AniList's free public API (no account or key needed).

## `anime_to_mal.py` — spreadsheet → MAL XML

Takes a CSV with columns `Title`, `Season/Part`, `Finished` and produces a
MyAnimeList import file.

```
python3 anime_to_mal.py "my list.csv"
```

It creates, next to your CSV:

- **`mal_import.xml`** — upload this to MyAnimeList or AniList's import page
- **`match_review.csv`** — every row, what it matched, and how sure it is (least sure at the top)
- **`.anilist_cache.json`** — saved lookups, so re-runs take seconds

**How matching works:** each title is searched on AniList and the results are scored
on title similarity, season/part number, and format (TV vs. movie vs. OVA).
If a search finds nothing, it retries with simpler spellings. Rows it isn't sure
about are left out of the XML until you confirm them.

**Fixing matches:** in `match_review.csv`, put the right MAL ID in the `override`
column (the number in a `myanimelist.net/anime/<ID>/` link), or `skip` to leave a
row out, then run again.

**Finish dates:** the `finish_date` column is what goes into the XML. Dates from
your sheet are kept; empty ones are filled in automatically:

- shows that ended before `STARTED_WATCHING` (top of the script) get a generated
  date spread across your early watching period, with seasons kept in order
- shows that ended after it get a date within two weeks of the final episode

Edit or clear any cell and your change sticks on later runs. `date_from` shows where
each date came from.

## `anilist_to_mal.py` — AniList export → MAL XML

AniList doesn't export MAL XML directly. Download your data from AniList
(the GDPR data download in your account settings), then:

```
python3 anilist_to_mal.py gdpr_data.json
```

This writes `anilist_export.xml` with every anime entry's status, progress, dates,
score, and rewatch count. It lists any completed shows missing a finish date, and
any shows that have no MAL ID (those can't go in a MAL-format file).

## Things to know

- **Western animation** (Castlevania, Arcane, etc.) isn't on MAL or AniList.
- **MAL splits seasons more finely than AniList.** Some MAL entries, like a
  season's second cour, are folded into one AniList entry and won't match on AniList.
- **Imports add and update but never delete.** Removing a show means doing it by hand.
- **AniList rate-limits its API** (~30–90 requests/min). The first run of
  `anime_to_mal.py` on a few hundred titles takes around 20 minutes; after that
  everything is cached.
