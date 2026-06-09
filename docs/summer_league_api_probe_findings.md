# Summer League — NBA Stats API Probe Findings

**Date:** 2026-05-30
**Probe script:** `scripts/probe_summer_league_api.py`
**Raw snapshots:** `scripts/data/sl_probe/*.json` (65 files)
**Goal:** Validate the `docs/summer_league_stats_plan.md` assumption that `stats.nba.com` is a
viable primary source for Summer League, and map LeagueID / season-format / field availability
before any schema work.

## TL;DR

- **Access required a fix.** `stats.nba.com` tarpits generic HTTP clients (httpx, curl) and even
  Playwright by TLS/JA3 fingerprint. The working client is **`curl_cffi==0.7.4` with
  `impersonate="chrome"`** (newer 0.15.0 fails to load on macOS arm64 — `_CFRelease` symbol).
- **Once impersonating Chrome, the API is rich and fully usable.** Box, advanced, scoring,
  play-by-play, and shot-chart endpoints all return data for modern Summer Leagues.
- **LeagueID → venue mapping is now resolved** (the spec was guessing).
- **Entity anchors confirmed:** boxscore rows carry the NBA `PLAYER_ID` (= PERSON_ID) and a
  league/season-encoding `GAME_ID` — exactly what `player_external_ids` (`system="nba_stats"`)
  and idempotent re-pulls need.

## Access: the one real obstacle (now solved)

`stats.nba.com` sits behind Akamai bot-management that **fingerprints the TLS/JA3 handshake** and
**tarpits** non-browser clients — the connection establishes to the edge node and then hangs until
timeout (not a 403). Verified dead ends: `httpx`, `curl`, `curl -4`, `curl --http1.1`, Playwright
Chromium (`ERR_HTTP2_PROTOCOL_ERROR`), `cdn.nba.com` (403), `data.nba.net` (dead cert). The
nba.com *homepage* loads fine over httpx, so egress is not the problem — the block is host +
fingerprint specific.

