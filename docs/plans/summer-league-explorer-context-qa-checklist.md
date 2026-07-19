# Summer League Explorer — League Context QA Checklist

> **Superseded for planning by** `docs/plans/event-environment-intelligence-pitch.md`.
> Retain this checklist as reusable QA input for the future optional NBA-comparison lens;
> do not use it as the implementation scope for the broader Event Environment work.

**Sources:**

- Product pitch: `docs/plans/summer-league-context-benchmarks-pitch.md`

This checklist defines the observable behavior required before the Summer League
Explorer’s **League Context** subject is complete. The Explorer is public; this
feature requires no login.

## Scope selection and discoverability

- A user can find **League Context** beside Players, Game Finder, Teams, and Matchups.
  - Verify: open `/stats/summer-league/explorer`.
  - Expected: the subject is a first-class Explorer tab, not a link to a separate
    season page; activating it preserves the Explorer’s query-string state.
  - Evidence: screenshot and URL.

- The Context view requires exactly one competition scope.
  - Verify: open the Context view with no year/venue; then with a year range; then
    with `year_min=2025&year_max=2025&venue=las_vegas`.
  - Expected: the first two cases show a useful instruction to choose one year and one
    venue; only the final case renders a comparison. The page never silently pools
    California Classic, Salt Lake City, and Las Vegas.
  - Evidence: each rendered state and URL.

- The selected event and comparison season are unambiguous.
  - Verify: load the pinned 2025 Las Vegas view.
  - Expected: heading identifies “2025 Las Vegas Summer League” and “2024–25 NBA
    regular season,” including the comparison rule in accessible text/tooltip.
  - Evidence: heading, source label, and tooltip text.

- The Teams view exposes the same context without duplicating or changing the
  comparison.
  - Verify: open the pinned Teams query for 2025 Las Vegas and select its Context
    preview/link.
  - Expected: the preview is present only when a valid context exists; its link opens
    the identical Context scope and values. Broad/multi-venue team queries show no
    fabricated summary.
  - Evidence: source URL and destination URL/value parity.

## Metrics and editorial integrity

- All seven v1 metrics render for a fully covered event: at-rim FG%, 3P%, 3PA share,
  assisted-FG rate, turnover rate, pace per 48, and offensive rating.
  - Verify: pinned 2025 Las Vegas view.
  - Expected: each row contains Summer League value, NBA value, and signed percentage-
    point or numeric difference; null/zero values never masquerade as a valid figure.
  - Evidence: screenshot and CSV header.

- Definitions use the correct group-level concepts.
  - Verify: inspect metric tooltips/footnotes.
  - Expected: “at rim” means NBA Stats Restricted Area; assisted-FG rate is `AST / FGM`
    and is not labeled player AST%; turnover rate is `TOV / (FGA + 0.44 × FTA + TOV)`;
    pace is normalized to 48 minutes.
  - Evidence: rendered definitions and formula tests.

- Metrics recompute from aggregate numerators and denominators, not an unweighted mean
  of team percentages.
  - Verify: independently sum the scoped team-game/zone inputs and compare to the
    persisted projection for a fully covered competition.
  - Expected: FG%, 3PA share, assisted-FG rate, turnover rate, and offensive rating
    equal the documented formula from totals within display rounding tolerance.
  - Evidence: DB/query calculation fixture and integration-test output.

- The initial published comparison is reproducible.
  - Verify: recompute the 2025 Las Vegas / 2024–25 NBA pair from its stored source
    snapshots.
  - Expected: values display as 62.8% vs. 66.4% at the rim, 31.3% vs. 36.0% from three,
    43.3% vs. 42.1% three-attempt share, 60.6% vs. 63.7% assisted-FG rate, 16.7% vs.
    12.6% turnover rate, 105.2 vs. 98.8 pace, and 103.2 vs. 114.6 offensive rating
    (normal rounding tolerance applies).
  - Evidence: snapshot-to-projection calculation report.

- The UI describes observations, not unsupported player-translation or causal claims.
  - Verify: inspect visible copy, footnotes, share text, and CSV metadata.
  - Expected: wording says the event was faster/less efficient where supported; it does
    not claim a player will translate by a fixed factor or attribute aggregate changes
    to a single cause.
  - Evidence: screenshot and exported metadata.

