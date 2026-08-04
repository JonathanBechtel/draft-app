# Global Player-Journey Graph — Data-Model Backbone

**Status:** Design note — **direction approved; schema NOT frozen.** Revised v2 per technical
review 2026-06-23 (see §0 + §14 review log).
**Date:** 2026-06-23
**Scope:** Foundational data model + product surfaces for tracking every basketball
prospect worldwide across leagues, events, and time. International/youth is the first
proving ground. **Basketball-first, sport-extensible** (not sport-generic — see §0).

---

## 0. The central reframe: an assertion-and-projection system

The backbone is **not "merely an event log."** It is an **assertion-and-projection
system**. Four kinds of fact flow through it, and each has *different correction
semantics* — preserving those distinctions is what makes the backbone reliable rather
than just flexible:

| Fact type | Store | Correction semantics |
|---|---|---|
| **Identity assertions** (this source-record IS this canonical player; merge/split) | `player_identity_action` + tombstones | auditable, reversible, redirectable |
| **Affiliation assertions** (player belonged to a team/program over an interval) | `player_affiliation` (assertions w/ supersession + bitemporal) | supersede / retract, not in-place edit |
| **Observations** (a measurement taken at a point in time) | `player_measurement` | append; correct by superseding observation |
| **Statistical facts** (game logs, aggregates) | stat spokes | versioned computation + coverage |

**Projections** are derived, replaceable read models computed *from* assertions:
`player_lifecycle` (current state), the journey timeline, the graph, connection summaries.
None of them are canonical data.

Two consequences the v1 draft got wrong:
- **Event sourcing does NOT dissolve identity-merge audit.** A lifecycle/affiliation event
  attached to the wrong `player_id` stays attached to the wrong identity. Identity needs
  its *own* audit model (§6).
- **Affiliations are not "append-only."** An interval whose `end_date` is later populated
  is a *mutation*. Corrections and retractions need explicit representation, or historical
  answers change invisibly after backfills (§5b).

**Sport scope:** design basketball-first with clean extension seams. Do **not** force the
first schema to be genuinely NFL-generic — that weakens important basketball semantics.
The reusable cross-sport asset is the *pattern* (identity → org/team → affiliation →
participation → projections), not a single physical schema.

---

## 1. North star

DraftGuru becomes the **default place to follow any basketball prospect from their first
marquee youth appearance to the NBA**, and to *connect the dots* on how development evolves
across leagues, events, and years. The UI may represent entities separately (event hubs,
college pages, NBA pages); the **backend glue stitches the journey together**. That glue —
canonical identity + time-aware affiliations + projections — is the most defensible asset
in the company, because it is accumulated human + machine judgment, not scrapeable data.

---

## 2. Core principles

1. **Hub-and-spoke.** Thin universal glue (identity, orgs/teams, affiliations,
   projections) + fat, domain-specific stat spokes (SL, international, college, NBA). Only
   the hub must be clean.
2. **Assertion-and-projection (§0).** Canonical = assertions with provenance + correction
   semantics. `player_lifecycle` and all timelines/graphs are *replaceable projections*.
3. **Precision over recall.** Ambiguous identity matches stay *visibly unresolved* or queue
   for review; they never silently mutate canon. Honest gaps render as intentional.
4. **Compute metrics bottom-up.** Game-by-game logs are atomic statistical truth; aggregates
   are computed from them with versioned calculation + coverage metadata.
5. **Graph as a projection.** The relational spine *is* a graph; implement in Postgres,
   derive connections via indexed joins, and materialize **only bounded summaries** after
   measured need (§8). No dedicated graph DB unless a shipped feature demands deep traversal.
6. **Event-gated inclusion is a *publication* rule, not an *identity* rule (§9).** Players
   may enter via boards, news, or source records before any marquee event; marquee-event
   presence gates what gets *published*, not who exists in the graph.
