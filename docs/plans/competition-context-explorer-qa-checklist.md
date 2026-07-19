# Competition Context Explorer — QA Checklist

**Status:** Ready for implementation planning · **Date:** 2026-07-18

**Sources:**

- First-release outline: `docs/plans/competition-context-explorer-first-release.md`
- Product direction: `docs/plans/event-environment-intelligence-pitch.md`
- Comparison policy: `docs/plans/event-environment-comparison-and-outcomes-framework.md`

This checklist defines observable completion criteria for the first release of the
Summer League Explorer’s fifth **Competitions** tab. Explorer is public; no account or
admin action is required.

## Navigation and scope

- A visitor can find **Competitions** beside Players, Game Finder, Teams, and Matchups.
  - Verify: open `/stats/summer-league/explorer` on desktop and mobile.
  - Expected: five Explorer tabs are visible, the selected tab is unambiguous to screen
    readers and visually, and the new view remains within the Explorer route.
  - Evidence: desktop/mobile screenshot and rendered navigation semantics.

- The tab has two explicit, shareable row grains.
  - Verify: activate **Summer League seasons**, then **Individual competitions**.
  - Expected: the first renders one row per calendar year across all included Summer
    League competitions; the second renders one row per competition edition (for example
    2025 Las Vegas). The URL preserves the selected view.
  - Evidence: row-grain assertion, screenshots, and URL round trip.

- A season row is not mislabeled as one venue/event.
  - Verify: inspect a season with more than one included competition.
  - Expected: its name clearly says Summer League/all competitions; the component
    competition count and venues are visible or reachable. It does not use a Las Vegas
    heading or silently omit California Classic/Salt Lake City.
  - Evidence: rendered row/detail and underlying membership assertion.

- An individual-competition row has a stable, unambiguous identity.
  - Verify: compare same-year entries for different venues and a repeated venue across
    years.
  - Expected: year, competition/venue, and competition identifier resolve to one exact
    source scope; no row aggregates different venues by accident.
  - Evidence: source IDs in test fixture/projection and visible labels.

## Scope controls and filtering

- Year ranges work in both views.
  - Verify: select 2020–2025 in season view, then the same range in individual-
    competition view.
  - Expected: all and only in-range rows render; the selected trend metric has one point
    per eligible year in season view and no out-of-range points.
  - Evidence: row/year assertions, chart data assertion, and URL.

- Venue/competition filtering is available only where meaningful.
  - Verify: inspect controls in both views; set Las Vegas in individual-competition
    view; switch back to season view.
  - Expected: venue filter is available and effective for individual competitions. It is
    hidden or explicitly disabled/cleared for all-competitions season scope—never applied
    invisibly to make a partial season look complete.
  - Evidence: control state, result rows, and canonicalized URL.

- Minimum completed-games filtering protects against early-event noise.
  - Verify: set a threshold above and below a known competition’s final-game count.
  - Expected: the row is included only at/below its completed-game count; scheduled,
    postponed, and in-progress games do not satisfy the threshold.
  - Evidence: fixture calculation and rendered result count.

- Coverage filtering is metric-aware.
  - Verify: filter to box complete, shot-chart complete, and PBP complete scopes using
    fixtures/seeded records with each coverage state.
  - Expected: each control returns only profiles meeting that requirement. A box-complete
    row with missing shot coverage remains available in the box view but has null shot
    metrics, not zeros.
  - Evidence: integration test and rendered coverage badges.

- Metric threshold filters compose with scope filters.
  - Verify: apply year range + coverage + `3PA share ≥ X` + `turnover rate ≤ Y`.
  - Expected: each row satisfies every active predicate; invalid values/metric keys fail
    safely with a validation message or ignored invalid predicate, never a 500.
  - Evidence: result spot check, response status, URL.

## Profile content and definitions

- Each row/detail displays the applicable identity and confidence facts.
  - Verify: inspect one complete individual competition and one multi-competition season.
  - Expected: dates, team count, completed-game count, included competition count where
    applicable, and box/shot/PBP coverage are present and correctly labeled.
  - Evidence: screenshot plus projection-to-source parity assertion.

