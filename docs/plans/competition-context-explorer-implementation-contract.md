# Competition Context Explorer — Implementation Contract

**Status:** Approved implementation contract · **Date:** 2026-07-19

**Parent documents:**

- `docs/plans/competition-context-explorer-first-release.md`
- `docs/plans/competition-context-explorer-qa-checklist.md`
- `docs/plans/event-environment-intelligence-pitch.md`

This document freezes the data, URL, operational, and performance decisions that workers
must share when implementing GitHub Project #15. It narrows ambiguity in the product
documents; it does not add NBA comparison, player outcomes, or translation scores to the
first release.

## 1. Pre-implementation coverage audit

Implementation starts with a reproducible audit of the production-like Summer League
spoke. The audit must report, by year and competition:

- normalized competitions and final/scheduled/in-progress/postponed/canceled games;
- final games with two complete team-box rows;
- final games with successfully parsed shot-chart and PBP inputs;
- resolved and unresolved appeared-player identities;
- availability of event-time draft status, age, position, and origin inputs;
- the number of competition and all-competitions season profiles that would certify
  each v1 metric.

The audit produces `docs/plans/competition-context-explorer-coverage-audit.md` and a
machine-readable CSV/JSON artifact from a repeatable read-only script. It does not change
raw facts. Metrics remain in the schema even when the audit shows sparse history, but the
report determines the honest trend range and which field-composition distributions can be
published rather than silently inferred.

## 2. Scope and profile identity

There are exactly two profile scope kinds:

| Scope kind | Stable scope key | Membership |
| --- | --- | --- |
| `season_all_competitions` | `season:<year>` | Every normalized Summer League competition in that calendar year |
| `competition` | `competition:<competition_id>` | Exactly one `SummerLeagueCompetition.id` |

An all-competitions season membership row names every normalized competition, including a
competition with zero final games. Only eligible final games contribute play metrics. The
profile separately records scheduled/in-progress/postponed/canceled games so an ongoing
season is distinguishable from missing data.

Persist a non-null `scope_key`; do not rely on a nullable `competition_id` for uniqueness.
Profile identity survives projection rebuilds. Projection row IDs are never public URL
identifiers.

Each profile records at least:

- `scope_kind`, `scope_key`, `year`, and optional `competition_id`;
- membership and per-member contribution counts;
- calculation and metric-definition versions;
- `computed_at`, input watermark, raw-run/source references, and coverage counts;
- `is_current` plus the values and unavailable reasons consumed by public reads.

If historical calculation versions are retained, enforce one current row per `scope_key`
with a partial unique index. Public queries always select current rows explicitly.

## 3. Eligible games and coverage

### Final-game policy

Only `SummerLeagueGame.status == FINAL` contributes basketball and appeared-player facts.
Scheduled, in-progress, postponed, canceled, and unknown games never contribute metric
numerators or denominators. They remain visible as schedule/status counts.

### Coverage vocabulary

Every metric carries one of:

- `complete` — every eligible final game has every required input and the metric may be
  displayed and filtered;
- `partial` — some eligible games have the required input; retain counts and reason but
  publish a null metric value;
- `unavailable` — no valid denominator or no required input; publish null plus reason.

Partial coverage is never extrapolated and never displayed as zero. Event completeness
(`is_event_complete`) is separate from data coverage: an in-progress competition can have
complete data through its currently final games.

Coverage is established from audited input/file status, not merely the existence of one
fact row:

- **Box complete:** every eligible game has exactly two usable team-box rows with all
  fields required by the metric.
- **Shot complete:** every eligible game has a successfully parsed shot-chart input;
  unknown/unmapped non-backcourt zones or reconciliation failures prevent certification
  and record a reason.
- **PBP complete:** every eligible game has a successfully parsed PBP input.
- **Identity coverage:** resolved and unresolved appeared-player counts and missing
  attributes are retained separately. Identity gaps do not invalidate box/shot metrics.

PBP completeness is an informational confidence badge/filter in v1. No displayed v1
metric requires PBP because assisted-FG rate is deliberately the box-derived `AST / FGM`
measure. A later PBP-only metric must be added to the registry before PBP can gate a
displayed value.

## 4. Certified metric registry

Implement one shared registry in code rather than duplicating labels/formulas in services
and templates. Each entry defines:

```text
metric_key, display_label, profile_section, source_fields, formula,
denominator, unit, scale, rounding, required_coverage, filterable,
sortable, eligible_scopes, definition_version, interpretation_note
```

Projection columns used by sorting and threshold filters remain typed numeric columns;
do not hide the filterable value set solely inside JSONB.

### Core environment metrics

All rates recompute from pooled totals. Percentages use the 0–100 display scale.

