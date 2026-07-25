# Aligning Summer League with the Player-Journey Graph (doc #4)

**Status:** Design analysis. Strategy document — no code changes proposed inline.

**Canonical backbone doc:** `docs/plans/global-player-journey-graph.md`. This doc does **not**
restate or replace it — it answers one question: *how should the existing Summer League
architecture be reorganized so it sits correctly on that backbone?*

**Governed by** doc #1's principles: **P1** (one canonical record; projections are thin readers)
and **P2** (longitudinal-first; destructive rebuild is the exception).

---

## 1. The finding: the backbone isn't missing, it's namespaced

The journey-graph critical path (§12) lists provenance primitives, the competition→edition→game
model, and the org/team_entry split as work still to be done. Verified against HEAD, **no generic
versions of these tables exist** — there is no `competitions`, `competition_editions`,
`team_entries`, `games`, `organizations`, or `team_programs` table in `app/schemas/`.

But Summer League has working implementations of nearly all of them, under an SL prefix:

| Journey-graph layer & concept | §12 status | Existing SL implementation |
|---|---|---|
| **Canon entity** — competition → *edition* | step 3, open | `summer_league_competitions` (one edition = year + league_id + venue_slug; carries `pbp_available`, `data_quality`) |
| **Canon entity** — *team_entry* | step 2 (§7a) | `summer_league_team_entries` (team-in-competition, `nba_stats_team_id`, W/L) |
| **Canon entity** — *game* first-class | step 3, open | `summer_league_games` (tip time, scores, status, round label) |
| **Provenance** — source_document / source_record | step 1, "missing" | `summer_league_raw_runs` + `_raw_files` (endpoint, s3_key, sha256, parse_status, manifest) |
| **Identity** — resolution + review queue | §6 | `summer_league_source_players` (candidates, confidence → `canonical_player_id`), `_player_resolution_reviews` |
| **Spoke** — participation grain | step 4 | `summer_league_participation` ✅ *already correct shape* |
| **Assertions** — affiliations | step 6 | `player_affiliations` ✅ *already generic & hub-level* |
| **Scope-relative baselines** (§7c "moat piece") | open | `summer_league_environment_*` — already uses generic `scope_key`/`scope_kind` |

**The reframe:** Summer League built the entire journey-graph stack privately. Those private
implementations are the **only** versions that have survived production contact with real data,
real provider lag, and real identity ambiguity.

> **Therefore: promote, don't rebuild.** The alignment work is largely lifting proven structures
> out of the `summer_league_` namespace — not designing generic equivalents from scratch.

This is also the correct application of the session's recurring lesson. Designing the generic
canon layer at N=0 would repeat the Event Desk mistake (framework-shaped, one instance, unvalidated).
Promoting a structure that already carries a season of production evidence is the opposite move.

---

## 2. The promotion test

Not everything should be promoted. Journey-graph principle 1 is explicit: *"thin universal glue +
**fat, domain-specific stat spokes**. Only the hub must be clean."* Over-promotion recreates the
N=1 framework trap one layer down.

**Promote a structure out of the SL namespace only when all three hold:**

1. **Layer test** — it belongs to the hub, canon-entity, provenance, or assertion layer in §3's
   stack (not to a stat spoke).
2. **Second-spoke test** — a FIBA/college/G-League spoke would need *the same shape*, not merely
   something similar.
3. **Evidence test** — the current design has survived production contact and its shape is
   understood.

**Keep it in the spoke** when it encodes genuinely event- or provider-specific semantics.

### Applying the test

**PROMOTE** (hub/canon/provenance — generic shape, proven):

- `summer_league_competitions` → generic **edition** (with a competition parent per §7)
- `summer_league_team_entries` → generic **team_entry** (retargeted at team/program, §7a)
- `summer_league_games` → generic **game** core + thin spoke extension (§7c explicitly wants this:
  *"a near-uniform box-score core + thin spoke extension"*)
- `summer_league_raw_runs` / `_raw_files` → generic **source_document / source_record** (§10)
- `summer_league_source_players` + `_player_resolution_reviews` → generic **source_record +
  resolution/review** machinery (§6 already calls for generalizing the review queue). The
  *candidate-scoring and review* workflow is generic; only the source-record *shape* is SL.
