# Summer League Explorer — Advanced Metrics as First-Class Citizens

**Status:** Spec / blueprint (Jun 27 2026)
**Author kickoff:** exploration session on `feature/infographics-skill`
**Depends on:** materialized `summer_league_player_seasons` (PR #357/#358), explorer base build (PR #365)

## 1. Problem

The Summer League Explorer (`/stats/summer-league/explorer`) is a faceted query
builder over players / teams / games, but **advanced metrics are not first-class**.
The full Basketball-Reference advanced suite is materialized in
`summer_league_player_seasons` (27 columns, 100% populated on `adv_eligible` rows),
yet the explorer surfaces almost none of it:

| Grain | Data source today | Advanced metrics shown |
|---|---|---|
| Career (year-range/venue pool) | sums raw `SummerLeaguePlayerGameLog` | **TS% only** |
| Per-competition, *single* comp (one year + one venue, `adv_eligible`) | reads `summer_league_player_seasons` | TS% + PER, ORtg, DRtg, BPM, WS, VORP (7) |
| Per-competition, multi-comp / Per-game | game logs | none beyond TS% |

So composites surface in exactly one narrow corner (single-competition view),
expose only 7 of ~25 advanced columns, and are **not sortable or filterable**
anywhere else.

**Goal:** make the advanced suite first-class across the explorer — visible,
sortable, and filterable at every applicable grain — without misrepresenting the
metrics' statistical meaning.

## 2. The core constraint

Composite metrics (PER, BPM±O/D, ORtg/DRtg, WS) are **recalibrated per
competition pool**: PER is centered to ~15 *within* a single competition, BPM to
0.0, WS off that pool's Pythagorean expectation. Therefore a player's PER cannot
be naively averaged across, say, Vegas-2023 + Orlando-2024 — the underlying scales
differ. The explorer's value proposition is *multi-pool* exploration (year ranges,
all venues, draft classes), which collides with per-pool calibration. Today the
collision is resolved by hiding composites outside a single pool.

## 3. Resolution — metric taxonomy + grain rule

Every stat falls into one of three buckets; each rolls up differently across a
multi-pool selection:

1. **Exact-recombinable from box totals** — TS%, eFG%, 3PAr, FTr, pts/100.
   Recompute from summed makes/attempts. *Valid at any grain, exact.*
2. **Additive shares** — WS, OWS, DWS, cumulative VORP, GameScore.
   *Sum across pools, exact.*
3. **Per-pool rate / centered composites** — PER, BPM (+O/D), ORtg, DRtg, NetRtg,
   USG%, AST%, ORB%, DRB%, TRB%, STL%, BLK%, TOV%.
   *Minute-weighted average across pools, labeled "avg," approximate.*

This mirrors the grain rule already established for the player-page career rollup
in `summer_league_metrics_service`; here it is generalized to the explorer's
arbitrary pools.

**Product decisions (confirmed Jun 27 2026):**
- **Composite pooling = pooled average, labeled.** At multi-pool grain, rate/centered
  composites show as minute-weighted averages, visibly marked approximate with a
  per-pool tooltip. Interpretation: "average performance relative to each
  summer-league field the player faced." Additive shares show as exact sums;
  exact-recombinable show plainly.
- **Filter UX = generic metric-filter builder.** 1–3 repeatable rows of
  `(metric ▾) (≥ / ≤) (value)`, server-validated against the column catalog,
  applied as a HAVING clause on the rolled-up values.

## 4. Target design

### 4.1 Unify the player data source on `summer_league_player_seasons`
The season table has a row per player-competition (2887 rows; the 886
sub-threshold rows carry box stats with NULL composites) **and** full box totals.
So career + per-competition grains can both read it losslessly while gaining
composites for free. Per-game grain stays on `SummerLeaguePlayerGameLog` (it is
inherently per-game). The career query becomes a roll-up of per-competition rows
rather than a raw game-log sum — simpler and pre-aggregated.

**Lossless box parity is the acceptance bar:** career-grain box totals (GP, MIN,
PTS, REB, …) computed from the season table must equal the current game-log-sum
values for every player.

### 4.2 Column catalog as data
Replace the ad-hoc `_PLAYER_STAT_COLUMNS` / `_PLAYER_ADVANCED_COLUMNS` lists with a
declarative catalog entry per column:
`{ key, label, group (box|shooting|advanced), bucket (recombinable|additive|rate_composite), rollup_fn, sortable, filterable, fmt }`.
Drives column rendering, sort-key validation, filter-metric options, and rollup
behavior from one place.