7. **Longitudinal-first — retain history by default (added 2026-07-25).** Anything carrying
   analytical or evidentiary value is materialized **append-only and as-of-dated**, with an
   atomic current-version pointer. **"Wipe clean and recompute" is an anti-pattern, not a
   shortcut** — it destroys the time axis, which is the backbone's whole point. This extends
   principle 4: aggregates are not merely *versioned in metadata*, their prior versions
   **survive**. The line is evidence vs. cache: canonical assertions are never destroyed;
   time-varying analytical projections use dated version-flip; only pure regenerable
   presentation caches may be overwritten in place (and must stamp the watermark of what they
   render). See `summer-league-simplification-backlog.md` §P2 and its app-wide audit.

---

## 3. Architecture: layers

```
PROJECTIONS    lifecycle (versioned) · journey timeline · graph · connection summaries
ASSERTIONS     identity-actions · affiliation-assertions · transactions · measurements
CANON ENTITIES organization → team/program → team_entry · competition → edition → game
IDENTITY HUB   players_master · external_ids · aliases · (new) identity-action audit
PROVENANCE     source_system · source_document/snapshot · source_record · assertion_evidence
── STAT SPOKES ── (fat, separate): participation → game_log → derived_agg, per domain
```

Lower layers block upper ones. The identity *hub* largely exists; **provenance,
org/team split, participation, affiliation assertions, and all projections are new.**
*(Superseded in part — see the §4 status update: participation and affiliation assertions have
since shipped.)*

### Where each layer lives in code (added 2026-07-25)

Maintained so the model and the codebase stay legible to each other. When a row here goes stale,
one of the two is drifting — that is the signal this table exists to give.

| Layer | Concept | Today | Domain type |
|---|---|---|---|
| Identity | canonical player | `players_master`, `player_external_ids`, `player_aliases` | `PlayerRef` |
| Identity | resolution + review | `summer_league_source_players` (class `SummerLeagueSourceRecord`), `_player_resolution_reviews` (class `SummerLeaguePlayerResolutionReview`) *(SL-namespaced table; class vocabulary aligned Phase 4)* | `SourceIdentity`, `ResolutionOutcome` |
| Provenance | source document / record | `summer_league_raw_runs` (class `SummerLeagueIngestionRun`), `_raw_files` (class `SummerLeagueSourceDocument`) *(SL-namespaced table; class vocabulary aligned Phase 4)* | `SourceDocumentRef`, `SourceRecordRef`, `Evidence` |
| Canon | organization → team/program | ✅ **built (Phase 4)** — `organizations`, `team_programs`, `organization_relationships` (`app/schemas/organization.py`: `Organization`, `TeamProgram`, `OrganizationRelationship`) | `OrganizationRef`, `TeamProgramRef` |
| Canon | competition → edition | `summer_league_competitions` (class `SummerLeagueEdition`) *(SL-namespaced table, class renamed Phase 4; parent `Competition` entity still not built — Wave C)* | `CompetitionRef`, `EditionRef` |
| Canon | team_entry | `summer_league_team_entries` *(SL-namespaced; nullable `team_program_id` added Phase 4, 519/622 dev rows backfilled)* | `TeamEntryRef` |
| Canon | game | `summer_league_games` *(SL-namespaced)* | `GameRef` |
| Assertions | affiliations | `player_affiliations` ✅ *generic, supersession-first; nullable `team_program_id` added Phase 4 — capability shipped, 0 dev rows populated (see §4 status update)* | `AffiliationAssertion` |
| Assertions | transactions / measurements | not built (`combine_anthro` is combine-scoped) | `Transaction`, `Measurement` |
| Spoke | participation | `summer_league_participation` ✅ *correct grain* | `ParticipationRef` |
| Spoke | game logs | `summer_league_player_game_logs`, `_team_game_logs` | — *(fat spoke, correctly)* |
| Spoke | derived aggregates | `summer_league_player_seasons` (class `SummerLeagueDerivedAgg`) ✅ *version-flipped (`DatedVersionMixin`), no longer destructively rebuilt* | `VersionStamps` |
| Analytical | scope baselines | `summer_league_environment_*`, `_cohort_baselines` *(SL-namespaced; already scope-generic)* | `Scope` |
| Projections | lifecycle | `player_lifecycle` *(denormalized; reducer not built)* | — |
| Presentation | event desk | `events`, `event_desk_state`, `event_desk_render_snapshots` *(generic names, SL-coupled code)* | `Watermark` |
| Domain vocabulary | temporal value objects | ✅ **built (Phase 4)** — `app/domain/temporal.py`: `Watermark`, `VersionStamps`, `Scope` | — |

