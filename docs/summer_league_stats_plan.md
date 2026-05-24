# Summer League Stats — Planning Doc

Brainstorming notes capturing decisions made on 2026-05-23 for a new "Summer League stats" feature. Intended as a reference for later implementation; not a final spec.

## Vision

A "basketball-reference for NBA Summer League" — a comprehensive, evergreen statistical reference covering ~10 years of Summer League data, with strong support for cross-linked exploration of players, teams, seasons, and games. Surfaces both raw box-score data and advanced stats, with explicit sample-size caveats.

The product extends DraftGuru's positioning naturally: from "pre-draft prospect data" → "early NBA performance data," bridging the gap between scouting and outcomes. Summer League coverage is genuinely underserved (Basketball-Reference is thin, NBA.com Stats is clunky), making this both a content gap to fill and a defensible niche.

## Scope Decisions

- **Evergreen, not seasonal.** Historical archive going back ~10 years, queryable any time. Live updates during July SL, but the product exists year-round.
- **Phased backfill.** Current year live → modern (2015+) backfill → deep backfill (2004–2014) → opportunistic Orlando-era (2002–2017). Earlier years gated on data quality; quality may degrade and partial coverage is acceptable with a "data quality: limited" badge.
- **Advanced stats included, with caveats.** Compute the full bbref-style advanced suite (TS%, eFG%, USG%, AST%, TRB%, STL%, BLK%, TOV%, per-36, per-100). Surface sample-size warnings inline; never hide the numbers.
- **Per-venue, per-season normalization.** Compute league-context stats (pace, league averages) **per (year, venue)**, never use NBA-season averages as a stand-in.
- **NBA Summer League only.** Vegas, Salt Lake, California Classic, and historical Orlando. Not G-League Winter Showcase, not Drew League. SL is the natural draft-adjacent dataset; mission-creep into G-League / early-career NBA is deferred to a separate future feature.

## Data Acquisition

### Primary source
**NBA.com Stats API** (`stats.nba.com`). Free, undocumented, rate-limited. SL has dedicated `LeagueID` values (Vegas = `14`; Utah/CA vary). Most mature option despite quirks.

### Architecture principles
- Snapshot raw responses to S3 so re-parsing is free.
- Pipeline stages: `raw → normalized → derived`. Scraper writes to raw store; normalization is a separate step. Never write directly to player tables from the scraper.
- Module location: `app/services/summer_league/` with clear separation between fetch, parse, resolve, and persist.

### Granularity tiers (what data unlocks what stats)

| Tier | Ingested | Computable |
|------|---------|------------|
| 1. Box totals | Final stat lines per player per game | Raw box, shooting splits, per-36, basic team stats |
| 2. Box + shot detail | Tier 1 + shot location/distance per FGA | TS%, eFG%, shot zones, shot charts |
| 3. Play-by-play | Tier 2 + every event with timestamp + players on court | On/off splits, lineup data, +/-, real possessions, real pace, accurate rate stats (USG%, etc.) |
| 4. Tracking | Player coordinates, defensive matchups | Not available for SL — skip |

**Target: tier 3 (PBP) where available** (~2015+). Tier 1 is the fallback for 2004–2014. Schema designed for PBP from day one; `data_quality` enum on `summer_league_seasons` (`box_only` / `full` / `partial`) lets UI degrade gracefully.

### Era-by-era best-guess availability (needs API probe to verify)

| Era | Venue(s) | NBA.com API | PBP | Target |
|-----|----------|-------------|-----|--------|
| 2016–present | Vegas + Utah + Cal | Solid | Yes | Full tier 3 |
| 2010–2015 | Vegas + Orlando | Mostly | Spotty | Tier 1 guaranteed, tier 3 best-effort |
| 2004–2009 | Vegas + Orlando | Box only, holes | Rare | Tier 1, accept missing games |
| 2002–2003 | Orlando only | Unreliable | No | Skip or manual |

**Pre-implementation probe:** spend ~2 hours hitting NBA.com Stats endpoints for SL games across 2004, 2010, 2015, 2020, 2024. Capture raw JSON, document field-level availability. Grounds the project in reality vs. assumptions.

## Player Entity Resolution

### Fit with existing identity model
Existing infrastructure (investigated 2026-05-23) is a good fit:

- **`player_external_ids`** (`app/schemas/player_external_ids.py`) — `(system, external_id)` shape is exactly right for NBA.com `PERSON_ID`. Use `system = "nba_stats"`. NBA.com PERSON_IDs are stable across SL / NBA / G-League / historical — one mapping resolves a player across all data sources.
- **`PlayerAlias`** (`app/schemas/player_aliases.py`) — store per-roster spelling variants with `context = "summer_league_{year}"`.
- **`PlayerMaster.is_stub`** — battle-tested pattern from news-mentions feature. SL-only players (never drafted, never NBA) get stubs.

### Resolution tiers
Expected distribution across ~3,000–4,000 historical SL players:

