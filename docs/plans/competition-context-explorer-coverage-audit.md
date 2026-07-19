# Competition Context Explorer — Coverage Audit

**Status:** Phase 0 audit (issue #616) · **Generated:** 2026-07-19T05:07:56.948647+00:00 · **Source DB:** `ep-ancient-flower-adlbmq0x-pooler.c-2.us-east-1.aws.neon.tech` · **Audit version:** 1

Read-only, reproducible inventory of what the Competition Context profiles (#606/#617) can publish honestly. Regenerate with `scripts/audit_summer_league_environment_coverage.py`. This report mutates no raw or derived Summer League fact.

## Honest historical trend range

- **Box-complete season span:** **2021–2025**. Only these years certify the box-derived environment metrics (pace, ORtg, shooting, turnover, assisted-FG) for every eligible final game of the all-competitions season rollup.
- Metrics remain in the schema for every year; sparse years publish `partial`/`unavailable` per metric rather than narrowing the product. Trend charts must show gaps, not interpolate.

## Season rollups (all competitions per year)

| Year | Comps | Final | Box✓ | Shot✓ | PBP✓ | Appeared (canon) | Unresolved | Draft known | Age known | Pos known | Origin known |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2007 | 1 | 55 | 50 | 55 | 0 | 144 | 31 | 67% | 91% | 91% | 91% |
| 2008 | 1 | 53 | 53 | 53 | 0 | 168 | 46 | 57% | 83% | 83% | 83% |
| 2009 | 1 | 55 | 55 | 55 | 0 | 172 | 58 | 59% | 88% | 88% | 88% |
| 2010 | 2 | 78 | 78 | 78 | 0 | 184 | 44 | 68% | 89% | 89% | 89% |
| 2011 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a |
| 2012 | 2 | 80 | 1 | 80 | 0 | 223 | 108 | 64% | 93% | 93% | 93% |
| 2013 | 2 | 86 | 3 | 86 | 0 | 218 | 93 | 61% | 91% | 91% | 91% |
| 2014 | 2 | 92 | 2 | 92 | 0 | 233 | 104 | 58% | 91% | 91% | 91% |
| 2015 | 3 | 98 | 1 | 98 | 0 | 219 | 127 | 56% | 93% | 93% | 93% |
| 2016 | 3 | 98 | 0 | 98 | 0 | 219 | 143 | 53% | 91% | 91% | 91% |
| 2017 | 3 | 93 | 93 | 93 | 0 | 204 | 66 | 58% | 94% | 94% | 94% |
| 2018 | 3 | 94 | 94 | 94 | 0 | 225 | 104 | 57% | 95% | 95% | 95% |
| 2019 | 3 | 94 | 94 | 94 | 94 | 253 | 222 | 44% | 94% | 94% | 94% |
| 2020 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a |
| 2021 | 3 | 85 | 85 | 85 | 85 | 262 | 168 | 52% | 95% | 94% | 95% |
| 2022 | 3 | 87 | 87 | 87 | 87 | 257 | 183 | 50% | 93% | 90% | 93% |
| 2023 | 3 | 88 | 88 | 88 | 88 | 267 | 172 | 55% | 93% | 87% | 93% |
| 2024 | 3 | 94 | 94 | 94 | 94 | 283 | 180 | 58% | 94% | 84% | 94% |
| 2025 | 3 | 88 | 88 | 88 | 88 | 280 | 173 | 63% | 89% | 81% | 89% |
| 2026 | 3 | 90 | 89 | 87 | 88 | 287 | 172 | 83% | 85% | 59% | 91% |

## Per-metric season certifiability

How many of the 20 season profiles certify each v1 metric (`complete` = every eligible final game carries the input).

| Metric | Source | Complete | Partial | Unavailable |
| --- | --- | --- | --- | --- |
| `points_per_team_game` | box | 11 | 6 | 3 |
| `estimated_possessions` | box | 11 | 6 | 3 |
| `pace_per_48` | box | 11 | 6 | 3 |
| `offensive_rating` | box | 11 | 6 | 3 |
| `three_attempt_share` | box | 11 | 6 | 3 |
| `three_fg_pct` | box | 11 | 6 | 3 |
| `free_throw_rate` | box | 11 | 6 | 3 |
| `offensive_rebound_rate` | box | 11 | 6 | 3 |
| `turnover_rate` | box | 11 | 6 | 3 |
| `assisted_fg_rate` | box | 11 | 6 | 3 |
| `rim_attempt_share` | shot | 17 | 1 | 2 |
| `rim_fg_pct` | shot | 17 | 1 | 2 |
| `average_score_margin` | score | 18 | 0 | 2 |
| `close_game_share` | score | 18 | 0 | 2 |
| `overtime_share` | ot_state | 1 | 0 | 19 |
| `team_ortg_iqr` | box | 11 | 6 | 3 |
| `top_decile_minutes_share` | box | 11 | 6 | 3 |
| `top_decile_points_share` | box | 11 | 6 | 3 |

## Individual competitions

| Year | Venue | Final | Box✓ | Shot✓ | PBP✓ | Score✓ | OT state✓ | Appeared | Unresolved |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2007 | las_vegas | 55 | 50 | 55 | 0 | 55 | 0 | 144 | 31 |
| 2008 | las_vegas | 53 | 53 | 53 | 0 | 53 | 0 | 168 | 46 |
| 2009 | las_vegas | 55 | 55 | 55 | 0 | 55 | 0 | 172 | 58 |
| 2010 | las_vegas | 58 | 58 | 58 | 0 | 58 | 0 | 142 | 35 |
| 2010 | orlando | 20 | 20 | 20 | 0 | 20 | 0 | 58 | 13 |
| 2011 | las_vegas | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2011 | orlando | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2012 | las_vegas | 60 | 1 | 60 | 0 | 60 | 0 | 175 | 87 |
| 2012 | orlando | 20 | 0 | 20 | 0 | 20 | 0 | 65 | 29 |
| 2013 | las_vegas | 61 | 3 | 61 | 0 | 61 | 0 | 164 | 73 |
| 2013 | orlando | 25 | 0 | 25 | 0 | 25 | 0 | 82 | 31 |
| 2014 | las_vegas | 67 | 2 | 67 | 0 | 67 | 0 | 184 | 79 |
| 2014 | orlando | 25 | 0 | 25 | 0 | 25 | 0 | 76 | 36 |
| 2015 | las_vegas | 67 | 1 | 67 | 0 | 67 | 0 | 171 | 99 |
| 2015 | orlando | 25 | 0 | 25 | 0 | 25 | 0 | 64 | 45 |
| 2015 | salt_lake_city | 6 | 0 | 6 | 0 | 6 | 0 | 31 | 15 |
| 2016 | las_vegas | 67 | 0 | 67 | 0 | 67 | 0 | 171 | 108 |
| 2016 | orlando | 25 | 0 | 25 | 0 | 25 | 0 | 65 | 49 |
| 2016 | salt_lake_city | 6 | 0 | 6 | 0 | 6 | 0 | 28 | 17 |
| 2017 | las_vegas | 67 | 67 | 67 | 0 | 67 | 0 | 167 | 57 |
| 2017 | orlando | 20 | 20 | 20 | 0 | 20 | 0 | 55 | 13 |
| 2017 | salt_lake_city | 6 | 6 | 6 | 0 | 6 | 0 | 29 | 12 |
| 2018 | california_classic | 6 | 6 | 6 | 0 | 6 | 0 | 23 | 17 |
| 2018 | las_vegas | 82 | 82 | 82 | 0 | 82 | 0 | 223 | 104 |
| 2018 | salt_lake_city | 6 | 6 | 6 | 0 | 6 | 0 | 29 | 13 |
| 2019 | california_classic | 6 | 6 | 6 | 6 | 6 | 0 | 30 | 25 |
| 2019 | las_vegas | 82 | 82 | 82 | 82 | 82 | 0 | 250 | 220 |
| 2019 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 0 | 30 | 28 |
| 2020 | california_classic | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2020 | las_vegas | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2020 | salt_lake_city | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2021 | california_classic | 4 | 4 | 4 | 4 | 4 | 0 | 31 | 23 |
| 2021 | las_vegas | 75 | 75 | 75 | 75 | 75 | 0 | 259 | 156 |
| 2021 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 0 | 28 | 24 |
| 2022 | california_classic | 6 | 6 | 6 | 6 | 6 | 0 | 26 | 30 |
| 2022 | las_vegas | 75 | 75 | 75 | 75 | 75 | 0 | 255 | 180 |
| 2022 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 0 | 42 | 15 |
| 2023 | california_classic | 6 | 6 | 6 | 6 | 6 | 0 | 49 | 31 |
| 2023 | las_vegas | 76 | 76 | 76 | 76 | 76 | 0 | 263 | 171 |
| 2023 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 0 | 31 | 20 |
| 2024 | california_classic | 12 | 12 | 12 | 12 | 12 | 0 | 56 | 54 |
| 2024 | las_vegas | 76 | 76 | 76 | 76 | 76 | 0 | 273 | 155 |
| 2024 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 0 | 37 | 19 |
| 2025 | california_classic | 6 | 6 | 6 | 6 | 6 | 0 | 39 | 24 |
| 2025 | las_vegas | 76 | 76 | 76 | 76 | 76 | 0 | 275 | 171 |
| 2025 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 0 | 37 | 20 |
| 2026 | california_classic | 12 | 12 | 12 | 12 | 12 | 12 | 71 | 45 |
| 2026 | las_vegas | 72 | 71 | 69 | 70 | 72 | 72 | 279 | 165 |
| 2026 | salt_lake_city | 6 | 6 | 6 | 6 | 6 | 6 | 39 | 22 |

## Schema / index notes for #606 and #617

- **No stored profile table exists yet.** `#606` must add the `scope_key`
  (`season:<year>` / `competition:<competition_id>`) projection with a partial
  unique index on the current row. This audit reads only raw spokes.
- **Overtime is not a normalized fact.** OT is inferred from
  `SummerLeagueGame.status_text ILIKE '%OT%'`; games with a null `status_text`
  have unknown OT state. `overtime_share` is a confidence-badged metric, not a
  certified count, until a normalized OT flag exists.
- **Shot/PBP coverage is proxied by parsed event rows**, not by raw-file parse
  status. A game with zero shot events is treated as shot-uncovered; #617
  should reconcile against `summer_league_raw_files.parse_status` for the
  `shotchartdetail`/`playbyplay` endpoints before certifying `shot_complete`.
- **Origin has no event-time affiliation source in the box spoke.** This audit
  approximates origin from current `players_master.birth_country`/`school`,
  which the contract (§5) forbids as the published source. #617 must resolve a
  pre-event affiliation/participation origin; treat the origin distribution as
  provisional until then.
- **Position** is read from the canonical `player_status.position_id` taxonomy
  (falling back to the near-empty `players_master.position`). Per §5 the
  *event-time* participation/roster position is preferred; that lives in
  `summer_league_participation.roster_position` and
  `summer_league_player_game_logs.starter_position` and is only ~24% populated,
  so #617 must decide the event-time-vs-canonical precedence explicitly rather
  than assuming the canonical value is the answer.
- Aggregation reads should key competition scope through the existing
  `ix_summer_league_team_game_logs_competition_team` and
  `ix_summer_league_player_game_logs_competition_player` indexes; a rebuild
  keyed by `game_id` uses `ix_summer_league_shot_events_game_id`.


## Method and honesty rules

- **Eligible games:** only `status = 'final'` games contribute metric inputs
  and appeared players. Scheduled/in-progress/postponed/canceled/unknown games
  are reported as status counts but never as numerators/denominators.
- **Appeared player:** a player-game log with `minutes_seconds > 0` in an
  eligible final game. DNP shells are excluded. Distinct people use canonical
  `player_id`; unresolved appearances are counted separately by
  `source_player_id` and never silently dropped.
- **Box complete (per game):** exactly two team-box rows with all of
  fga/fgm/fg3a/fta/oreb/dreb/tov/pts/minutes non-null.
- **Coverage verdict (per metric, per scope):** `complete` when every eligible
  final game carries the input, `partial` when some do, `unavailable` when
  none do or there are no eligible games. `partial` is never shown as zero.
- **Season dedup:** canonical appeared players and attribute known/total are
  recomputed at year grain so a player at multiple venues counts once;
  additive game/coverage counts are summed across competitions.