- The first-release environment metric set is available whenever inputs support it.
  - Verify: load a fully covered profile.
  - Expected: points per team game, possessions/pace per 48, offensive rating, 3PA share,
    3P%, free-throw rate, offensive-rebound rate, turnover rate, assisted-FG rate,
    score-margin/OT distribution, rim share, and rim FG% render with units.
  - Evidence: screenshot, column catalog assertion, and CSV/header assertion if export
    ships in the same release.

- The reader-requested terms have accurate group-level definitions.
  - Verify: inspect labels/tooltips/footnotes.
  - Expected: rim metrics name their zone/source policy; turnover rate exposes its
    documented possession denominator; pace is normalized to 48 minutes; “assisted
    field-goal rate” is `AST / FGM` and is not presented as player AST%.
  - Evidence: rendered definition text and formula unit tests.

- Field-composition facts are useful and honest.
  - Verify: inspect a profile with drafted, undrafted, rookie, and returner players.
  - Expected: distinct players, rookie/returner, drafted/undrafted, draft-slot bands,
    age/position/origin distributions, and team representation render only where source
    data supports them. Missing identity attributes are labeled as unknown/coverage gaps,
    not silently dropped from a denominator.
  - Evidence: seeded integration fixture and visible breakdown.

- Performance-landscape content remains descriptive.
  - Verify: inspect leader/concentration/spread modules and their links.
  - Expected: leader links carry the same selected scope into existing Players Explorer
    results. Copy does not treat tournament record or a small-sample scoring lead as a
    player-development conclusion.
  - Evidence: destination URL parity and screenshot/copy review.

## Aggregation and data integrity

- All-competitions season rates are recomputed from underlying totals.
  - Verify: seed or select a year with at least two competitions whose team-level rates
    differ, then independently sum numerator/denominator inputs.
  - Expected: pace, offensive rating, 3PA share, shooting percentages, free-throw and
    rebound rates, turnover rate, and assisted-FG rate equal the documented calculation
    from season-wide totals within display tolerance. They are not an unweighted mean of
    competition values.
  - Evidence: unit/service test with deliberately unequal denominators.

- Individual-competition calculations are isolated.
  - Verify: calculate a metric for one competition in a year containing several venues.
  - Expected: only games/team logs/shot events linked to that competition contribute;
    season-wide values never leak into the detail row.
  - Evidence: integration test with contrasting venues.

- Season field-composition player counts deduplicate canonical identities.
  - Verify: seed a player who appears in two competitions in the same year.
  - Expected: the player counts once in distinct-player, draft-status, age, and position
    distributions; participation/player-game counts retain their own clearly labeled
    totals; repeat participation is disclosed where promised.
  - Evidence: integration fixture and exact count assertions.

- Final-game policy is consistent across every metric.
  - Verify: include scheduled, in-progress, postponed, and final records in one scope.
  - Expected: completed-game totals/rates use the documented final status policy;
    schedule metadata remains visible without contaminating play-style metrics.
  - Evidence: integration test and detail labels.

- Partial coverage produces an honest metric-level unavailable state.
  - Verify: seed missing shot or PBP inputs while retaining complete boxes.
  - Expected: box-derived metrics continue to render; affected shot/PBP metrics render
    em dash/unavailable plus coverage explanation. No denominator-zero, stale fallback,
    or fabricated 0.0% is shown.
  - Evidence: negative-case integration test and screenshot.

- Profile rebuilds are deterministic and preserve raw facts.
  - Verify: rebuild after a corrected source input or a calculation-version change.
  - Expected: the derived profile changes predictably with refreshed provenance/version;
    raw game, player, team, and shot records remain unchanged.
  - Evidence: before/after projection assertion and raw-fact checksum/row check.

## Trend, sharing, and cross-surface behavior

- The trend chart uses the exact filtered season scope.
  - Verify: choose a metric and years 2020–2025; apply minimum-game/coverage filters.
  - Expected: chart points equal table values for the same surviving years, use a visible
    unit, and show gaps rather than interpolating unsupported years.
  - Evidence: chart-data vs. table assertion and visual capture.