| Key | Formula / definition | Coverage |
| --- | --- | --- |
| `points_per_team_game` | `total_pts / team_game_rows` | box |
| `estimated_possessions` | Sum the existing opponent-adjusted `Box.poss` result at team-game grain | box |
| `pace_per_48` | `48 * estimated_possessions / (team_minutes / 5)` | box |
| `offensive_rating` | `100 * total_pts / estimated_possessions` | box |
| `three_attempt_share` | `100 * fg3a / fga` | box |
| `three_fg_pct` | `100 * fg3m / fg3a` | box |
| `free_throw_rate` | `100 * fta / fga` | box |
| `offensive_rebound_rate` | `100 * oreb / (oreb + opponent_dreb)` from paired team totals | box |
| `turnover_rate` | `100 * tov / (fga + 0.44 * fta + tov)` | box |
| `assisted_fg_rate` | `100 * ast / fgm`; group measure, never player AST% | box |
| `rim_attempt_share` | `100 * restricted_area_fga / mapped_non_backcourt_fga` | shot |
| `rim_fg_pct` | `100 * restricted_area_fgm / restricted_area_fga` | shot |
| `average_score_margin` | Mean `abs(home_score - away_score)` across eligible games | game/final score |
| `close_game_share` | `100 * games_with_abs_margin_lte_5 / eligible_games` | game/final score |
| `overtime_share` | `100 * confirmed_overtime_games / games_with_known_ot_state` | game/status text |

Reuse/extract `app.services.summer_league.metrics.Box.poss`; do not create a competing
possession formula. Zero denominators return null, never `0.0`.

### Performance landscape

- `team_ortg_iqr`: team-entry offensive-rating 75th percentile minus 25th percentile.
- `top_decile_minutes_share`: share of appeared-player minutes held by the top `ceil(10%)`
  of appeared players.
- `top_decile_points_share`: equivalent share of points.
- The leaders strip is presentation over existing Players Explorer results and must link
  to the exact profile scope. It is not stored as a second leaderboard source.

## 5. Field-composition contract

An **appeared player** has a player-game log in an eligible final game with positive
minutes. DNP shells do not count as appearances. Use canonical `player_id` for distinct
people and retain separately:

- distinct unresolved `source_player_id` appearances;
- participation/roster rows;
- player-game rows;
- canonical players appearing in more than one competition in a season profile.

Every distribution includes known, unknown, and total counts. Unknown attributes never
disappear from a denominator without disclosure.

- **First-time/returner:** appearance rank is the player's distinct Summer League
  calendar-year rank across all competitions; rank 1 is first-time.
- **Drafted entering the event:** `draft_year <= profile year` with known draft round.
  Players drafted later are labeled `not yet drafted`, not retrospectively undrafted.
- **Draft bands:** lottery is round 1, picks 1–14; later first round and second round are
  separate. Shares use all resolved appeared players with known event-time draft status;
  show the unknown count.
- **Age:** age on competition `starts_on`; for a season profile use the player's first
  eligible appearance date in that year. Fall back to July 1 only when the date is absent
  and expose fallback coverage.
- **Position:** prefer event participation/roster position normalized through the
  canonical position taxonomy; current canonical position is a labeled fallback.
- **College/international origin:** use the latest supported pre-event affiliation or
  participation source. Do not infer historical origin from current biography text. The
  coverage audit may leave this distribution unavailable until provenance is sufficient.

V1 threshold filters include only registry-certified numeric field facts: team count,
first-time share, drafted-entering-event share, lottery share, and median age. Expanded
categorical composition filters and origin breakdown filters are second-wave controls.

## 6. URL, detail, trend, and export contract

Do not overload the existing player `grain` parameter. Competition state uses:

```text
subject=competitions
profile_scope=season|competition
year_min=<year>&year_max=<year>
venue=<slug>
min_gp=<int>
coverage=all|box_complete|shot_complete|pbp_complete
trend_metric=<registered metric key>
competition_id=<stable SummerLeagueCompetition.id>
detail_year=<year>
sort=<registered key>&dir=asc|desc&page=<int>
fcol0=<registered key>&fop0=<gte|lte|eq>&fval0=<number>
```

Metric thresholds reuse the Explorer's existing indexed `fcol{i}` / `fop{i}` /
`fval{i}` contract for `i = 0..2`; do not introduce parallel metric-specific query
parameters. Competition requests accept only registry entries marked filterable for the
selected profile scope. Invalid or incomplete predicates are dropped with visible
validation state and never broaden another scope parameter.

- `detail_year` identifies a season detail; `competition_id` identifies a competition
  detail. Projection row IDs are not accepted.
- Season scope clears `venue` and `competition_id` during canonicalization.
- Competition detail treats `competition_id` as authoritative; inconsistent year/venue
  inputs are removed rather than broadening the scope.