*SL-namespaced* = the structure exists and works but lives under a `summer_league_` prefix; see
`summer-league-journey-graph-alignment.md` for the promotion plan. Domain types are specified in
`journey-graph-domain-vocabulary.md`.

---

## 4. Gap analysis — what exists vs. what's missing

### Already built (reuse)
- `players_master` (slug, parsed names, `birthdate`, `birth_country`, draft fields,
  `is_stub`, bio provenance); `player_external_ids` crosswalk; `player_aliases`;
  `player_mention_service.find_existing_player()` (+ `player_embeddings` vector fallback);
  `player_merge_service` (logic only — see correction below); `combine_anthro` time-series
  (combine-scoped); the Summer League spoke; `board_entries.resolution_method` vocabulary;
  consensus board tables.

### Status update 2026-07-25 — items since BUILT (supersedes the corrections below)

Verified against HEAD. Three items previously listed as missing have shipped for the Summer
League spoke:

- **Participation now EXISTS** — `SummerLeagueParticipation` (`app/schemas/summer_league.py:550`)
  at exactly the §7b grain: one row per `(player, team_entry, stint)` with `stint_no`, an
  `affiliation_id` FK, and player game logs referencing it rather than raw `(player, edition)`.
  *(The v1 correction below is now historical.)*
- **Affiliation assertions now EXIST with supersession** — `player_affiliations`
  (`app/schemas/player_affiliation.py`) carries `effective_start`/`effective_end`,
  `recorded_at`, `supersedes_id`, `superseded_at`, `retracted_at` — the §13.4 ratified
  supersession-first model, shipped.
- **SL retrofit onto participation is done** (§12 step 5).

**Status update 2026-08-03 — the org-model blocker SHIPPED (Phase 4).** `Organization` /
`TeamProgram` / `OrganizationRelationship` now exist (`app/schemas/organization.py`), with
`org_kind` (CLUB / FEDERATION / LEAGUE / SCHOOL / ACADEMY / NATIONAL_PROGRAM) and typed
relationship edges (OWNS / ACADEMY_OF / FEEDS / AFFILIATED_WITH). `player_affiliations` and
`summer_league_team_entries` both carry a nullable `team_program_id`, resolved through one
shared helper (`app.services.player_affiliation.resolve_team_target`). An integration test
proves the capability end-to-end: a FEDERATION-owned team_program affiliation with
`nba_team_id` NULL. **Two caveats, not yet closed:**
- **Population, not just capability.** `player_affiliations.team_program_id` has backfilled
  **zero rows on dev** — every dev row has `nba_team_id` NULL too, because Summer League roster
  ingest never populated that column on `player_affiliations` in the first place, so the
  backfill (which derives `team_program_id` from `nba_team_id`) has nothing to read from.
  `summer_league_team_entries` fared better (519/622 dev rows backfilled by
  `scripts/backfill_sl_team_entry_team_program.py`, which derives from `nba_stats_team_id`).
  The column, index, backfill script, and dual-read helper all ship and work; there is simply
  nothing (on the affiliation side) to backfill yet.
- **No read path calls the dual-read helper yet.** `summer_league_team_service.py` and
  `summer_league_franchise_service.py` still read `nba_team_id` directly. Infrastructure is in
  place; rendering is unchanged by construction.

**Two new consistency/debt findings (2026-07-25) — both now resolved:**