## Provenance, coverage, and lifecycle

- Every displayed comparison has source and calculation provenance.
  - Verify: inspect the Context view and its backing projection.
  - Expected: user-visible source links/name plus stored source snapshot identifiers,
    retrieval time, coverage counts, and calculation version.
  - Evidence: rendered source affordance and DB assertion.

- Partial source coverage blocks the comparison rather than producing a plausible-
  looking number.
  - Verify: seed/select a competition with missing team boxes, missing zone data, or a
    coverage flag below the required threshold.
  - Expected: Context reports what is missing and hides affected values/comparison;
    there is no stale fallback or denominator-zero value.
  - Evidence: negative-case integration test and screenshot.

- A corrected source snapshot can safely replace the read model.
  - Verify: run the rebuild after changing a fixture/source snapshot.
  - Expected: the derived context projection changes deterministically, receives a new
    calculation/source version, and does not mutate raw Summer League facts.
  - Evidence: before/after projection rows and raw-row parity.

- The data follows the journey-graph assertion/projection boundary.
  - Verify: inspect the schema/service flow.
  - Expected: raw NBA and Summer League inputs retain provenance; the competition-to-
    benchmark comparison is a rebuildable projection, not a parallel player store or a
    hand-edited page payload.
  - Evidence: migration/schema review and rebuild test.

## Sharing and export

- A pinned Context URL round-trips exactly.
  - Verify: copy a valid Context URL into a fresh browser session, then reload with JS
    disabled.
  - Expected: same scope, values, definitions, and source labels render server-side.
  - Evidence: cold-load and JS-disabled screenshots.

- CSV export is analysis-ready.
  - Verify: export the pinned Context view.
  - Expected: CSV carries event/comparator identifiers, all metric values, difference,
    formula/calculation version, coverage, and source references; values match the UI.
  - Evidence: downloaded CSV header and a row-by-row spot check.

- Invalid Context parameters never 500.
  - Verify: use a bogus subject, invalid year, unknown venue, multi-year range, and a
    valid year/venue with no benchmark.
  - Expected: safe fallback or clear empty state, HTTP 200/404 according to existing
    route conventions, and no SQL/traceback leakage.
  - Evidence: response status and rendered state.

## Performance and visual QA

- The Explorer request remains within its query budget.
  - Verify: run `conda run -n draftguru make perf` for the base Explorer, pinned Context
    view, and pinned Teams preview.
  - Expected: no N+1 behavior; any intentional budget change is documented in
    `tests/integration/perf/budgets.py`.
  - Evidence: perf output.

- New reads are indexed and cheap on a production-like database.
  - Verify: run `conda run -n draftguru make explain ROUTE='<pinned context URL>'` for
    the context projection and Teams preview queries.
  - Expected: index-backed lookup by competition/benchmark scope; no sequential scan of
    an unbounded snapshot/projection table.
  - Evidence: saved EXPLAIN output; add index and Alembic migration if absent.

- The feature is readable at desktop and mobile widths.
  - Verify: run `make dev`, then `conda run -n draftguru make visual`; inspect Context
    valid, Context empty, and Teams-preview states at the harness’s desktop/mobile
    viewports.
  - Expected: metric names, two comparison values, and differences remain aligned;
    footnotes and source links are reachable; no horizontal clipping or duplicate
    scrollbar.
  - Evidence: save `sl-explorer-context-valid.png`, `sl-explorer-context-empty.png`,
    `sl-explorer-context-mobile.png`, and `sl-explorer-teams-preview.png` under
    `tests/visual/screenshots/`.

## Completion bar

The feature is ready when QA can show that:

1. League Context is a shareable Explorer subject that only compares a valid single
   competition to its explicit NBA-season comparator.
2. Every metric is computed from the documented totals, is reproducible from versioned
   source snapshots, and is labeled with an unambiguous group-level definition.
3. Incomplete source coverage produces an honest unavailable state, never a fabricated
   league average.
4. Context and Teams-preview URLs/CSV exports round-trip correctly without client-side
   dependence.
5. The new projection reads meet Explorer query-budget, indexing, and desktop/mobile
   visual requirements.