### 4.3 Advanced columns first-class at all player grains
- Sortable everywhere (whitelist generated from the catalog, not hand-maintained).
- At multi-pool grain: additive = exact sum, rate_composite = minute-weighted avg
  with an "avg" marker + tooltip, recombinable = exact.
- Generalize the `adv_eligible` banner to: "N of M competitions in this pool
  qualify for composites" (rate composites null-skip in the weighted average).

### 4.4 Generic metric-filter builder
- Server: parse repeatable `fcol`/`fop`/`fval` params into validated predicates;
  apply as HAVING on rolled-up expressions; validate metric keys against the
  catalog `filterable` flag (reject/ignore unknown keys, never 500).
- UI: up to 3 filter rows in the form; "add filter" reveals the next row; metric
  dropdown sourced from the catalog. Reuse the existing AJAX partial swap.
- URL-encode filters in the shareable query string (`explorer_qs` macro).

### 4.5 Honest labeling
Per the project's labeling / analytical-voice principles: pooled composites carry a
visible "avg" marker and a tooltip naming the caveat; the eligibility banner states
coverage explicitly. No UI implies a single rigorous cross-pool composite.

## 5. Phased implementation

- **P0 — Catalog + taxonomy (foundation).** Declarative column catalog; classify all
  ~25 advanced cols into the three buckets with rollup functions. Pure refactor;
  unit-tested.
- **P1 — Unify player source on the season table.** Repoint career + per-competition
  grains; implement the three rollup paths. Acceptance: lossless box parity vs.
  current game-log sums. `make perf` (expected flat or lower) + `make explain` on a
  prod branch *once the table is deployed there* (see §6).
- **P2 — Expose advanced columns + sorting at all player grains** with labeling;
  catalog-driven sort whitelist; generalized eligibility banner.
- **P3 — Generic metric-filter builder** (server validation + HAVING + minimal JS).
- **P4 — Teams subject advanced** (ORtg/DRtg/Pace/NetRtg from box-log averages).
  Games subject has little advanced surface; out of initial scope.
- **P5 — Polish.** CSV export carries advanced cols; column-group show/hide toggle;
  per-pool drill-down from a pooled row.

## 6. Perf, indexing, data-availability notes

- No schema changes — every column already exists in `summer_league_player_seasons`.
- Perf budget currently 9 for the explorer route (`tests/integration/perf/budgets.py`).
  Unifying on the season table should keep it flat or reduce it; confirm with `make perf`.
  **Query count is the primary perf guard** (the explorer's real risk is N+1 / aggregate
  cost, not single-table scans).
- `make explain` **works today** against the prod-read branch (`EXPLAIN_DATABASE_URL` →
  `ep-dawn-meadow`). Verified Jun 27 2026: the table is present there (3,164 rows) and
  indexed on `(year, venue_slug)`, `competition_id`, `player_id`, and unique
  `(competition_id, player_id)` — exactly the explorer's access patterns. The harness
  (`scripts/explain_route.py`) accepts any URL path including a query string, so run e.g.
  `make explain ROUTE='/stats/summer-league/explorer?subject=players&grain=career'`
  (and a filtered variant) on the **final** feature to verify performance.
- **Interpretation note:** at ~3k rows the season table is small enough that a Seq Scan
  may be the *correct* plan even on the prod branch — do not treat that as a regression.
  Explain is here to catch pathological plans on the new rollup aggregation + HAVING
  (external sorts, >=10x row-estimate misses, spills), not to demand Index Scans on a
  tiny table.

## 7. Files in play

| Purpose | Path |
|---|---|
| Explorer service (catalog, query builders, parse/validate, rollups) | `app/services/summer_league_explorer_service.py` |
| Advanced metrics read service (reuse grain rule helpers) | `app/services/summer_league_metrics_service.py` |
| Route | `app/routes/summer_league.py` |
| Template (full) | `app/templates/stats/summer-league/explorer.html` |
| Template (results fragment) | `app/templates/stats/summer-league/_explorer_results.html` |
| JS enhancer (filter rows, AJAX swap) | `app/static/summer-league-explorer.js` |
| Season-table schema (reference; no changes) | `app/schemas/summer_league_metrics.py` |
| Perf budget | `tests/integration/perf/budgets.py` |
| Tests | `tests/integration/test_summer_league_explorer.py` |

## 8. Open questions / follow-ups

- Should pooled-composite rows expose a one-click "show per-competition breakdown"
  (P5 drill-down) or is the tooltip sufficient at launch?
- Default sort when advanced columns are first shown at career grain (suggest WS or
  VORP — exact additive shares — rather than a pooled-avg composite).
- Teams-subject advanced scope (P4) — confirm which composites are trustworthy from
  box-log averages given plus_minus is only ~68% populated.
