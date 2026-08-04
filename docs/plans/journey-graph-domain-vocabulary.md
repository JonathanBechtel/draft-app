# Journey-Graph Domain Vocabulary

**Status:** Design spec. Strategy document — no code changes proposed inline. **`temporal.py`
SHIPPED 2026-08-03** (Phase 4, #780) — `app/domain/temporal.py` defines `Watermark`,
`VersionStamps`, `Scope` as frozen dataclasses, adopted at the trend read seam
(`latest_trend_watermark()`). The rest of the catalog below (`identity.py`, `canon.py`,
`assertions.py`, `provenance.py`, `spoke.py`, `stats.py`) remains scheduled for Wave C — see
Sequencing at the bottom.

**Purpose:** make the journey-graph vocabulary **real in code**, so the backbone doc and the
codebase describe the same system in the same words. Companion to
`docs/plans/global-player-journey-graph.md` (the model) and
`summer-league-journey-graph-alignment.md` §5b (why these types, and where OOP stops paying).

## Why this exists

The backbone is currently expressed in a document and in table names, but not in a shared code
vocabulary. The cost is concrete and already paid: §12 of the backbone doc listed the
competition→edition→game model as unbuilt while `summer_league_competitions` *was* an edition
table. Nothing in the code said so.

A named type layer fixes that in both directions — code that says `EditionRef` cannot quietly
drift from a doc that says "edition," and a reviewer can see which backbone layer a service
touches from its signatures alone.

## Design rules

1. **Refs, not entities.** A `*Ref` is a lightweight identity token — canonical id, natural key,
   and enough display text to render — never a mirror of the row. Anything else rebuilds the ORM
   in a second place. Load full rows through services; pass refs.
2. **Dataclasses, not Pydantic.** Per repo convention, Pydantic is reserved for API
   request/response boundaries; internal DTOs are dataclasses. These are internal.
3. **No ORM imports.** The domain layer must not import `app/schemas/`. Adapters map rows → refs
   at the edges. This is what keeps the vocabulary source-agnostic.
4. **Frozen by default.** Value objects are immutable; corrections produce new assertions (§5b of
   the backbone), they do not mutate.
5. **Every type cites its backbone section** in its docstring — the convention already modeled by
   `SummerLeagueParticipation` ("journey-graph §7b").

## Home

A new `app/domain/` package — neither `app/models/` (API shapes) nor `app/schemas/` (tables),
matching the existing split. This also gives the Summer League DTOs currently scattered inside
service modules (`GamesPage`, `ExplorerResult`, `EnvironmentScope`, `DeskTrackerSection`) a proper
home, closing doc #1 Bucket 5.3.

Suggested layout, mirroring the backbone's §3 layering:

```
app/domain/
  identity.py      PlayerRef · SourceIdentity · ResolutionOutcome
  canon.py         OrganizationRef · TeamProgramRef · TeamEntryRef
                   CompetitionRef · EditionRef · GameRef
  assertions.py    Assertion · AffiliationAssertion · Transaction · Measurement
  provenance.py    SourceSystemRef · SourceDocumentRef · SourceRecordRef · Evidence
  spoke.py         ParticipationRef · Coverage
  temporal.py      Watermark · VersionStamps · Scope
  stats.py         StatInputs · PoolContext · Capability     (doc #2)
```

---

## The catalog

### Identity hub (§6, §10)

| Type | Encodes | Carries |
|---|---|---|
| `PlayerRef` | the canonical player token passed everywhere | `player_id`, `slug`, `display_name` |
| `SourceIdentity` | a source's own view of a player *before* resolution | `source_system`, `external_id`, `raw_name`, optional birthdate/team hints |
| `ResolutionOutcome` | the §13.6 three-band result | `status` (AUTO_ACCEPTED / REVIEW_QUEUED / UNRESOLVED), `confidence`, `method`, `model_version`, candidate refs |

`ResolutionOutcome` is deliberately a first-class type: precision-first resolution (principle 3)
means "unresolved" is a legitimate, renderable state — not a null to be papered over.

### Canon entities (§7a, §7)

| Type | Encodes | Carries |
|---|---|---|
| `OrganizationRef` | corporate/governing body | `org_id`, `org_kind` (§13.3), `name` |
| `TeamProgramRef` | the competitive team/squad — **what affiliations point at** | `team_program_id`, `organization_ref`, `name`, `level` |
| `CompetitionRef` | the recurring series ("NBA Summer League") | `competition_id`, `name` |
| `EditionRef` | one instance of a series (2026 Las Vegas) | `edition_id`, `competition_ref`, `year`, `venue`/`region` |
| `TeamEntryRef` | a team's entry in one edition | `team_entry_id`, `team_program_ref`, `edition_ref` |
| `GameRef` | a game within an edition | `game_id`, `edition_ref`, `tip_at`, `status` |

**The `CompetitionRef` / `EditionRef` split is the point.** Before Phase 4, `SummerLeagueCompetition`
named the parent concept while modelling the child; the class is now `SummerLeagueEdition` (#786),
which is the correct name for what it models — but the parent `Competition` entity it was
conflated with still doesn't exist as its own table, so `CompetitionRef` remains aspirational
until Wave C promotes a real `competitions` table. The two types stay the reason to keep the
distinction unmissable when that lands.

### Assertions (§0, §5b, §5c)

| Type | Encodes | Carries |
|---|---|---|
| `Assertion` | the generic envelope — *what makes a fact correctable* | subject ref, `effective_start`/`effective_end`, `recorded_at`, `superseded_at`, `retracted_at`, `supersedes_id`, `evidence` |
| `AffiliationAssertion` | player belonged to a team/program over an interval (§5b) | `Assertion` + `player_ref`, `team_program_ref`, `status`, `scope`/`type` |
| `Transaction` | a point lifecycle transition (§5c) | `player_ref`, `transaction_type` (the closed §13.1 enum), `effective_date`, `evidence` |
| `Measurement` | an observation at a point in time (§5c) | `player_ref`, `metric_key`, `value`, `unit`, `measured_at`, `method`, `edition_ref` |

The three streams stay **separate types** because §5c's central correction was that observations
must not enter the lifecycle stream. Separate types make that structural rather than remembered.

### Provenance (§10)

| Type | Encodes | Carries |
|---|---|---|
| `SourceSystemRef` | a feed/site/ruleset | `source_system_id`, `key`, `version` |
| `SourceDocumentRef` | an ingestion snapshot (the fetched artifact) | `document_id`, `source_system_ref`, `fetched_at`, `sha256`, `location` |
| `SourceRecordRef` | a row within a document | `record_id`, `document_ref`, `locator` |
| `Evidence` | links an assertion to supporting source records | record refs, `resolution_method`, `model_version`, `confidence` |

This replaces SL-coded `source` strings like `"nba_summer_league_roster"`. Per §10, **confidence
belongs to the assertion or resolution decision, not the underlying row** — `Evidence` is where it
lives, which is what lets the graph carry conflicting claims honestly.

### Spoke (§7b, §7c)

| Type | Encodes | Carries |
|---|---|---|
| `ParticipationRef` | the `(player, team_entry, stint)` bridge game logs reference | `participation_id`, `player_ref`, `team_entry_ref`, `stint_no` |
| `Coverage` | completeness of a participation's data (§7c) | `level` (FULL / PARTIAL / BOX_ONLY / RAW_ONLY), `pbp_available`, `games_covered`/`games_expected` |

`Coverage` as a type is what lets "4 of 7 group games" render as *intentionally* partial rather
than as a silent gap — principle 3's honest-gaps requirement, made concrete.

### Temporal & versioning (doc #2 §5, doc #3 §1)

| Type | Encodes | Carries |
|---|---|---|
| `Watermark` | the doc #3 freshness contract | `source_as_of`, `projection_built_at`, `projection_version` |
| `VersionStamps` | the three stamps that must never be conflated | `version`, `registry_version`, `calculation_version` |
| `Scope` | a comparison population | `scope_key`, `scope_kind` |

`Watermark` is the type that makes doc #3's core invariant checkable: *every user-visible
assertion on a card carries the same watermark.* When the projection returns `Watermark` alongside
its payload, mixing provenances requires deliberately constructing two — visible in review rather
than emergent.

`VersionStamps` mirrors `environment_profiles`' existing three-stamp discipline (doc #2 §5) and is
the value-object half of the `DatedVersionMixin` in alignment doc §5b — the same rule expressed
once for tables and once for values.

### Stats (doc #2)

| Type | Encodes | Carries |
|---|---|---|
| `StatInputs` | neutral box/rate inputs — today's `Box`, lifted | minutes, makes/attempts by type, rebounds, assists, turnovers, possessions, optional PBP counts |
| `PoolContext` | league/competition-relative context — today's `LeagueContext` | pool rates, pace, calibration eligibility |
| `Capability` | which canonical inputs a source provides | provided input keys; `computable = metric.requires ⊆ provides` |

---

## Keeping doc and code aligned

The vocabulary only stays aligned if drift is *visible*. Three mechanisms, all cheap:

1. **Bidirectional mapping.** The backbone doc's §3 layer diagram gains a "where it lives in
   code" column; every domain type's docstring cites its backbone section. Drift then shows up
   from either side.
2. **The domain package is the index.** `app/domain/`'s module layout mirrors §3's layers, so the
   architecture is legible from the file tree.
3. **Review question.** For any new table or service: *which backbone layer is this, and which
   domain types does it speak?* A structure that cannot answer is either misplaced or a genuinely
   new layer that the backbone doc should record.

## Sequencing

Wave A of the alignment doc — these are free, encode decisions already made, and make later waves
mechanical. Practical order:

1. `temporal.py` (`Watermark`, `VersionStamps`, `Scope`) — needed by docs #2 and #3 immediately,
   and the highest-value types since they encode P2 and the freshness contract. **✅ SHIPPED
   2026-08-03 (#780).**
2. `stats.py` — lands with the engine extraction (doc #2 Issue A). **Not yet built** — the stat
   engine (doc #2) shipped in Phase 2 without a corresponding `app/domain/stats.py`; `StatInputs`
   and friends still live in `app/services/stats/`. Remains scheduled, no committed date.
3. `identity.py`, `spoke.py` — describe what already exists; adopting them is mostly renaming at
   call sites. **Not yet built.** Scheduled for Wave C.
4. `canon.py`, `provenance.py`, `assertions.py` — introduce alongside the org model and canon-entity
   promotion (alignment doc Waves B/C), where they gain real second consumers. **Not yet built.**
   Wave B (the org model) shipped 2026-08-03 without these — the decision was to hold them for
   Wave C alongside canon-entity promotion rather than introduce them ahead of a second consumer.
   Scheduled for Wave C, deliberately deferred (not next-up).

**Adopt incrementally at the seams**, not in a sweeping refactor: use the types in new and
touched code paths first. A big-bang vocabulary migration is exactly the kind of broad change this
retrospective is trying to make unnecessary.
