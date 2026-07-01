# Summer League Roster Reconcile, QA & Enrichment — Test Plan

**Sources:**
- Tech spec: `docs/plans/summer-league-roster-reconcile-enrichment-spec.md`

**Sibling artifact:** QA checklist at `summer-league-roster-reconcile-enrichment-qa-checklist.md`

## Purpose

Ties tests to product risk: the reconcile is a data-quality signal (wrong flags erode trust in the roster data), the affiliation heal touches the append-only assertion contract (a bug there corrupts identity history), and the enrichment-targeting code gates how much of the app's player data gets populated for the July 2026 event. All risks are backend/data — verified via DB assertions and CLI output, not the browser.

## Conventions (from `docs/plans/ai-orchestrator-ticket-spec.md`)

- Conda env `draftguru`; tests via `conda run -n draftguru --no-capture-output python -m pytest <path>`.
- Integration tests need `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`; wrap with `scripts/with-db-env.sh` or export `.env`. New schema modules must be imported in `tests/integration/conftest.py`.
- Run integration with `GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=` to dodge the embedding-listener flakiness.
- No `no_deps`/`with_deps` split; single integration tier. Backend-only — no e2e/visual.
- Type: `mypy app --ignore-missing-imports`; patch coverage: `make coverage.diff` (≥80%).

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| Cohort selector returns resolved SL players by year/league; excludes unresolved + out-of-scope | integration | `tests/integration/test_summer_league_cohort.py` | T0 |
| Reconcile: announced-not-played, played-not-announced, and announced∧played classified correctly | integration | `tests/integration/test_roster_reconcile.py` | T1 |
| Reconcile joins on `(competition_id, source_player_id)` — NULL `participation_id` logs still count as played | integration | `tests/integration/test_roster_reconcile.py` | T1 |
| Reconcile is read-only (no row mutations across two runs) | integration | `tests/integration/test_roster_reconcile.py` | T1 |
| Empty / unresolved-only competition → zero report, no crash | integration | `tests/integration/test_roster_reconcile.py` | T1 |
| Reconcile CLI prints report + exit 0 (arg parse; report formatting) | unit + integration | `tests/unit/test_roster_reconcile_cli.py` (parse) + live smoke | T2 |
| Loader heal: box-score-first participation gains ANNOUNCED assertion; box_score assertion retained + chained | integration | `tests/integration/test_roster_loader.py` (extend) | T3 |
| Loader heal is idempotent across re-loads | integration | `tests/integration/test_roster_loader.py` | T3 |
| Normal announced player unaffected by heal branch | integration | `tests/integration/test_roster_loader.py` | T3 |
| QA harness surfaces reconcile counts as accepted warnings (non-blocking) | integration | `tests/integration/test_summer_league_qa_service.py` (extend) | T4 |
| Fetcher reported count == unique snapshot entries (duplicate-person fixture) | unit | `tests/unit/test_roster_scraper.py` (extend) | T5 |
| Bio enrichment SL-cohort selection restricts target set; no-bbref → manual-review list | integration | `tests/integration/test_bio_enrichment_cohort.py` | T6 |
| College-stats SL-cohort selection restricts target set; no-source enumerated | integration | `tests/integration/test_college_enrichment_cohort.py` | T7 |
| Image-gen SL-cohort selection builds batch over cohort (vision call stubbed, no spend) | integration | `tests/integration/test_image_gen_cohort.py` | T8 |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Reconcile produces sensible flags on real 2025 CA Classic + SLC data | CLI smoke: `scripts/with-db-env.sh conda run -n draftguru python scripts/reconcile_summer_league_rosters.py --year 2025 --league-id 13` (and 16) | CLI stdout with plausible DNP / late-add lists |
| Full suite + mypy + coverage green | `pytest tests/unit tests/integration` + `mypy app` + `make coverage.diff` | QA-gate ticket report |
| Test-effectiveness audit + spec compliance | `test-quality-auditor` + `/review` on the diff | audit verdict |

## Ticket Injection Notes

- **Ticket T0 — SL cohort selector**
  - Required tests: selector returns resolved `player_id`s with a participation in a year/league scope; excludes `player_id IS NULL`; excludes other scopes.
  - DB assertions: seeded participations across two competitions resolve to the expected cohort set.

- **Ticket T1 — Reconcile service**
  - Required tests: three-way classification (announced-not-played / played-not-announced / both); NULL-`participation_id` logs count as played; read-only (no mutations); empty + unresolved-only edge cases.
  - DB assertions: report lists carry name + team; totals sum correctly.

- **Ticket T2 — Reconcile CLI**
  - Required tests: arg parse (year/league/all-venues); live smoke against loaded 2025 data prints report, exit 0.

- **Ticket T3 — Affiliation heal in loader**
  - Required tests: box-score-first participation → ANNOUNCED roster assertion appended on load; prior box_score assertion retained with `superseded_at` set and `supersedes_id` chained; idempotent on re-load; normal announced flow unchanged.
  - DB assertions: `player_affiliations` chain for the player.

- **Ticket T4 — Reconcile into QA harness**
  - Required tests: reconcile counts reported as warnings; harness exit code unaffected by reconcile warnings alone.

- **Ticket T5 — Fetch count fix**
  - Required tests: fixture with a person under two team subpages → fetcher summary `players=N` equals unique snapshot entries.

- **Ticket T6 — Bio enrichment cohort targeting**
  - Required tests: cohort selection restricts target set to bbref-having cohort players; no-bbref players → manual-review list, not error.

- **Ticket T7 — College-stats cohort targeting**
  - Required tests: cohort selection restricts target set; non-NCAA/international → no-source list.

- **Ticket T8 — Image-gen cohort targeting**
  - Required tests: batch built over cohort players missing a stylized image; vision/generation stubbed; no live spend.

- **Ticket T9 — QA gate**
  - Required: full suite, mypy, ≥80% patch coverage, test-effectiveness audit, spec-compliance review, reconcile-CLI live smoke on 2025 data.