**Fix:** `curl_cffi` (libcurl + BoringSSL, reproduces Chrome's JA3).

```
conda run -n draftguru pip install "curl_cffi==0.7.4"
```

Pin **0.7.4** — 0.15.0 installs but raises `ImportError: symbol not found '_CFRelease'` on this
macOS arm64 env. The probe script uses `curl_cffi.requests.Session(impersonate="chrome")` and lets
impersonation supply the browser headers; only the nba-specific headers (`Referer`, `Origin`,
`x-nba-stats-origin`, `x-nba-stats-token`) are set explicitly. Typical latency 2.5–5 s/call.

> **Productionization note:** `curl_cffi` is currently a probe-only dependency (not in
> `environment.yml`). The real SL ingestion service will need it (or `tls-client` / a proxy /
> real-Chrome-via-CDP) as a first-class dependency. Also note: the existing combine scraper's
> `stats.nba.com` "API fallback" (`scripts/nba_draft_scraper.py:506`) would tarpit the same way —
> its working path is headless-Chromium HTML, not the JSON API.

## LeagueID → venue map (RESOLVED)

Identified from `TEAM_NAME`, host team, dates, and team counts per league/season:

| LeagueID | Venue | Evidence | Coverage |
|----------|-------|----------|----------|
| **15** | **Las Vegas Summer League** (main event) | 30 teams, Jul 12–22 (2024), ~150 team-games/yr | **2010 → present** |
| **14** | **Orlando Pro Summer League** (defunct) | 40 rows (2010), 50 (2015), 0 after | 2010, 2015; gone by 2019 |
| **13** | **California Classic** (Sacramento-hosted) | 8 teams incl. "Sacramento Kings 1/2", Jul 6–10 (2024) | 2019 → present |
| **16** | **Salt Lake City Summer League** (Utah-hosted) | 4 teams incl. Utah Jazz, Jul 8–10 (2024) | 2015 → present |
| 00 | NBA (control) | 2460 rows/season | all years |

So a single SL "season" is **multi-venue** and must be queried as **multiple LeagueIDs** — Vegas
(15) is the spine; 13/14/16 are the satellite events. This validates the spec's
`summer_league_seasons (year, venue, ...)` model.

## Season-string format (RESOLVED)

`leaguegamelog` accepts a **bare four-digit year** (`Season=2024`) — not the `2024-25` form. NBA
control returned a full 2460-row season for `Season=2004`, confirming the format works; SL leagues
simply have no rows that far back.

## Coverage matrix — team-games in `leaguegamelog` (PlayerOrTeam=T)

| LeagueID | 2004 | 2010 | 2015 | 2019 | 2021 | 2024 | 2025 |
|----------|-----:|-----:|-----:|-----:|-----:|-----:|-----:|
| 00 (NBA) | 2460 | 2460 | 2460 | 2118 | 2460 | 2460 | 2460 |
| 13 (CA Classic) | 0 | 0 | 0 | 12 | 8 | 24 | 12 |
| 14 (Orlando) | 0 | 40 | 50 | 0 | 0 | 0 | 0 |
| 15 (Vegas) | 0 | 116 | 134 | 164 | 150 | 152 | 152 |
| 16 (Utah/SLC) | 0 | 0 | 12 | 12 | 12 | 12 | 12 |

**Backfill floor is ~2010, not 2004.** Nothing for any SL league in 2004. Vegas (15) reaches back
to 2010; Orlando (14) exists only at 2010 & 2015 in this sample. The spec's "2004–2014 deep
backfill" and "Orlando 2002–2017" ambitions are **not supported by this endpoint** for the early
years — backfill should be scoped to **Vegas 2010+** with satellite venues where present.

## Per-game tier availability (drill-downs)

One `GAME_ID` drilled per era. Endpoints: `boxscoretraditionalv2` / `boxscoreadvancedv2` /
`boxscorescoringv2` / `playbyplayv2` / `shotchartdetail`. Row counts:

| Year (league drilled) | Box trad (PlayerStats) | Advanced | Scoring | **PBP events** | Shot chart |
|---|---:|---:|---:|---:|---:|
| 2025 (L13) | 33 | 33 | 33 | **431** | 131 |
| 2024 (L13) | 28 | 28 | 28 | **418** | 142 |
| 2019 (L13) | 24 | 24 | 24 | **384** | 130 |
| 2015 (L14 Orlando) | **0** | 0 | 0 | **0** | 148 |
| 2010 (L14 Orlando) | 11 (partial) | 11 | 11 | **0** | 142 |

Reads:
- **Tier 3 (PBP) is available 2019 → present** (~380–430 events/game) → on/off, lineups, real pace,
  accurate USG%/rate stats are computable for the modern era. **Absent in 2010.** PBP floor is
  somewhere 2011–2018 (a finer probe of 2013/2016/2018 would pin it).
- **Tier 1–2 (box + advanced + scoring + shot chart) available 2019 → present** and partially 2010.
- **Orlando (L14) box data is unreliable** even when the team gamelog exists — the 2015 drill
  returned 0 boxscore rows for a game that appears in `leaguegamelog`. For old/Orlando games, the
  **team-level box from `leaguegamelog` is more trustworthy** than `boxscoretraditionalv2`.
- `shotchartdetail` returned rows even where box was empty because it keys off Season; treat its
  counts as season-scoped, not strictly per-game, until params are tightened.

## Entity-resolution anchors (CONFIRMED)

- **`boxscoretraditionalv2` PlayerStats** carries `PLAYER_ID` + `PLAYER_NAME` (+ TEAM_ID,
  START_POSITION, full box). Sample: `1629639 = "Tyler Herro"` — that is the NBA **PERSON_ID**,
  stable across NBA/SL/G-League. → resolve via `player_external_ids (system="nba_stats")` exactly
  as the spec planned.
- **`GAME_ID` encodes league + season**: `15`2`24`00076 = Vegas/2024/#76; `14`2`15`00021 =
  Orlando/2015/#21. Stable, parseable, ideal for idempotent re-pulls (persist as an external id).
- **`commonallplayers` does NOT support SL LeagueIDs** — returned empty for L13 every year. SL
  player enumeration must come from **gamelogs/boxscores**, not a roster endpoint. (Use
  `leaguegamelog?PlayerOrTeam=P` or per-game boxscores to harvest the player universe.)

## Updates this implies for `docs/summer_league_stats_plan.md`

1. **Data Acquisition → Primary source:** add the hard caveat — `stats.nba.com` requires a
   TLS-impersonating client (`curl_cffi`); it is not reachable from a plain client. Make
   "establish access method" a prerequisite ticket gating all SL work.
2. **LeagueID table (lines 42–47):** replace guesses with the resolved map above (15=Vegas,
   14=Orlando, 13=California Classic, 16=Salt Lake City).
3. **Backfill scope:** floor at **2010 (Vegas)**, not 2004. Drop the 2002–2009 Orlando ambition or
   flag it as manual-only; the API has nothing there.
4. **Granularity:** confirm Tier-3 (PBP) for **2019+**; treat 2011–2018 as "probe further", and
   2010-and-earlier as Tier-1 (box) only, with Orlando box gaps expected.
5. **Player enumeration:** drop `commonallplayers` for SL; harvest players from gamelogs/boxscores.
6. **Season format:** four-digit year string.

## Suggested next probe (optional, ~30 min)
- Pin the PBP floor: drill `playbyplayv2` for one Vegas (L15) game in 2013, 2016, 2018.
- Re-run drill-downs against **Vegas (L15)** specifically (this pass drilled L13/L14 because the
  target-picker chose the first non-NBA league with data); confirm box/PBP completeness on the
  main event rather than the satellites.

## Re-running

```
conda run -n draftguru python scripts/probe_summer_league_api.py --verbose --timeout 20
```
Raw JSON → `scripts/data/sl_probe/`; coverage matrix + tier summary → stdout.

For production raw snapshot collection, use
`scripts/fetch_summer_league_raw.py` instead of the probe. See
`docs/summer_league_raw_ingestion.md`.