- `summer_league_environment_*` → generic **scope/environment profiles**. Already scope-generic by
  design and the best-designed temporal model in the repo (doc #2 §5). Both §7c's level-adjusted
  metric model and the Ledger's "native reference frame" need exactly this.
- `summer_league_cohort_baselines` → generic scope baselines (same rationale)
- `summer_league_pipeline_states` / `_batch_progress` → generic pipeline orchestration
- Stat computation (`metrics.py`) → the shared engine, per doc #2

**KEEP FAT IN THE SPOKE** (correctly SL-specific):

- `summer_league_player_game_logs` / `_team_game_logs` — box scores. Per §7c these get a
  *near-uniform core* with a thin SL extension, but the spoke owns them.
- `summer_league_shot_events` / `_play_by_play_events` — spoke-local event detail
- `normalization.py`, `nba_stats_client.py`, `endpoints.py`, roster parsing — provider-specific
  adapters. **This is where SL-specificity is supposed to live.**
- `summer_league_participation` — §3 places participation *in the spoke*; its current shape is
  already correct. (Fix the `roster_status` dual-write; don't relocate the table.)

**HOLD — do not promote yet** (N=1, or blocked):

- `app/services/event_desk/*` — framework-shaped at one instance. Doc #3 says fix the freshness
  contract and latency partitioning *before* generalizing. Harvest the framework when a real
  second event forces the seams.
- `summer_league_desk_*` projection tables — presentation projections, not backbone.

---

## 3. The blocker, and why it's also the unlock

`player_affiliation.py:73` reads `# team_program_id: reserved — added when the generic org model
ships`. Affiliations can only target `nba_team_id` today, so **no non-NBA source can assert an
affiliation at all.** That makes §7a (organization → team/program → team_entry) the single live
blocker for spoke #2.

The useful part: **promoting `summer_league_team_entries` and shipping the org model are the same
piece of work.** SL's `team_entries.nba_stats_team_id` and affiliations' `nba_team_id` both need
retargeting at the same new `team/program` entity. Doing them together means the org model ships
*validated by an existing production spoke* rather than as a speculative schema.

Order of operations for that one change:

1. Introduce `organization` (with `org_kind` per §13.3) → `team_program` → retarget `team_entry`.
2. Populate from existing NBA teams (a known, closed, correct set — a safe first population).
3. Add `team_program_id` to `player_affiliations`; backfill from `nba_team_id`; keep both during
   transition.
4. Retarget SL `team_entries`.

---

## 4. Service-layer reorganization: sources become adapters

Tables are only half of it. Today `app/services/` mixes layers: ~40 modules under
`summer_league/` plus 10 top-level `summer_league_*.py` files, spanning ingestion, normalization,
identity, metrics, environment, desk projection, and pipeline plumbing — all one namespace.

**Target shape**, matching §3's layering:

```
app/services/
  stats/        engine + metric registry + capability model        (doc #2)
  backbone/     identity resolution · affiliations · participation ·
                canon entities (edition/team_entry/game) · provenance
  ingest/       generic pipeline orchestration, batching, locks, state
  sources/
    summer_league/   NBA Stats client, endpoints, normalization,
                     roster parsing → emits canonical assertions
    <spoke 2>/       FIBA LiveStats adapter (§13.5)
  event_desk/   presentation projection                             (doc #3)
```

**The governing rule — the one the user stated as the product requirement:**

> Every data source is an **adapter** whose only job is to translate its raw feed into canonical
> assertions on the shared backbone. No source keeps a parallel store. Everything downstream —
> stats, environment baselines, projections, timelines — is generic and source-blind.

Two concrete consequences:

- **`source` strings become structured provenance.** Today affiliation `source` values are
  SL-coded strings like `"nba_summer_league_roster"`. Under §10 these become
  `source_system` / `source_document` / `source_record` references with `assertion_evidence`.
  That is what lets one fact carry multiple supporting *or conflicting* sources.
- **The stat engine is fed by adapters, not tables.** Doc #2's `StatInputs` is the neutral shape;
  each spoke supplies a small adapter. A new spoke inherits every metric its data supports.
- **Adapters own a source-quirk ledger.** The SL provider's pathologies were each discovered as
  an incident rather than scoped as a known cost: TLS impersonation required for access at all,
  minutes silently corrupted mid-event (+97:00 offsets reached dev *and* prod before a guard
  existed), a legacy player-id crosswalk for pre-2017 data, a skeletal-pool pace floor. Under P3
  the adapter is where that knowledge lives: a documented failure-mode inventory per source, and
  ingress validation guards at the adapter boundary, so a lying feed is rejected or quarantined
  before it becomes canonical — and the next spoke's vendor is scoped as "this feed will lie to
  us in these ways" up front.

---

## 5. Sequencing — cheapest and least reversible last

Table renames and migrations are expensive and risky; service reorganization and interface
extraction are cheap and internal. Sequence accordingly.

**Wave A — free, no migration.** Reorganize the service layer into the §4 shape (module moves +
imports). Extract the stat engine (doc #2 Issue A). Introduce generic *read interfaces* over
existing SL tables — the adapter seams — without touching schemas. **Plus the vocabulary
alignment pass in §5a.** This delivers most of the reuse benefit at near-zero schema risk and
makes the remaining coupling visible.

### 5a. Light namespacing — align vocabulary without touching the schema

**The enabling fact:** these SQLModel classes set `__tablename__` explicitly, so **renaming the
Python class produces no migration.** Alembic compares table names and columns, not class names.
Class names, module paths, and docstrings are therefore free to align with journey-graph
vocabulary today.

**This is not tidiness — it is semantics, and the misalignment has already cost us.** §12 lists
the "competition → edition → game model" as open while `summer_league_competitions` *is* an
edition table. A reader moving between the backbone doc and the code cannot tell they are the
same thing. Aligning the vocabulary converts *"we must build an edition model"* into *"we have
one, it is misnamed"* — which is the entire finding of §1 of this doc.

**Already aligned — leave alone:** `SummerLeagueTeamEntry` (team_entry), `SummerLeagueGame`
(game), `SummerLeagueParticipation` (participation — and its docstring already cites
*journey-graph §7b*, which is the pattern below), `SummerLeagueEnvironmentProfile` (scope profile).

**Misaligned — rename the class, keep the table:**

| Current class | Journey-graph term (§) | Note |
|---|---|---|
| `SummerLeagueCompetition` | **edition** (§7) | The *competition* is the recurring series; the 2026 Las Vegas instance is an **edition**. Current name claims the parent concept. |
| `SummerLeagueRawFile` | **source_document** (§10) | An ingestion snapshot — exactly §10's definition |
| `SummerLeagueRawRun` | **source_document batch / ingestion run** (§10) | The run that produced a set of source documents |
| `SummerLeagueSourcePlayer` | **source_record** (§10) + resolution target (§6) | A row within a document, carrying the identity assertion |
| `SummerLeaguePlayerSeason` | **derived_agg** (§3) | §3's spoke chain is `participation → game_log → derived_agg`; this is the third element |

**Two conventions to adopt, both free:**

1. **Docstring citation.** Every table implementing a journey-graph concept cites its section, as
   `SummerLeagueParticipation` already does. This makes the hub↔spoke mapping self-documenting
   and directly prevents a future reader concluding a layer is unbuilt when it exists.
2. **Module namespacing.** Consolidate the incoherent split — ~40 modules under
   `app/services/summer_league/` plus 10 top-level `app/services/summer_league_*.py` files — into
   one package organized by §4's layers. The current split communicates nothing.

**Do NOT rename in this pass:** `__tablename__` values, column names, public URLs
(`/stats/summer-league/...` carries SEO value), or template directories. Those are Wave C, and
only when a second consumer justifies the migration.

**Why do this early:** it costs nothing, it makes the promotion targets in §2 obvious rather than
archaeological, and when Wave C arrives the physical rename becomes mechanical — the conceptual
work is already done and reviewed.

**Wave B — the blocker.** Ship the org → team/program model and retarget affiliations +
`team_entries` (§3). This is the one migration that genuinely gates spoke #2.

**Wave C — promote canon entities.** Generalize edition / game / provenance out of the SL
namespace, with SL as the first (already-populated) spoke. Best done *with* spoke #2 in flight so
the shape is validated by two real cases rather than one plus a guess.

**Wave D — promote the analytical layer.** Environment/scope profiles and cohort baselines to
generic scope-relative baselines; this is the substrate the Ledger and the level-adjusted metric
model both need.

**Throughout:** doc #2's dated materialization (P2) and doc #3's Desk work proceed in parallel —
they are orthogonal to this reorganization and address the operational risk.

---

## 5b. Domain types: where OOP actually pays (and where it repeats the trap)

The journey graph is a genuine domain model, and expressing it in code — not only in tables and
docs — is what would make the app coherent across competitions. But "shared base classes for
competitions" is also exactly how the Event Desk became framework-shaped at N=1. The two are
separated by one rule:

> **An abstraction whose shape is dictated by a consumer that exists today is safe at N=1. An
> abstraction generalized from a single producer is not.**

Doc #2's engine *requires* a neutral `StatInputs` — a real requirement right now, with SL merely
its first supplier. Safe. A `BaseCompetition` distilled from `SummerLeagueCompetition` is guessing
what spoke #2 needs from a sample of one. Not safe. Same instinct, opposite risk.

**Current state:** `app/schemas/base.py` contains exactly one mixin (`SoftDeleteMixin`);
`Protocol`/ABC patterns are already used in ~5 services (including `event_desk/registry.py`,
`raw_ingestion.py`, `player_resolution.py`). The pattern is familiar here — the domain-type layer
is simply thin.

### Three layers that pay off immediately

**1. Value objects — the vocabulary made real.** Plain dataclasses, no ORM coupling:
`EditionRef`, `TeamEntryRef`, `ParticipationRef`, `Scope` (scope_key/scope_kind), `Watermark`
(`source_as_of` + `projection_version`, per doc #3 §1), `Assertion` with provenance (§10). These
give every spoke one vocabulary and cost nothing structurally. They also make §5a's naming
alignment enforceable in signatures rather than by convention.

**2. Protocols — behavioral interfaces defined by their consumers.** Composition, not inheritance:

- `SourceAdapter` — fetch raw → emit canonical assertions (the §4 rule as a type)
- `StatInputsProvider` — spoke rows → neutral `StatInputs` (doc #2)
- `CapabilityDeclaration` — which canonical inputs this source provides (doc #2's capability model)
- `ScopeProvider` — which scopes exist for baselines

Each is shaped by what the engine/backbone **needs**, so a second spoke implements a known
contract instead of inheriting a guess.

**3. Mixins for cross-cutting invariants — the highest-value item here.** A `DatedVersionMixin`
carrying `version` / `registry_version` / `calculation_version` / `is_current` / `as_of`
**encodes P2 as a type.** Longitudinal-first stops being a principle someone must remember and
becomes something a table inherits. This matters concretely: the metrics rebuild violated P2 in
part because nothing in the code made the rule visible. A mixin makes the correct shape the
default and the violation conspicuous in review.

Related candidates: a `ProvenanceMixin` (§10 source_document/record references) and a
`CoverageMixin` (FULL / PARTIAL / BOX_ONLY / RAW_ONLY per §7c).

### The trap: ORM polymorphic inheritance across spokes

**Do not build `BaseCompetition` → `SummerLeagueEdition` → `FIBAEdition` as an ORM hierarchy.**

- Joined/single-table inheritance imposes real query and migration complexity, against this
  repo's stated "intentionally boring and conventional" backend ethos.
- It is generalization from a single producer — the N=1 trap in a new costume.
- **§7c already prescribes the composition answer at schema level:** *"a near-uniform box-score
  core + thin spoke extension."* That is a shared core table plus a spoke extension table — not a
  class hierarchy. Follow it.

Shared *columns* via mixins: yes. Shared *behavior* via protocols: yes. Shared *identity* via ORM
polymorphism: no.

### Sequencing

Value objects and the `DatedVersionMixin` belong in **Wave A** — they are free, they encode
decisions already made (P2, doc #3's watermark contract), and they make later waves mechanical.
Protocols land with their consumers: `StatInputsProvider` and `CapabilityDeclaration` with doc #2's
engine; `SourceAdapter` when the service layer is reorganized (§4). Nothing here waits on spoke #2
— but nothing here is *generalized from* SL either.

---

## 6. Anti-goals

- **Do not rename *tables* purely for tidiness.** Every physical rename is a migration with real
  risk. Promote a table when a second consumer needs it, not to satisfy a naming scheme.
  **Distinguish this from §5a:** aligning *Python class names, modules, and docstrings* to
  journey-graph vocabulary is free (no migration) and semantically valuable — do that early.
  Changing `__tablename__`, columns, or public URLs is the expensive kind — defer to Wave C.
- **Do not generalize the Event Desk framework yet** (doc #3 §7). One instance is not evidence.
- **Do not build ORM inheritance hierarchies across spokes** (§5b). Shared columns via mixins and
  shared behavior via protocols, yes; polymorphic `BaseCompetition` subclassing, no — §7c's
  "uniform core + thin spoke extension" is the composition answer already on the books.
- **Do not make the spokes thin.** Journey-graph principle 1 wants fat spokes. SL-specific
  normalization and provider clients belong in SL and should stay there.
- **Do not build a generic canon layer speculatively ahead of spoke #2.** Wave C is deliberately
  scheduled *alongside* the second spoke so two real cases define the shape.
- **Do not let the second spoke start its own parallel store.** This is the failure mode the whole
  backbone exists to prevent, and the pressure to do it will be highest under deadline.

---

## 7. What this buys

- **Spoke #2 becomes small.** With the org model shipped and canon entities promoted, a new
  competition needs: a source adapter, a `StatInputs` adapter, a capability declaration. It
  inherits identity resolution, affiliations, participation, metrics, scope baselines, and
  longitudinal history.
- **The Ledger becomes reachable.** The Player Development Ledger needs stages from multiple
  sources on one identity with comparable reference frames — which is precisely promoted canon
  entities + generic scope baselines + doc #2's registry semantics.
- **The moat compounds.** Per §1 of the backbone doc, the defensible asset is accumulated
  identity + affiliation judgment. Every source that feeds the shared backbone deepens it; every
  source that keeps a private store does not.