- Invalid metric keys, ranges, or identifiers degrade to a visible validation/empty state
  and never silently broaden results.
- Season trend has one point per surviving year and visible gaps.
- Competition trend renders only after one venue/competition series is selected; the
  unfiltered competition table prompts for a venue rather than combining unrelated
  competitions into one line.
- Competition list/detail CSV ships in v1 and includes stable scope identifiers, values,
  definitions/units, coverage counts/status, calculation version, and freshness. CSV and
  HTML use the same result contract.

To preserve exact competition handoffs, `subject=players`, `subject=teams`, and
`subject=games` (labeled **Matchups** in the UI) accept an optional validated
`competition_id` scope. An all-competitions season handoff uses a pinned year with no
venue/competition ID. Presentation-only player/draft/stat filters never alter the
environment profile summarized in a context strip.

## 7. Public-surface reuse

- Player, Team, and Matchup context strips render only when the query resolves to one
  approved profile. Ambiguous scopes show a link to choose a profile and no values.
- The season hub keeps its component competition/venue cards and adds one explicitly
  labeled all-competitions summary. The aggregate never replaces or masquerades as the
  venue portfolio.
- The venue page renders exactly one competition profile.
- Every consumer receives the same service DTO, values, definitions, coverage, and
  calculation version; routes/templates never recompute metrics.

## 8. Rebuild, backfill, and operational publication

The aggregation command supports one competition, one year, and full historical rebuilds.
It is deterministic and set-based rather than issuing per-profile/per-player query loops.

Publication requirements:

1. Begin the rebuild transaction and acquire the established transaction-scoped Summer
   League advisory/write lock **before** reading any source fact, derived metric, current
   profile, or input watermark used by the rebuild.
2. Hold that same lock and transaction through source loading, watermark capture,
   calculation, validation, version insertion, and the current-version switch. Do not
   commit, release the lock, or move calculation reads to another session between those
   steps.
3. Insert the validated version and atomically switch `is_current`; one commit publishes
   both. Any failure rolls back the candidate and leaves the prior current profile
   readable.
4. An incremental refresh invoked from the existing locked metrics/materialization phase
   reuses that session and transaction. A manual or standalone rebuild opens its own
   transaction and acquires the same lock before its first input read.
5. Never mutate raw Summer League facts.
6. Emit requested/built/skipped/failed scope counts, metric coverage counts, version,
   input watermark, duration, and failure reasons.

This ordering is mandatory because the existing lock uses `pg_advisory_xact_lock`. Atomic
publication alone is insufficient: reading inputs before acquiring the lock could combine
raw facts and derived metrics from different writer snapshots even if the final row switch
is atomic.

The historical backfill runs before public modules are enabled. The normal Summer League
pipeline invokes an incremental profile rebuild after normalized facts/advanced metrics
are materialized. Retries are idempotent. A runbook documents manual rebuild, inspection,
rollback to the prior current version, and recovery from a stale/failed run.

Public DTOs expose `computed_at` and input watermark. A profile beyond the configured
freshness threshold remains readable as the last good version but displays a stale badge;
it is never silently replaced by request-time aggregation.

## 9. Performance contract

- `/stats/summer-league/explorer?subject=competitions`, including list, filtered trend,
  and selected detail, stays at or below the existing 10-query route budget.
- Competition requests load subject-specific facets only; they do not execute draft,
  country, position, or team facet queries intended for player results.
- `partial=1` costs no more queries than the corresponding full render.
- Season and venue pages add at most one indexed profile read, for a maximum expected
  route budget of 9 unless the profile is folded into an existing query.
- No member-, metric-, competition-, team-, leader-, or source-per-row query loops.
- Public reads touch current profile/membership tables only; raw game/shot/PBP aggregation
  is offline.
- Profile scope/current lookups, membership reads, and any source-reference reads are
  covered by production-like EXPLAIN evidence. A rebuild query keyed directly by
  competition must use an indexed path through `game_id` or add the required
  competition-leading index.

## 10. Deterministic verification data

Integration and browser/visual verification must use a repeatable seed containing:

- two competitions in one year with unequal denominators and one repeat player;
- at least two years for one venue series;
- complete-box/complete-shot, box-only, partial, and unavailable profiles;
- scheduled, in-progress, postponed, canceled, final, and overtime games;
- resolved/unresolved players and known/unknown field attributes;
- a stale prior profile and a failed replacement attempt that preserves it.

The visual harness must capture season list/detail/trend, competition list/detail/trend,
partial coverage, empty/invalid, cross-subject strip, season/venue reuse, and desktop/mobile
states from deterministic data rather than whichever records happen to exist locally.