- **Tier 1 (~25%):** NBA `PERSON_ID` matches a player who later played NBA. Easy link via external ID.
- **Tier 2 (~30%):** Matches a draft prospect already tracked. Fuzzy match on `(name, draft_year)`. Easy stub link.
- **Tier 3 (~45%):** Undrafted, never made NBA, often international. Create stub. Never enrich. Accept they exist only as SL entities.

### Required additions to existing model
- **Add fuzzy matching.** Current resolution is exact-after-normalization. For SL rosters we'll see "Cam Reddish" vs "Cameron Reddish," "RJ Hampton" vs "R.J. Hampton." Extend `app/services/player_mention_service.py:463-490` with a `rapidfuzz` tier (e.g., `token_sort_ratio >= 92`). Surface ambiguous matches to admin review queue rather than auto-creating.
- **New `bio_source` value:** `"summer_league_ingest"` for tier-3 stubs. **Skip the Gemini enrichment pipeline entirely** for these — the SL stats are the canonical record; there's nothing to enrich from public web sources for an obscure 2008 SL guy.
- **Admin review queue.** Ambiguous tier-2 matches go here; bulk SL ingestion is too high-volume for inline review.

### Explicit decisions
- SL stats live in their own `summer_league_*` tables, **not** in a `PlayerCollegeStats` analogue.
- SL players are first-class `PlayerMaster` rows; **don't** prefix slugs with `sl-`.
- Stubs for SL-only players never go through Gemini enrichment.

## Stat Inventory

Organized by required granularity. Comprehensive ingestion — show everything, with sample-size context.

### Tier 1 (box-only sufficient)
**Per-game totals:** MIN, PTS, FGM/FGA/FG%, 3PM/3PA/3P%, FTM/FTA/FT%, ORB, DRB, TRB, AST, STL, BLK, TOV, PF, +/-

**Per-game derived:** per-36 of all above, GameScore (Hollinger)

**Season aggregates:** totals, per-game averages, single-game highs/lows

**Shooting splits (basic):** 2P/3P/FT splits, AST:TO ratio

### Tier 1 + season-level league context
- **TS%, eFG%** — need league average context per `(season, venue)`
- **Rate stats (per-100 possessions)** — *only valid if team possessions known*; tier 3 ideal, tier 1 only via estimation

### Tier 3 (PBP required for accuracy)
- USG%, AST%, TRB%, ORB%, DRB%, STL%, BLK%, TOV% — all need on-court teammate stats
- True pace, possessions
- On/off splits, lineup +/-, lineup ORtg/DRtg
- Clutch splits (last 5 min, score within 5)
- Shot location distributions (if shot detail captured)

### Composite metrics — proceed with caution
- **BPM, VORP** — formulas calibrated to regular season; require recalibration for SL or accepting noise
- **PER** — compute it but always show next to games/minutes
- **Win Shares** — pace-and-context-dependent; probably skip

### DraftGuru-specific derived stats (the moat)
- **SL performance vs pre-draft consensus** — derived rank delta
- **Cohort percentile** — rank within the player's draft class's SL performance
- **Year-N SL trajectory** — for players with multiple SL appearances
- **Career SL totals** — multi-year aggregate, distinct from per-season

## Data Model Sketch

Mirror bbref's organizational logic; resist mega-tables.

- `summer_league_seasons` — `(year, venue, date_range, data_quality)`. Venue matters: Orlando (2002–2017), Vegas (2004–), Utah (2015–19 + revival), California (2018–) ran in parallel.
- `summer_league_teams` — one row per `(team, season)`. Roster turnover means "Lakers SL 2019" ≠ "Lakers SL 2024."
- `summer_league_games` — game metadata (date, teams, venue, score, attendance if available).
- `summer_league_player_game_logs` — per-player per-game box score. Source of truth.
- **Season totals + advanced stats: materialized views or scheduled rebuild jobs**, not base tables. Recomputing is cheap and we'll iterate on advanced-stat formulas without backfill pain.

**External IDs everywhere.** Persist NBA.com `PERSON_ID` and `GAME_ID` as external IDs. They're the only stable anchors and make re-pulls idempotent.

## Information Architecture

### Nav placement
SL lives under the **Stats** tab, alongside Combine. The `/stats` URL becomes a true hub.

### URL structure (decided)
```
/stats                                              ← hub: combine preview + SL preview
/stats/combine                                      ← combine landing (migrated from /stats)
/stats/combine/{metric_key}                         ← per-metric leaderboard
/stats/combine/{draft_year}                         ← already exists
/stats/summer-league                                ← SL landing
/stats/summer-league/{year}                         ← season hub (multi-venue)
/stats/summer-league/{year}/{venue}                 ← single venue
/stats/summer-league/{year}/{venue}/{team}          ← team-season
/stats/summer-league/{year}/games/{game_id}         ← box score + PBP splits
/stats/summer-league/leaders                        ← career + season leaderboards
/stats/summer-league/explorer                       ← faceted query builder
/stats/summer-league/all-summer-league              ← historical awards
/stats/summer-league/teams/{team}                   ← franchise SL history
/players/{slug}#summer-league                       ← anchor on existing player page
/players/{slug}/summer-league/{year}                ← per-game logs for one player's SL season
```

