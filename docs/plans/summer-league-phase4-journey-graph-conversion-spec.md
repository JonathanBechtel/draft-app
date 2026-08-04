# Summer League Phase 4 — Journey-Graph Conversion: Tech Spec

**Status:** Ready for QA-checklist + project generation.

**Reads with:** `summer-league-remediation-roadmap.md` (Phase 4 section — this spec is its detail),
`north-star-architecture.md` (P1–P3 govern every choice below),
`summer-league-journey-graph-alignment.md` (§3 the blocker, §4 service shape, §5a namespacing, §5b domain types),
`journey-graph-domain-vocabulary.md` (the type catalog), `global-player-journey-graph.md` (§7a org model, §13.3 `org_kind`).

**Roadmap alignment (journey-graph statement).** This phase *is* backbone work: it converts the
proven-but-namespaced Summer League structures into the shared hub the roadmap has been building
toward, and ships the one migration that gates spoke #2. It adds no parallel store and no new
product surface. Per the alignment doc's governing finding — **promote, don't rebuild** — nothing
here designs a generic equivalent from scratch; it lifts structures that have already survived
production contact. Spoke #2 itself, the canon-entity promotion that should be validated *by* it,
and the Player Development Ledger all remain deferred (§3).

---

## 1. Context and preconditions

