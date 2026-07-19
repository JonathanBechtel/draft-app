# Summer League Explorer — League Context Test Plan

> **Superseded for planning by** `docs/plans/event-environment-intelligence-pitch.md`.
> Preserve the formula, coverage-gate, CSV, and request-path tests for the later NBA
> comparison lens; they are not the complete test plan for Event Environment Intelligence.

**Sources:**

- Product pitch: `docs/plans/summer-league-context-benchmarks-pitch.md`
- QA checklist: `docs/plans/summer-league-explorer-context-qa-checklist.md`

## Purpose

League Context is a derived, source-backed comparison between exactly one Summer League
competition and its preceding NBA regular season. The main risks are not cosmetic:
incorrectly pooling overlapping venues, averaging rate statistics instead of recomputing
them from totals, exposing partial data as a league baseline, and making a live external
API call on a public request path.

Tests must establish that the comparison is scoped, reproducible, versioned, and
reachable through the Explorer’s normal URL/CSV patterns.

## Test conventions

- Use Conda for all checks: `conda run -n draftguru <command>`.
- Pure calculation/parsing tests belong in `tests/unit/`.
- Persistence, route, CSV, and query-count checks belong in
  `tests/integration/test_summer_league_explorer_context.py` (or a focused sibling
  module if the suite grows).
- Integration tests require `TEST_DATABASE_URL` and `PYTEST_ALLOW_DB=1`; use the shared
  async `app_client` and `db_session` fixtures.
- Browser/visual evidence is post-build QA, not a replacement for formula tests.

## Required build-time tests

| Requirement | Test type | Suggested test | Expected assertion |
| --- | --- | --- | --- |
| A Context query must pin exactly one year and venue | unit | `test_parse_context_scope_requires_single_year_and_venue` | Missing venue, open range, and multi-year range produce the intentional unavailable state; a single year + venue is valid. |
| Context comparison season pairs correctly | unit | `test_context_pairs_sl_2025_with_nba_2024_25` | A Summer League year maps to the immediately preceding NBA regular-season label/key. |
| Venue scopes never combine | unit | `test_context_scope_has_one_competition_not_all_venues` | The lookup receives/resolves one competition ID only. |
| Rate metrics use aggregate formulas | unit | `test_context_metrics_recompute_from_totals` | At-rim FG%, 3P%, 3PA share, assisted-FG rate, TOV%, ORtg, and pace match documented numerator/denominator formulas. |
| Rates are not simple averages | unit | `test_context_weighted_total_differs_from_team_mean` | Deliberately uneven team attempts make the aggregate result differ from an unweighted team-rate mean. |
| Pace matches the project convention | unit | `test_context_pace_is_opponent_adjusted_per_48` | Opponent-adjusted possession estimate is normalized to 48 minutes, including 40-minute SL games. |
| Difference formatting is semantically correct | unit | `test_context_difference_formats_pp_vs_numeric` | Percentages render signed percentage-point differences; pace/ratings render signed numeric differences. |
| Coverage gate blocks incomplete inputs | unit | `test_context_coverage_gate_rejects_missing_boxes_or_zones` | Missing team box or required zone coverage returns unavailable metrics/reason, never zero-filled output. |
| Projection rebuild is deterministic/versioned | integration | `test_context_rebuild_persists_provenance_and_replaces_projection` | Same snapshots rebuild identically; changed input yields updated derived row/version while raw facts are unchanged. |
| Valid Explorer Context view renders | integration | `test_context_route_renders_pinned_comparison` | HTTP 200, active Context tab, correct event/NBA labels, all seven metrics, source affordance. |
| Invalid/broad scopes are safe | integration | `test_context_route_renders_scope_guidance_for_invalid_or_broad_query` | HTTP 200 with actionable guidance and no comparison table; invalid params never 500. |
| Teams preview agrees with Context | integration | `test_teams_preview_links_to_matching_context` | Pinned Teams HTML has preview/link and destination values/scope agree; broad query has no preview. |
| Context CSV is complete | integration | `test_context_csv_has_metrics_metadata_and_matches_html` | CSV includes scopes, values, differences, formula/calculation version, coverage, and source refs; values equal HTML. |
| URL round-trip is server-rendered | integration | `test_context_query_round_trip_full_and_partial` | Full and `partial=1` responses preserve scope and content without JS. |
| Published baseline is reproducible | integration | `test_2025_las_vegas_baseline_regression` | Seeded fixture computes 62.8/66.4 rim FG%, 31.3/36.0 3P%, 43.3/42.1 3PA share, 60.6/63.7 assisted-FG rate, 16.7/12.6 TOV%, 105.2/98.8 pace, 103.2/114.6 ORtg, within rounding tolerance. |
| No live source call in request path | integration | `test_context_route_reads_projection_without_external_client` | Patch/spy NBA client; route uses persisted projection and makes zero external calls. |
| Route query count stays in budget | perf integration | `make perf` with pinned Context + Teams preview | Query count is within a documented budget; no N+1 increase. |
| New reads are indexed | explain integration | `make explain ROUTE='<pinned context URL>'` | Competition/benchmark projection lookup is index-backed; migration adds any missing index. |

## Fixture design

Use a minimal, deterministic dataset rather than a full raw archive:

1. One 2025 Las Vegas `SummerLeagueCompetition` with two or more completed games,
   team-game logs, and shot-zone events. Include uneven attempt volumes so weighted-
   total tests catch accidental `AVG()` logic.
2. A paired persisted NBA 2024–25 benchmark source snapshot/projection with known totals
   and coverage metadata.
3. One incomplete Summer League competition: either one team box missing or zone coverage
   incomplete. It should remain selectable but show the unavailable state.
4. A second venue in 2025 to prove the pinned Las Vegas query cannot include its rows.
5. A second NBA source/version for rebuild/versioning tests.

The regression fixture may store compact aggregate totals instead of every historical game,
but it must preserve the source/coverage fields used by the real build.

## Required post-build QA

- Run `make dev`, then open a valid URL such as
  `/stats/summer-league/explorer?subject=context&year_min=2025&year_max=2025&venue=las_vegas`.
  Confirm the active tab, scope label, metric table, definitions, sources, and CSV link.
- Open Context with no venue and with a multi-year range; confirm the useful pinned-scope
  instruction rather than a blended comparison.
- Open pinned Teams; verify the preview links to the same Context values. Open broad
  Teams; verify the preview is absent.
- Copy the valid Context URL into a fresh browser context and reload with JS disabled;
  it must render identically.
- Run `conda run -n draftguru make visual` and inspect desktop/mobile captures named in
  the QA checklist.
- Run `conda run -n draftguru make perf` and
  `conda run -n draftguru make explain ROUTE='<pinned context URL>'` on a prod-like DB.

## Completion bar

The implementation is test-complete when all unit and integration rows above pass, the
route reads only materialized/context data at request time, coverage failures are visible
and safe, performance/index evidence is captured, and browser QA verifies valid, empty,
Teams-preview, export, and mobile states.