### Migration required
`/stats` is currently the combine landing. Convert to a hub; move per-metric URLs from `/stats/{metric_key}` → `/stats/combine/{metric_key}`. Combine year URLs already at `/stats/combine/{year}`, so namespacing is half-built. Internal links to update:

- `app/templates/stats/index.html:23` — metric link
- `app/templates/stats/metric.html` — `:16`, `:130`, `:286`, `:296`, `:304`, `:311`
- `app/routes/stats.py` — split combine routes under `/stats/combine/...`
- Optional belt-and-suspenders: redirect `/stats/{metric_key}` → `/stats/combine/{metric_key}`

External link risk is essentially zero (combine metric pages not actively promoted).

### Future-proofing
The Stats-hub pattern naturally accommodates G-League, rookie-NBA, etc. as additional bands on the hub page when those land.

## Visual & Table Treatment

Comprehensive data, no fancy graphics. The differentiator is information design quality — bbref's content with thoughtful presentation.

### Pattern library
- **Heat-shaded cells** — two-color scale (pale green top quartile, pale red bottom quartile, neutral mid) on commensurate-stat columns
- **Top-N markers** — gold/silver/bronze dot next to top 3 in any leaderboard column
- **Sample-size badges** — small grey "5 GP · 87 MIN" pill next to per-game / rate stats. Italicize rate stats with <30 total minutes
- **Year-over-year arrows** — inline `↑ +2.3` chip on career tables, different color treatment than top-N
- **Sparklines** — inline trend line in career tables for multi-year SL players
- **Confidence dimming** — 90% opacity on rows where data quality is `box_only` / `partial`, with tooltip
- **Sticky header + first column** — non-negotiable for wide stat tables, critical on mobile

### Required controls
- **Per Game / Per 36 / Per 100 / Totals / Advanced toggle** above every stat table. This single control is more important than any visual treatment.
- **Min-minutes filter** on leaderboards (otherwise top of every list is a 1-game wonder).

### Cross-linking discipline
Mechanical, but the value compounds when 100% consistent:
- Player name → player SL page
- Team name → team-season page (in context) or career team page (cross-year)
- Game date or score → game box score
- Season year → season hub
- Venue name → venue-season page

### Mobile
Sticky first column with player name + team chip, horizontal-scroll for stats, "swipe left for more" affordance, "compact view" toggle dropping to PTS/REB/AST/+- only. No collapsing — analytics users want the data.

### Explorer page (power surface)
Faceted query builder, analog of Stathead Basketball. URL must fully encode query state for shareable links (most of the SEO value).

- **Subject:** players / teams / games
- **Time:** year range, single year, venue(s)
- **Player filters:** position, draft class, draft round, draft pick range, pre-draft consensus rank range, age at SL, college
- **Stat filters:** min/max on any stat, "had a game with X+ points" predicates
- **Sort:** any column, asc/desc, secondary sort
- **Output:** sortable table + save query + share URL

### DraftGuru-specific pages (the moat)
- `/stats/summer-league/{year}/draft-class` — current draft class's SL performance sorted by pick. "Did the lottery deliver?"
- **Model validation page** — sortable table of pre-draft consensus rank vs. SL composite score, biggest over/underperformers highlighted. Annual update post-SL.
- **Per-prospect SL section** on player pages — pre-draft above the fold, SL right below as the natural next chapter.
- **Cohort percentile column** — alongside raw stats, optionally show percentile within draft class.

## Open Questions

- **Backfill ceiling.** "10 years" is the appetite, but the API probe will determine what's actually feasible. Don't gate launch on historical depth.
- **Launch target.** Is the implicit deadline SL 2026 (July)? If so, scraper + schema + current-year ingestion must be done by ~June. Or off-season launch with historical depth from day one?
- **All-Summer-League historical awards** — selection methodology? Pure stats? Composite with team success? Position-balanced teams?
- **Sample-size threshold for leaderboards.** What's the qualifying minimum? bbref uses minutes thresholds for the regular season; SL needs its own.
- **Whether nav becomes a dropdown eventually** — current decision is single "Stats" link with hub-page routing. Megamenu is the eventual evolution if Stats grows past 2–3 modules.

## Suggested Next Steps

1. **API probe** — pull one game from each year 2014–2024 via NBA.com Stats API; document field-level availability across endpoints. ~2 hours.
2. **Entity resolution prepass** — script that takes one full SL season's rosters (e.g., 2024) and runs it against `PlayerMaster` + `player_external_ids`, producing match / ambiguous / no-match buckets. Validates fuzzy threshold before any SL-specific tables exist.
3. **Schema sketch** — based on probe results, draft the `summer_league_*` table set.
4. **Stats migration** — convert `/stats` to a hub; namespace combine URLs under `/stats/combine`. Independent of SL work; can ship anytime.