- Selected scope carries into existing Explorer subjects.
  - Verify: open a season profile and navigate to Players, Teams, and Matchups; repeat
    from an individual competition.
  - Expected: target views receive the exact all-competitions-year or competition scope,
    respectively, and their context strip/profile link returns to the same scope.
  - Evidence: source/destination URL and query-result parity.

- URLs round trip without client-side dependence.
  - Verify: copy a filtered/sorted seasonal profile URL and a competition detail URL into
    a fresh session; reload with JavaScript disabled.
  - Expected: the same view, filters, detail identity, table, and no-data/coverage state
    render server-side.
  - Evidence: cold-load and JS-disabled screenshots.

- The tab feeds reusable public surfaces without divergent calculations.
  - Verify: compare a profile’s selected facts with the corresponding venue/tournament
    or season-hub module once those consumers are included in scope.
  - Expected: identical values, definitions, coverage badges, and profile version; no
    separate hard-coded calculation exists in a consumer.
  - Evidence: shared-projection service test and cross-surface screenshot.

## Accessibility, visual quality, and resilience

- The tab, controls, table, and trend are keyboard and screen-reader usable.
  - Verify: tab through controls, change native selects, submit filters, and inspect
    focus/accessible names.
  - Expected: no keyboard trap; active tab/state and chart metric are announced; visual
    encoding is not the only carrier of coverage or trend meaning.
  - Evidence: browser accessibility/keyboard pass and screenshot.

- Mobile layout remains usable.
  - Verify: inspect both grains, active filters, a detail profile, an unavailable metric,
    and a 2020–2025 trend at the mobile harness viewport.
  - Expected: tabs and filters remain reachable, table has a clear responsive treatment,
    metric definitions/coverage notes are readable, and chart/table content is not clipped.
  - Evidence: saved visual captures.

- Empty, sparse, and invalid states are safe and explanatory.
  - Verify: use an unknown scope, an unsupported historical year, a valid filter yielding
    zero rows, and invalid query parameters.
  - Expected: clear empty/validation state with no stack trace or SQL error; route follows
    existing 200/404 conventions and does not silently broaden the requested scope.
  - Evidence: response tests and screenshots.

## Performance and release verification

- The new tab meets the Explorer query budget.
  - Verify: run `conda run -n draftguru make perf` for the default season list, filtered
    season trend, individual-competition list, and one detail/profile route.
  - Expected: no N+1 query pattern; any intentional budget update is documented in
    `tests/integration/perf/budgets.py`.
  - Evidence: perf output.

- New reads are indexed on a production-like database.
  - Verify: run `conda run -n draftguru make explain ROUTE=<each new profile route>`
    against the configured production-like database.
  - Expected: indexed scope/membership/projection lookups; no sequential scan over large
    raw snapshot/profile tables. Add the index and Alembic migration if required.
  - Evidence: saved EXPLAIN output and migration review.

- Required automated coverage exists.
  - Verify: run focused unit tests for formulas/scope normalization, integration tests for
    profile query/filter/aggregation/link behavior, and browser/visual checks for both
    grains; then run the repository Definition-of-Done commands.
  - Expected: all checks pass, including `conda run -n draftguru make precommit`, `conda
    run -n draftguru mypy app --ignore-missing-imports`, relevant pytest suites,
    `conda run -n draftguru make coverage.diff`, and visual/performance checks required
    by the change.
  - Evidence: command output and saved screenshots under `tests/visual/screenshots/`.

## Completion bar

The first release is complete when QA can demonstrate that:

1. Competitions is a first-class Explorer tab with explicit, shareable all-competitions
   Summer League season and individual-competition views.
2. All-competitions rates pool raw totals correctly, individual competition scopes remain
   isolated, and player field composition uses defensible distinct-identity rules.
3. Reader-requested shooting, turnover, and assist context is present with accurate
   definitions, denominators, coverage, and no misleading NBA/player-translation claim.
4. Filters, trend charts, details, and links preserve the selected scope across Explorer
   and degrade honestly for sparse/partial data.
5. The projection is provenance-bearing, rebuildable, indexed, accessible, responsive,
   and verified through the repository’s required automated and visual checks.