- ~~`summer_league_player_seasons` is destructively rebuilt~~ — **RESOLVED.** The class is now
  `SummerLeagueDerivedAgg` (`app/schemas/summer_league_metrics.py`), adopts
  `DatedVersionMixin`, and the metrics rebuild (`app/services/sources/summer_league/metrics.py`,
  moved from `app/services/summer_league/metrics.py` in #789) version-flips (`is_current`)
  instead of deleting. No full-wipe call remains at the old line numbers. Principle 7 is now
  observed here.
- **`roster_status` dual-write — still open.** `SummerLeagueParticipation.roster_status`
  still denormalizes what `player_affiliations` already asserts — two writers for one truth, a
  drift risk that §0's assertion-vs-projection split exists to prevent. Unchanged by Phase 4;
  tracked as Phase 5 backlog item 4.1 in `summer-league-remediation-roadmap.md`.

### Corrections to the v1 inventory (from the 2026-06-23 review — see status update above)
- **Participation does NOT exist today.** SL game logs repeat `competition_id`,
  `team_entry_id`, source identity, and canonical `player_id` *inline on every log*
  (`app/schemas/summer_league.py:430`). `summer_league_player_seasons` is a **derived
  rollup**, not a player-season/roster *bridge* that logs reference. Participation is **new
  infrastructure** (§7). ***— RESOLVED 2026-07-25; participation shipped.***
- **Merge is not audited or reversible.** `player_merge_service` reassigns/deletes across a
  manually maintained table list (`player_merge_service.py:83`) and does **not persist its
  actor** (`:520`). Event sourcing does not fix this (§6).
- **`player_lifecycle` is fully denormalized** (`app/schemas/player_lifecycle.py:143`) —
  stores `current_affiliation_name` as a string, no `current_affiliation_id`.

### Missing / partial (the build)
1. **Provenance/evidence primitives** — first-class (§a below).
2. **Identity-action audit + tombstoning** (§6).
3. **Organization → team/program → team_entry split + organization_relationship** (§1-org).
4. **Participation** (player, team_entry, stint) (§7).
5. **Affiliation assertions** with supersession/bitemporal + lifecycle reducer (§5).
6. **Measurements** as observations, event-scoped (§5c).
7. **Generic competition/edition/game model** beyond SL.
8. **Projections:** journey timeline, connection summaries, level normalization.
9. **College depth** — retrofit onto org/team + participation.

---

## 5. The journey spine: assertions → projection

### 5a. Lifecycle is a *versioned* projection
`player_lifecycle` becomes a derived, replaceable projection reduced from **affiliation
assertions + transactions** (NOT observations). Never hand-edited. Each field is a rule
over the assertions:

| lifecycle field | reduced from |
|---|---|
| `current_affiliation_id` *(add — not just a name)* | the affiliation assertion currently in effect |
| `lifecycle_stage` | level of the in-effect affiliation (senior club ⇒ PRO_NON_NBA; post-debut ⇒ NBA_ACTIVE) |
| `draft_status` | latest draft-relevant transaction |
| `commitment_status` | latest commitment transaction |
| `expected_draft_year` | **NOT** birthdate+affiliation alone — eligibility ruleset + sourced/manual override |

**Version the reducer.** Persist `reducer_version`, `derived_at`, and `input_watermark` on
the projection so a stale or buggy reduction is detectable and rebuildable.

### 5b. Affiliations are assertions, not append-only intervals
An interval whose `end_date` is later filled is a mutation; retraction must be representable.
Use **immutable affiliation assertions** with `supersedes_id` / `retracted_at`, *or* a
**bitemporal** model. At minimum distinguish three times:
- **effective** — when the affiliation was *true* in the world;
- **recorded** — when DraftGuru *learned* it;
- **superseded/rejected** — when a later assertion replaced or invalidated it.

Otherwise historical answers (and the comp corpus) shift invisibly after every backfill.

### 5c. Observations stay OUT of the lifecycle stream
"Measured" is an **observation, not a lifecycle transition.** Split the streams:
- `player_affiliation` (interval assertions, §5b)
- `player_transaction` (point lifecycle transitions: committed / signed / declared /
  withdrew / drafted / debuted / transferred)
- `player_measurement` (anthro observations, event-scoped — generalize `combine_anthro`
  beyond the NBA combine to any edition + date)
- optional `player_timeline_projection` combining all of the above **for display**

The UI shows one unified timeline; only affiliations + transactions feed the lifecycle
reducer.

---

## 6. Identity: action audit + tombstoning (its own model)

Identity correction is a *separate* concern from lifecycle/affiliation correction. Add:

```
player_identity_action
  action_type        MERGE | SPLIT | REASSIGN
  surviving_player_id, affected_player_id(s)
  actor, reason, occurred_at
  movement_manifest  per-record list of what moved
  reversal_payload   before/after values or a reversible operation
```

- **Tombstone merged players** with a `canonical_player_id` redirect rather than deleting
  immediately. Without surviving redirect rows, true automated **splitting** stays
  extremely hard.
- Resolution remains **precision-first**: confidence-scored, ambiguous → visible-unresolved
  / review queue (generalize `summer_league_player_resolution_reviews`), never auto-merge
  into canon.

---

## 7. Stat spokes: org/team, participation, game logs

### 7a. Organizations vs. competitive teams (don't compress)
```
organization            FC Barcelona · a national federation        (corporate / governing)
  └── team/program       FC Barcelona Bàsquet · its U18 squad · U17 national team
        └── team_entry    that team's entry in a specific competition edition
```
Add typed `organization_relationship` rows: `OWNS`, `ACADEMY_OF`, `FEEDS`, `AFFILIATED_WITH`.
**Player affiliations point to a team/program, normally NOT a corporate/governing org.**
Generalize SL's `team_entry.nba_team_id` to reference a team/program.

### 7b. Participation grain (new)
The bridge grain is **`(player, team_entry, participation_stint)`** — not `(player, edition)`,
which fails when a player changes teams mid-season, appears for multiple squads in one
competition, plays as a guest/replacement, or is erroneously assigned to two teams pending
review. **Game logs reference the participation**, not raw (player, edition).

A participation typically maps into an affiliation, but the rule is **not "exactly one
affiliation."** National-team and club affiliations legitimately overlap; loans, dual
registrations, and uncertain dates occur. Any exclusion constraint must include
**affiliation scope + type**, not just the time interval.

### 7c. Game-by-game everywhere + provenance
Collect per-game box scores for every participation **as much as possible** — the
precondition for the recalibrated-advanced-metrics moat.
- `game` becomes first-class in every spoke; a near-uniform **box-score core** + thin
  spoke extension (box scores are far more uniform than aggregates).
- **Aggregate provenance is richer than computed/source-reported:** record
  `calculation_version`, `input_coverage`, `source_snapshot`, and `metric_definition`.
- **Completeness model** per participation (generalize SL `data_quality`):
  FULL / PARTIAL / BOX_ONLY / RAW_ONLY + `pbp_available`. Lets "4 of 7 group games" render
  as intentionally partial. **PBP deferred.**
- **Volume:** game logs are the highest-cardinality table (SL alone ~22k rows; global =
  millions) — index the participation→game-log path up front; honor the page-perf guard.

---

## 8. The graph — derive first, summarize later

The spine *is* a typed, time-stamped graph; the graph view is a **projection** of the
relational tables.

**Nodes:** `Player` · `Organization` · `Team/Program` · `Competition`/`Edition` · `Game` ·
`Cohort`. **Canonical edges** (already in the tables): affiliation, participation, game
membership (`PLAYED_IN`), team_entry (`ENTERED`), edition `INSTANCE_OF` competition.

**Do NOT materialize pairwise edges yet.** `TEAMMATE_OF` and especially per-game
`OPPONENT_OF` are quadratic — a single 12-v-12 game yields 144 opponent pairs, quickly far
larger than the game-log data it summarizes. Instead:
- keep participation + game membership as the canonical edges;
- **derive connections via indexed joins** first;
- materialize **only bounded, product-specific summaries**, and only after query
  measurements justify it:

```
player_connection_summary(
  player_low_id, player_high_id, connection_type,
  first_date, last_date, shared_event_count, shared_game_count)
```

Defer a real graph store; if deep variable-length traversal ever ships, Apache AGE keeps it
in Postgres — a "only if a feature demands it" move.

---

## 9. Inclusion: publication rule, not identity rule

A player may **enter the graph** through boards, news, source records, or resolution work —
*before* any marquee event. **Marquee-event presence (official stats + measurements) gates
what is publicly *published* and how prominently**, not whether the identity exists. This
keeps identity creation decoupled from product-surface eligibility.

---

## 10. Provenance / evidence primitives (first-class)

`source` / `confidence` / `confirmed_by` columns are insufficient for the moat. Introduce
reusable concepts so one fact can have multiple supporting *or conflicting* sources:

```
source_system          a feed/site/ruleset
source_document        an ingestion snapshot (the fetched artifact)
source_record          a row within a document
assertion_evidence     links an assertion to the source_record(s) supporting it,
                       with resolution_method + model/ruleset version
```

**Confidence belongs to an *assertion* or *resolution decision*, not to the underlying
player or affiliation row.** This is what lets the graph carry conflicting claims honestly.

---

## 11. Product surfaces

**Net-new, uniquely graph-shaped:** (1) **Connection Finder / "six degrees"** (shared
academy / teammate / opponent — viral share cards); (2) **Pipeline / lineage pages** (org
as producer; federation pipelines); (3) **Teammate/opponent context** as automatic
analytical-voice facts ("dropped 30 *against* [future lottery pick]").
**Graph-enhanced roadmap items:** (4) **Comparable-journey discovery** (path/subgraph
similarity, richer than stat-snapshot KNN); (5) **Draft-stock-market as a network**
(contagion, portfolio recommendations); (6) **Interactive explorer + Scout's Notebook**
(a watchlist is a curated subgraph — the literal "organize thoughts" surface).
**Supporting surfaces:** Event Hub (article-as-app), Draft-Class Watch
(`/draft/2028/international` — long-lead SEO), International Calendar (retention spine),
player-page International/Youth section.

---

## 12. Recommended revised critical path

*Progress marked 2026-07-25 — see §4 status update. Updated 2026-08-03 — Phase 4 shipped
step 2.*

1. **Source/evidence primitives + identity-action audit** (§10, §6). — *open*
2. **Organization → team/program → team_entry** model + `organization_relationship` (§7a).
   — **✅ DONE (Phase 4).** `Organization` / `TeamProgram` / `OrganizationRelationship` shipped
   (`app/schemas/organization.py`); `player_affiliations.team_program_id` and
   `summer_league_team_entries.team_program_id` are live columns with a shared resolution
   helper. **Not yet done:** populating `player_affiliations.team_program_id` (0 dev rows) and
   switching any read path off `nba_team_id` — see §4 status update 2026-08-03.
3. **Competition → edition → game** model (§7). — *open (SL-scoped equivalents exist; class
   vocabulary aligned to `SummerLeagueEdition`/`SummerLeagueGame` in Phase 4 — physical
   promotion to generic tables is still Wave C, deliberately deferred alongside spoke #2)*
4. **Participation** model — `(player, team_entry, stint)` (§7b). — **✅ DONE (SL-scoped)**
5. **Retrofit Summer League** onto participation; prove the complete path (queries,
   aggregation, resolution) **before** building the international spoke. — **✅ DONE**
6. **Affiliation assertions** (supersession/bitemporal) + **versioned lifecycle reducer**
   (§5). — **✅ assertions DONE** (supersession-first, shipped; can now target either
   `nba_team_id` or `team_program_id`); *reducer still open*.
7. **International source-resolution + ingestion spoke.**
8. **Measurements + computed aggregates** (§5c, §7c).
9. **Timeline / read projections.**
10. **Connection summaries** — only after real feature queries exist (§8).

**Cheapest proof of the backbone (icing):** backfill 10–20 known NBA-international journeys
(Jokić, Dončić, Wembanyama) to seed the comp corpus and validate identity stitching against
*known-correct* answers — but note it does not exercise live ingestion/resolution, so it
follows, not replaces, steps 1–5.

---

## 13. Decision log — resolved & remaining

**Resolved 2026-07-08** (⭐ = user-ratified; others adopted along the review recommendation):

1. **Transaction-event vocabulary** — closed, versioned enum of *transitions only*
   (measurements/stats excluded), grouped:
   - *Recruiting:* `COMMITTED`, `DECOMMITTED`, `RECLASSIFIED`
   - *Draft process:* `DECLARED`, `WITHDREW`, `DRAFTED`, `WENT_UNDRAFTED`
   - *Pro movement:* `SIGNED`, `WAIVED`, `TRANSFERRED`, `LOANED`, `DEBUTED`
   - *College:* `ENROLLED`, `ENTERED_PORTAL`, `TRANSFERRED_SCHOOL`

   Each row carries `effective_date` + evidence.
2. **Eligibility ruleset ⭐** — **heuristic + override now.** A pathway heuristic
   (`birthdate` + pathway + HS class) yields a *default* `expected_draft_year`, always beaten
   by a sourced/manual value that wins; store `expected_draft_year_source`
   (RULE | SOURCE | MANUAL). Full rules engine deferred until international volume justifies it.
3. **Org model** — add an `organization.org_kind` discriminator
   (CLUB / FEDERATION / LEAGUE / SCHOOL / ACADEMY / NATIONAL_PROGRAM). Affiliations point only
   to a team/program; a national team is a team/program **OWNED** by a federation org; leagues
   live in the competition/edition model with a governing org attached via a typed edge.
4. **Affiliation temporal model ⭐** — **supersession-first.** Immutable assertions with
   `supersedes_id` / `retracted_at` + effective/recorded/superseded timestamps. Upgrade to full
   bitemporal only if a shipped feature must reproduce past *beliefs* exactly (see remaining).
5. **Data-sourcing spike ⭐** — **FIBA LiveStats first**, targeting an upcoming U18/U19 summer
   edition, mirroring `scripts/probe_summer_league_api.py`. EuroLeague/EuroCup is the intended
   second spoke; RealGM deprioritized (scrape-fragile).
6. **Auto-stitch policy** — precision-biased three bands: (a) auto-accept only on hard
   external-ID match or exact name+birthdate+crosswalk with no competing candidate; (b) anything
   below a high score → visible-unresolved / review queue; (c) never auto-merge two existing
   canonical players (always via `player_identity_action` + actor). Calibrate the numeric
   threshold against the known-journey backfill as a labeled set — no hard-coded magic number.
7. **Graph-store trigger** — adopt a graph store (Apache AGE) only when a *shipped* feature
   needs variable-length / >2-hop traversal that indexed SQL can't serve within the perf budget
   (e.g., interactive multi-hop shortest-connection). Bounded 1–2 hop stays in Postgres via
   `player_connection_summary`.

**Still open:**
- **Level-adjusted metric model (§7c).** ⚠️ **Correction 2026-07-25:** the claim that "the rate
  layer (per-36/per-100) ships now as a shared util" **did not hold in practice** — per-36/per-100
  is implemented at least twice (Python `explorer_service.py:2527-2541` and SQL
  `:2591-2607`), and core ratio formulas like TS% appear at ~8 sites. The shared-util intent
  is now specified in **`summer-league-stat-engine-reuse-spec.md`** (doc #2), which defines the
  source-agnostic engine, the metric registry (declare each formula once, with `registry_version`),
  and a `metric → required inputs → source provides` capability model. **The level-adjustment
  translation model itself remains open** — doc #2 supplies the engine it would live in, not the
  translation study. Note doc #2's registry is designed to carry the Ledger's
  `comparison_semantics` / `allowed_reference_kinds` / `minimum_sample_rule` fields, which are the
  guardrails any level-adjustment work must respect.
- **Bitemporal upgrade trigger** — the concrete feature that would force the
  supersession → bitemporal upgrade (reproduce-past-beliefs reproducibility).
- **Auto-stitch numeric threshold** — to be set empirically from the backfill labeled set,
  not chosen up front.

---

## 14. Review log

- **2026-06-23 — technical review (v1 → v2).** Direction approved; schema explicitly not
  frozen. Eight necessary changes incorporated: (1) identity-action audit + tombstoning is
  separate from lifecycle event-sourcing; (2) affiliations are assertions with
  supersession/bitemporal correction, not append-only; (3) organization vs. team/program
  vs. team_entry split + `organization_relationship`; (4) participation is new
  infrastructure (SL has no participation bridge today); (5) participation grain is
  `(player, team_entry, stint)`, and "exactly one affiliation" relaxed to include scope +
  type; (6) first-class provenance/evidence primitives; (7) observations
  (`player_measurement`) separated from the lifecycle transaction stream; (8) do not
  materialize pairwise graph edges — derive first, summarize via `player_connection_summary`
  later. Plus: inclusion is a publication rule not an identity rule; `current_affiliation_id`
  on lifecycle; versioned reducer (`reducer_version`/`derived_at`/`input_watermark`);
  eligibility ruleset for draft year; richer aggregate provenance; basketball-first /
  sport-extensible (not sport-generic). Critical path reordered (§12).
- **2026-07-25 — post-Summer-League retrospective pass.** Verified the doc against shipped code
  after the SL launch. (1) §4 gap analysis updated: **participation** and **affiliation
  assertions with supersession** are now BUILT (SL-scoped), and the SL retrofit is done —
  §12 steps 4, 5, and the assertion half of 6 marked complete. (2) Identified the
  **organization → team/program model (§7a, step 2) as the single live blocker** for a second
  spoke, since `team_program_id` stays reserved until it ships. (3) Added **core principle 7,
  longitudinal-first**, after an app-wide audit found the SL metrics rebuild destroys history on
  every run — a concrete violation of §7c's versioned-computation intent. (4) Recorded the
  `roster_status` dual-write as a drift risk. (5) Corrected the §13 claim that the per-36/per-100
  rate layer shipped as a shared util — it did not; the shared engine is now specified in doc #2.
  Companion docs from this pass: `summer-league-simplification-backlog.md`,
  `summer-league-stat-engine-reuse-spec.md`, `summer-league-desk-simplification-spec.md`.
- **2026-07-08 — open-items decision pass.** Resolved §13: transaction vocabulary (closed
  set), `org_kind` discriminator, auto-stitch three-band policy, and graph-store trigger rule
  (adopted along recommendation); user-ratified: affiliation temporal model =
  **supersession-first**, first data-sourcing spike = **FIBA LiveStats**, eligibility =
  **heuristic + override now**. Still open: level-adjusted metric model spec, bitemporal
  upgrade trigger, empirical auto-stitch threshold.
- **2026-08-03 — Phase 4 (journey-graph conversion) shipped.** The org model landed: `Organization`
  / `TeamProgram` / `OrganizationRelationship` (§7a, §12 step 2), closing the single blocker
  identified in the 2026-07-25 pass — `player_affiliations` and `summer_league_team_entries`
  both now carry a resolvable `team_program_id`. The destructive-rebuild finding on
  `summer_league_player_seasons` (renamed `SummerLeagueDerivedAgg`) is also resolved via
  `DatedVersionMixin` and version-flip. Class vocabulary aligned to backbone terms across the SL
  spoke (`SummerLeagueEdition`, `SummerLeagueSourceDocument`, `SummerLeagueSourceRecord`,
  `SummerLeagueIngestionRun`, `SummerLeagueDerivedAgg`); `app/domain/temporal.py` shipped
  (`Watermark`, `VersionStamps`, `Scope`). **Not resolved:** `team_program_id` population on
  `player_affiliations` (0 dev rows) and read-path adoption of the dual-read helper; the generic
  competition/edition/game promotion (Wave C) and the rest of `app/domain/` (identity, canon,
  provenance, assertions, spoke) remain deliberately deferred alongside spoke #2. See
  `summer-league-journey-graph-alignment.md` and `journey-graph-domain-vocabulary.md`.

---

## 15. Connections to existing roadmap

- **Draft-calendar pipeline strategy** — international/youth is the "intl" leg and the
  longest-lead SEO moat.
- **Sport-extensibility / nfldraft.app (April 2028)** — the reusable asset is the *pattern*,
  not a sport-generic schema; build basketball-first with clean seams.
- **Draft-stock-market metaphor** — the journey graph rendered as price-over-time; the
  network view powers contagion + portfolio recommendations.
- **Summer League** — the first spoke retrofitted onto participation and the proof of the
  complete path.