**Verified against HEAD** (the north-star build practice "prove the schema supports the design
before ticketing" — these are measured facts, not assumptions):

| Fact | Evidence |
|---|---|
| `app/domain/` exists but is **empty** (`__init__.py` only) | `app/domain/` |
| Import contract 2 (`app.domain` must not import schemas/services/routes) is **installed and green** — vacuously | `pyproject.toml:231-235` |
| `app/services/stats/` shipped in Phase 2 (7 modules); contract 3 green | `app/services/stats/`, `pyproject.toml:240` |
| `app/services/sources/` exists but is **empty**; contract 5 (spoke independence) installed, vacuous at one spoke | `pyproject.toml:321-324` |
| `DatedVersionMixin` exists and is adopted by **exactly two tables** | `app/schemas/base.py:12`; `summer_league_metrics.py:93,153` |
| **No** `organizations` / `team_programs` / `organization_relationships` table exists anywhere | `app/schemas/` |
| `player_affiliations` can only target `nba_team_id`; `team_program_id` is a **reserved comment** | `app/schemas/player_affiliation.py:73` |
| `summer_league_team_entries` carries both `nba_team_id` (FK) and `nba_stats_team_id` (source string) | `app/schemas/summer_league.py:247-268` |
| `nba_teams` is a closed, correct franchise set — a safe first population | `app/schemas/nba_teams.py:8` |
| Service sprawl: **48** modules under `app/services/summer_league/` + **11** top-level `app/services/summer_league_*.py` | `app/services/` |

**Measured rename blast radius** (Python references; this measurement is what shapes the ticket
split in §6 — the alignment doc calls §5a "free," which is true of *migrations*, not of diff size):

| Class | References |
|---|---|
| `SummerLeagueCompetition` | 499 |
| `SummerLeaguePlayerSeason` | 327 |
| `SummerLeagueSourcePlayer` | 322 |
| `SummerLeagueRawFile` | 88 |
| `SummerLeagueRawRun` | 71 |

**Phase 3 handoff.** Phase 3 deferred "legacy snapshot stamps" (`metric_snapshots` /
`player_image_snapshots` lack `registry_version` / `calculation_version` / `as_of`) as its own
ticket. That work is **claimed by this phase's mixin-adoption item** (T5) rather than left ambient.

**Preconditions:** none outstanding. Phase 3 merged (PR #765) and its four follow-ups (#766–#769)
are closed. The org model is schema-additive and depends on nothing in Phases 1–3.

## 2. Goals

1. **A non-NBA source can assert an affiliation.** The `organization → team_program → team_entry`
   model ships, populated from the known NBA set, with affiliations and SL team entries retargeted
   additively. This is the single live blocker for spoke #2.
2. **The journey-graph vocabulary becomes real in code** — `app/domain/` populated starting with
   `temporal.py`, misnamed classes aligned to backbone terms, docstrings citing their sections.
3. **The service layer is layered by role**, not by feature: `stats/` `backbone/` `ingest/`
   `sources/<spoke>/` `event_desk/`, so a source is structurally an adapter (P3).
4. **Versioned tables inherit P2** instead of re-deriving it, closing Phase 3's deferred stamp gap.
5. **Doc and code stop drifting** — the backbone doc's "where each layer lives in code" table is
   accurate on merge.

## 3. Non-goals (named deferrals — do not build)

- **Wave C — canon-entity promotion.** Generalizing edition / game / provenance *out* of the
  `summer_league_` namespace. Deliberately scheduled alongside spoke #2 so two real cases define
  the shape; building it now is the N=1 trap the alignment doc §6 names explicitly.
- **Wave D — analytical promotion.** Environment profiles and cohort baselines to generic scope
  baselines.
- **Spoke #2 itself** (FIBA LiveStats adapter). This phase makes it small; it does not start it.
- **Structured provenance replacing `source` strings.** The alignment doc §4 wants
  `"nba_summer_league_roster"` to become `source_system` / `source_document` / `source_record`
  references. That depends on the provenance tables being promoted — Wave C. Deferred with it (D8).
- **`__tablename__`, column, public-URL, or template-directory renames.** Expensive migrations with
  no second consumer yet (alignment §6). Class names only.
- **Event Desk generalization** — one instance is not evidence (alignment §2 HOLD).
- **ORM polymorphic hierarchies across spokes** — mixins and protocols only (alignment §5b).
- **Player Development Ledger.**

## 4. Settled decisions

- **D1 — Scope is Waves A + B.** This phase delivers the alignment doc's Wave A (service shape,
  domain types, vocabulary alignment) and Wave B (the org-model migration). Waves C and D are §3
  non-goals. Rationale: Wave B is the only item that actually gates spoke #2; Waves C/D are
  explicitly better done *with* spoke #2 in flight.
- **D2 — Org model shape and population order.** `organization` (with the closed §13.3 `org_kind`
  enum: CLUB / FEDERATION / LEAGUE / SCHOOL / ACADEMY / NATIONAL_PROGRAM) → `team_program` →
  retarget `team_entry`. Typed `organization_relationship` rows (`OWNS`, `ACADEMY_OF`, `FEEDS`,
  `AFFILIATED_WITH`) ship with the tables but are populated only where a real relationship exists.
  Population order per alignment §3: create tables → populate from `nba_teams` (closed, correct,
  safe) → add `team_program_id` to `player_affiliations` → backfill → retarget SL `team_entries`.
- **D3 — Retargeting is additive and dual-target; no SL row is ever repointed.** `team_program_id`
  lands as a **nullable** column beside `nba_team_id`; both are retained through this phase. Reads
  prefer `team_program_id` and fall back to `nba_team_id`. Dropping `nba_team_id` is a Wave C
  decision requiring a second consumer, not part of this phase. The affiliation schema comment
  already commits to exactly this ("lands additively as a nullable column — SL rows are never
  repointed", `player_affiliation.py:70-72`); this decision honors it.
- **D4 — Class renames are free of migrations, not free of risk.** At 499 references,
  `SummerLeagueCompetition → SummerLeagueEdition` is a mechanical but repo-wide diff that will
  conflict with every branch in flight. Therefore: **one class per ticket, executed as a codemod,
  serialized (never in parallel with another rename or with the service move), with zero behavior
  change and a full test run per rename.** No compatibility aliases — an alias defeats the entire
  point of §5a, which is that the code and the doc use one word.
- **D5 — Domain types are adopted at the seams, not big-bang.** `temporal.py` first
  (`Watermark`, `VersionStamps`, `Scope`) — the vocabulary doc's own recommended order, and the
  types that encode P2/P4. Adopt in new and touched code paths only. `identity.py` / `spoke.py`
  describe what already exists and may follow; `canon.py` / `provenance.py` / `assertions.py`
  belong to Wave C, where they gain real second consumers.
- **D6 — Service reorganization is moves and import rewrites only.** No behavior change, no
  signature change, `git mv` to preserve history. It runs **after** the renames (D4) because both
  touch the same files repo-wide. **Import contracts 3 and 4 enumerate module names explicitly**
  (`pyproject.toml:244-263, 287-306`) — moving `app.services.summer_league_*` to
  `app.services.sources.summer_league.*` invalidates those lists, so the contract lists and
  `tests/unit/test_import_contract_coverage.py` must be updated in the *same* change. Contract 4's
  `unmatched_ignore_imports_alerting = "error"` means a stale baseline fails the build, which is
  the desired tripwire, not an obstacle.
- **D7 — Mixin adoption covers four tables, two of which need a migration.**
  `SummerLeagueEnvironmentProfile` and `SummerLeagueCohortBaseline` already carry the columns
  hand-rolled — adopting the mixin is a refactor with no DDL. `MetricSnapshot` and
  `PlayerImageSnapshot` carry `version` + `is_current` but lack `registry_version` /
  `calculation_version` / `as_of` — these need an additive migration (§1.7-compliant, idempotent
  per the `create_all` gotcha). This is Phase 3's deferred legacy-stamp ticket, now owned.
- **D8 — `source` strings stay strings this phase.** Converting SL-coded `source` values to
  structured provenance requires the promoted `source_document` / `source_record` tables (Wave C).
  Attempting it now would either build those tables speculatively or invent a third representation.
  Recorded here so the gap stays owned rather than ambient.

## 5. Design

### 5.1 Org / team-program model + affiliation retarget (the blocker)

New tables in `app/schemas/` — `organization`, `team_program`, `organization_relationship` — with
`org_kind` as a closed enum per §13.3. `team_program` carries `organization_id`, `name`, and
`level`; it is **what affiliations point at**, never the corporate/governing org (§7a is explicit).

Population: one row per `nba_teams` entry — an `organization` of kind `CLUB` plus its
`team_program` — via an idempotent operator script in `scripts/`, re-runnable and reporting counts.
Then `player_affiliations.team_program_id` and `summer_league_team_entries.team_program_id` land
nullable and are backfilled from the existing `nba_team_id` join.

**The exit proof is a test, not an inspection:** an integration test creates an organization of kind
`FEDERATION`, a `team_program` owned by it, and writes a `player_affiliation` targeting that
program with `nba_team_id` NULL — demonstrating that a non-NBA source can now assert an
affiliation. That single test is the phase's headline exit criterion made mechanical.

Query-shape note: affiliation reads gain a nullable join. Per the repo's page-perf rule, any
route whose query set changes runs `make perf`, and each new/changed query gets
`make explain ROUTE=...` against a prod-like branch with the index shipped in the same change.

### 5.2 Domain vocabulary (`app/domain/`)

`temporal.py` first: `Watermark` (`source_as_of`, `projection_built_at`, `projection_version`),
`VersionStamps` (`version`, `registry_version`, `calculation_version`), `Scope` (`scope_key`,
`scope_kind`). Frozen dataclasses, no ORM imports (contract 2 already enforces this and is green —
it starts failing the moment someone reaches for `app/schemas/`). Every type cites its backbone
section in its docstring.

Adoption in this phase is deliberately narrow: the trend/metrics read paths that already return a
watermark-shaped tuple return `Watermark` instead. Breadth is a Wave C concern.

### 5.3 Vocabulary alignment (§5a class renames)

`SummerLeagueCompetition → SummerLeagueEdition`, `SummerLeagueRawFile → SummerLeagueSourceDocument`,
`SummerLeagueRawRun → SummerLeagueIngestionRun`, `SummerLeagueSourcePlayer → SummerLeagueSourceRecord`,
`SummerLeaguePlayerSeason → SummerLeagueDerivedAgg`. `__tablename__` values are untouched, so
Alembic sees nothing (it compares table names and columns, not class names) — **each rename ticket
must include an `alembic revision --autogenerate` run producing an empty diff as its proof.**

Plus the two free conventions: docstring citation of the backbone section on every table
implementing a journey-graph concept, and the module namespacing handled by 5.4.

Already aligned, leave alone: `SummerLeagueTeamEntry`, `SummerLeagueGame`,
`SummerLeagueParticipation`, `SummerLeagueEnvironmentProfile`.

### 5.4 Service-layer reorganization (§4 shape)

Target: `stats/` (exists) · `backbone/` (identity resolution, affiliations, participation) ·
`ingest/` (pipeline orchestration, batching, locks, state) · `sources/summer_league/` (NBA Stats
client, endpoints, normalization, roster parsing) · `event_desk/` (unchanged, presentation).

The 11 top-level `app/services/summer_league_*.py` modules and the 48 under
`app/services/summer_league/` collapse into that layering. Read-side services that are neither
adapter nor backbone (`summer_league_explorer_service`, `_leaders_service`, …) stay where they are
this phase — moving them is presentation-layer churn with no contract benefit, and Phase 5 owns the
god-file decomposition that would actually change their shape.

Contract lists updated in the same change (D6). The `app/cli/` ↔ `scripts/` boundary and the
`.dockerignore` read rule are unaffected by module moves *within* `app/services/`, but
`make lint.entrypoints` runs in the QA gate regardless.

### 5.5 Mixin adoption (P2 as a type)

Per D7. `SummerLeagueEnvironmentProfile` is the original template the mixin was copied *from*, so
adopting it is a pure dedup. `MetricSnapshot` / `PlayerImageSnapshot` get the three missing columns
additively; existing rows take a documented sentinel rather than a fabricated version, and the
backfill decision is stated in the ticket rather than assumed.

## 6. Work breakdown (ticket-shaped)

| # | Ticket | Depends on | Verification | Key files |
|---|---|---|---|---|
| T1 | `app/domain/temporal.py` — `Watermark` / `VersionStamps` / `Scope`; adopt at the metrics/trend read seam | — | unit, integration | `app/domain/`, trend read service |
| T2 | Org model: `organization` + `team_program` + `organization_relationship` tables, `org_kind` enum, migration | — | unit, integration (migration round-trip) | `app/schemas/`, new alembic revision |
| T3 | Populate from `nba_teams` — idempotent operator script, reported counts, dev before prod | T2 | integration | `scripts/` |
| T4 | Affiliation retarget: nullable `team_program_id`, backfill, dual-read, **non-NBA affiliation test**, perf/explain | T3 | unit, integration, perf | `player_affiliation.py`, affiliation services, migration |
| T5 | `DatedVersionMixin` adoption on the 4 remaining versioned tables + legacy stamp migration (D7) | — | unit, integration (migration round-trip) | `summer_league_environment.py`, `summer_league_desk.py`, `metrics.py`, `image_snapshots.py`, migration |
| T6 | SL `team_entries` retarget to `team_program_id` (additive, dual-read) | T3 | unit, integration | `summer_league.py`, migration |
| T7a–e | Class renames, **one per ticket, serialized** — Edition, SourceDocument, IngestionRun, SourceRecord, DerivedAgg; each with an empty-autogenerate proof | T1–T6 | unit, integration, full suite | repo-wide codemod |
| T8 | Service-layer reorganization into §4 shape + import-contract list updates + coverage-test update | T7 | unit, integration, `make lint.entrypoints`, full suite | `app/services/`, `pyproject.toml`, `test_import_contract_coverage.py` |
| T9 | Docs: backbone §3 code-location table, alignment-doc status, vocabulary-doc adoption state | T8 | doc review | `docs/plans/` |
| T10 | QA gate: full suite, perf budgets, `make visual` baseline unchanged, spec compliance | all | all | — |

**File-overlap notes for `/create-project`:** T1, T2, and T5 touch different schema modules and are
parallel-safe. T3→T4/T6 is a real dependency chain (population before retarget); T4 and T6 are
parallel after T3. **T7a–e must be serialized against each other and against T8** — every one is a
repo-wide diff. T8 is the single largest conflict surface in the phase and should run alone. This
is the phase's critical path: T2→T3→T4/T6→T7(×5)→T8.

## 7. Exit criteria (mapped to the roadmap)

1. `team_program_id` is populated on `player_affiliations` and `summer_league_team_entries`, and
   affiliations resolve through it.
2. **A non-NBA source could assert an affiliation** — proven by the T4 integration test writing a
   federation-owned program affiliation with `nba_team_id` NULL, not by inspection.
3. The backbone doc's code-location table has no stale rows; every renamed class matches the
   backbone term it implements.
4. `app/domain/` is non-empty, contract 2 still green, and at least one real read path returns a
   `Watermark` rather than an ad-hoc tuple.
5. Every versioned table inherits `DatedVersionMixin`; Phase 3's legacy-stamp gap is closed.
6. `app/services/` is layered by role; import contracts 3, 4, and 5 pass against the new module
   names with no list drift.
7. **No user-visible change.** Page query budgets hold (or are consciously bumped), `make visual`
   baselines are unchanged, and the full suite is green.

## 8. Product QA

A standalone QA checklist is **deliberately skipped**: this phase ships no customer-facing surface,
and its correct outcome is that nothing changes visibly. The browser-verifiable contract is
therefore a regression contract, folded into T10 rather than a separate document:

1. A player page with SL affiliations renders identical team attribution before and after the
   retarget (spot-check a stable player against a pre-change capture).
2. The SL Explorer, Leaders, and season pages render unchanged after the service move.
3. `make visual` produces no baseline diffs across the phase.
4. No page's query budget regresses (`make perf`).

Note that item 4 is not a formality here: T4 adds a nullable join to affiliation reads, which is
the one change in this phase with a plausible performance signature.

## 9. Risks

- **T7/T8 conflict surface.** Five repo-wide renames plus a service move will invalidate any
  long-running branch. Mitigation: serialize them, run them late, and land them fast — this is a
  scheduling risk, not a technical one. Do not start this phase's tail while other large SL work is
  in flight.
- **Empty-autogenerate assumption.** §5a's "renaming the class produces no migration" is true
  because `__tablename__` is set explicitly on these classes — verified, but each rename ticket
  proves it per-class rather than trusting the general claim.
- **Population correctness (T3).** The NBA franchise set is closed and known, which is exactly why
  it is the first population. Historical/relocated franchises are the edge case; the script reports
  counts and the ticket states the expected number before prod.
- **Over-promotion pressure.** Wave C will look tempting once the org model lands and the canon
  entities are the obvious next lift. The alignment doc's §2 promotion test and §6 anti-goals are
  the guard: a second real spoke defines those shapes, not a third guess.
- **Scope creep into Phase 5.** The service move invites god-file decomposition. It is explicitly
  moves-only (D6); decomposition is Phase 5's, with its own guardrails already installed.
