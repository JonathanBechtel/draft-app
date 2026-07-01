# Spec: Summer League Roster Reconcile, QA & Enrichment (Workstream B2/B3 + C)

## Context

The 2026 Summer League roster foundation (Project #11, PR #449) shipped the
roster→participation→affiliation→resolution pipeline plus the A4 announced-roster
surface, and PR #450 fixed the loader's schema registration. We have now loaded
**real 2025 California Classic (53) + Salt Lake City (75) rosters** into dev
end-to-end, which unblocks the remaining data-dependent workstreams.

This spec covers the code-shaped remaining work: **B2 reconcile**, **B3 QA
integration**, a small loader **affiliation-heal** fix, a **fetch-count** bug,
and making the **bio/college/image enrichment** paths targetable to the Summer
League rostered cohort. The actual enrichment *runs* (executing the scripts,
Gemini image spend) are operational and out of scope for the code tickets.

Reference: `docs/plans/summer-league-2026-full-roster.md` (Workstreams B and C)
and `docs/plans/summer-league-2026-workstream0-schema.md` (the participation +
affiliation primitives these build on).

## Goal

Close the announced↔played loop with a reconcile check surfaced in QA, complete
the affiliation stream for box-score-first players, correct the fetch player
count, and let each enrichment script target exactly the SL rostered cohort — so
the full roster + enrichment pipeline is production-ready for the July 2026 event
and validated now against 2025 data.

## Out of scope

- Executing enrichment runs (bio/college/image generation) — operational, done
  separately; these tickets only add the *targeting* code.
- Live 2026 game-log ingest during the event (proven historically; not code).
- D (prod replication / scheduling / monitoring) — a separate later effort. The
  daily 2026 satellite poll already exists (`scripts/poll_2026_satellite_rosters.sh`).

## Data model recap (already shipped)

- `summer_league_participation` — stable bridge `(competition, team_entry,
  source_player, stint)`; `player_id` (canonical, nullable), `affiliation_id`,
  `roster_status`.
- `player_affiliations` — append-only assertions. Roster-sourced rows carry
  `source='nba_summer_league_roster'`; box-score-discovered rows carry
  `source='nba_summer_league_box_score'` (status `CONFIRMED`).
- `summer_league_player_game_logs` — box-score lines; `participation_id` is a
  soft reference (NULL on pre-B1 rows, so joins must key on
  `(competition_id, source_player_id)`, not `participation_id`).

## Workstream / ticket breakdown

### T0 — Shared SL-cohort selector (foundational)

A reusable read helper that returns the set of canonical `player_id`s (and/or
`source_player_id`s) that have a Summer League **participation**, filterable by
year / league_id / venue. Lives in a read service (e.g.
`app/services/summer_league/cohort.py`). Every enrichment ticket (T5–T7) targets
this cohort instead of scanning all players.

- **Acceptance:** given a year/league filter, returns the resolved `player_id`s
  with a participation in that scope; excludes unresolved (null player_id);
  covered by an integration test on seeded participations.
- **Deps:** none (root).

### T1 — B2 reconcile service

`app/services/summer_league/roster_reconcile.py` with
`reconcile_competition(db, competition_id) -> RosterReconcileReport`. Computes:

- `announced_not_played` — participations whose affiliation source is
  `nba_summer_league_roster` and whose `source_player_id` has **no** game-log row
  in the competition (DNP / cut / never suited up).
- `played_not_announced` — `source_player_id`s with a game-log row in the
  competition that have **no** roster-sourced participation (box-score late-adds).
- Totals: announced, played, announced∧played.

Set arithmetic over two queries (announced participations; distinct game-log
source players); each entry carries name (`display_name` or `raw_player_name`)
and team. Join game logs on `(competition_id, source_player_id)` (NOT
`participation_id`, which is NULL on pre-B1 rows).

- **Acceptance:** on a seeded competition with an announced-but-DNP player, a
  played-and-announced player, and a played-but-unannounced player, the report
  classifies all three correctly; idempotent (read-only).
- **Deps:** none (root, parallel to T0).

### T2 — B2 reconcile CLI

`scripts/reconcile_summer_league_rosters.py --year --league-id [--all-venues]`.
Calls `reconcile_competition` per competition and prints a readable report
(counts + the two flagged lists). Read-only (no `--dry-run` needed). Follows the
existing script conventions (`load_schema_modules()`, `_prepare_asyncpg_connection`,
`with-db-env.sh`).

- **Acceptance:** runs against dev and prints the two flagged lists + totals for
  a given year/venue; exit 0.
- **Deps:** T1.

### T3 — Heal box-score-first affiliation in the loader

In `roster_ingest.load_roster_snapshot`'s **"unchanged"** branch: when a player
already has a participation whose current affiliation is **box-score-sourced**
(`nba_summer_league_box_score`, i.e. discovered from a game before being
announced), append an `ANNOUNCED` `nba_summer_league_roster` assertion
(superseding, per the append-only contract) so the announced-roster history is
complete. Does not change the `roster_status` promotion semantics elsewhere.

- **Acceptance:** a participation created box-score-first (CONFIRMED, box_score
  source) that then appears in a roster snapshot gains an ANNOUNCED roster
  assertion on load; the box_score assertion is retained (append-only);
  idempotent across re-loads.
- **Deps:** none (root, independent).

### T4 — B3: reconcile findings into the QA harness

Surface T1's reconcile results as checks in the SL QA harness
(`scripts/qa_summer_league_backbone.py` / its service). `played_not_announced`
and `announced_not_played` are **accepted-warning** classes (informational, not
blocking) with counts per competition, consistent with the runbook's
accepted-warning taxonomy.

- **Acceptance:** the QA harness reports reconcile counts per sliced competition
  as warnings (not blocking findings); existing QA behavior unchanged otherwise.
- **Deps:** T1.

### T5 — Fetch player-count fix

The roster fetcher's summary line reported `players=67` for 2025 California
Classic while the persisted snapshot / load was `53`. Reconcile the count so the
fetcher's reported player total matches the deduplicated set it writes to the
snapshot (root cause likely players appearing under multiple team subpages, or a
summary tally that double-counts). Add a regression test on a fixture where a
person appears twice.

- **Acceptance:** fetcher's reported `players=N` equals the number of unique
  entries in the written snapshot; covered by a unit test.
- **Deps:** none (root, independent).

### T6 — C3: bio enrichment SL-cohort targeting

Add SL-cohort selection to the bio enrichment path (`bbref_bio_scraper.py` /
`ingest_player_bios.py`) so it can run over just the SL rostered cohort (T0) that
has a `bbref` external id, instead of all players. Internationals without a BBRef
page fall to a manual-review list (flagged, not failed).

- **Acceptance:** a `--summer-league-year/--league-id` (or equivalent) selection
  restricts the bio target set to the SL cohort; players without a bbref id are
  reported, not errored; unit/integration coverage on the selector.
- **Deps:** T0.

### T7 — C4: college-stats enrichment SL-cohort targeting

Add SL-cohort selection to `scrape_college_stats.py` (`--only-missing`) so it
targets resolved SL players with `school` + a bbref id. Non-NCAA / international
players report "no source" (flagged, not failed).

- **Acceptance:** SL-cohort selection restricts the college-stats target set;
  no-source players are enumerated; covered by tests.
- **Deps:** T0.

### T8 — C2: image-generation SL-cohort targeting

Add SL-cohort selection to `generate_player_images.py` (`--batch`,
`--missing-only`) so image generation targets the SL cohort, consuming the C1
reference headshots already stamped on `PlayerMaster.reference_image_url`. Code
only — no batch spend in this ticket.

- **Acceptance:** SL-cohort selection restricts the image target set to cohort
  players missing a stylized image; the batch is *built* (submit path exercised
  in a test with the vision call stubbed), not spent.
- **Deps:** T0.

### T9 — QA gate

Full suite + `mypy app` + `make coverage.diff` (≥80% patch) + test-effectiveness
audit + spec-compliance review; live smoke of the reconcile CLI against the
loaded 2025 CA/SLC data. Backend-only feature (unit + integration; no e2e/visual).

- **Deps:** T0–T8.

## Suggested DAG

```
T0 (cohort) ─┬─ T6 (bio) ─┐
             ├─ T7 (college) ─┤
             └─ T8 (images) ──┤
T1 (reconcile svc) ─┬─ T2 (CLI) ─┤
                    └─ T4 (QA) ──┤
T3 (heal affiliation) ───────────┤
T5 (fetch count) ────────────────┴─ T9 (QA gate)
```

Roots (parallel): T0, T1, T3, T5. Then T2/T4 after T1; T6/T7/T8 after T0; T9 last.

## Verification

- Unit tests for pure selectors/classification; integration tests (Postgres) for
  the reconcile service, the loader heal, and the cohort selector, using seeded
  participations + game logs.
- Live validation: run the reconcile CLI against the already-loaded 2025 CA
  Classic + SLC data and confirm sensible flagged lists.
- Conda for all checks; `GEMINI_API_KEY=` for integration runs (embedding-listener
  flakiness). Disposable pgvector on localhost for integration.

## Definition of done

- Reconcile service + CLI ship and produce correct flags on 2025 data; findings
  surfaced as accepted-warning QA checks.
- Box-score-first participations gain an ANNOUNCED assertion on later roster load.
- Fetcher's reported player count matches the persisted snapshot.
- Each enrichment script can target the SL cohort; no-source players are
  enumerated, not silently dropped.
- Repo checks green (precommit, mypy, unit/integration, ≥80% patch coverage).
