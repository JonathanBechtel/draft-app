# North-Star Architecture

**Status:** Canonical principles. Short by design — the detail lives in the docs mapped below.

**This is the permanent home of the architectural principles.** Other documents reference them;
they are not restated or amended elsewhere.

---

## The one idea

> **Keep one durable canonical record. Everything users see is a thin, disposable projection
> computed *from* that record through *one* code path, each carrying an explicit watermark. Retain
> history by default.**

Every significant defect in the Summer League retrospective was a projection drifting — from the
record, or from another projection. A metric disagreeing with the prose beside it. A freshness
badge disagreeing with the data under it. A live overlay disagreeing with the snapshot it patched.
The same statistic computed eight ways.

And every reuse win is the same move in the other direction: if all sources feed one record, and
one engine reads it, a second competition and a richer Explorer both come nearly for free.

**The root cause of the failures and the enabler of the reuse are the same architectural
decision.**

---

## Principles

### P1 — One canonical record; projections are thin readers

Canonical **assertions** (identity, affiliations, participation, stats — each with provenance) are
the durable record. Everything else — timelines, lifecycle, desk views, leaderboards, render
snapshots — is a **replaceable projection** computed from them.

- One computation lives in **one** place; every surface calls it.
- One fact is stored in **one** place; no "fast read" copies that can drift.
- Every projection carries a **watermark** identifying the source state it reflects.

### P2 — Longitudinal-first: retain history by default

Anything carrying analytical or evidentiary value is materialized **append-only and as-of-dated**,
with an atomic current-version pointer. **"Wipe clean and recompute" is an anti-pattern**, not a
shortcut — it destroys the time axis, which is the product's compounding asset.

The actionable line is **evidence vs. cache**:

| Data kind | Example | Rule |
|---|---|---|
| **Canonical facts (assertions)** | game logs, affiliations, participation | Append-only, bitemporal; **never** destroyed |
| **Time-varying analytical projections** | player advanced lines, cohort baselines, environment profiles | **Dated version-flip; history retained** |
| **Pure regenerable presentation caches** | render-snapshot variants | Overwrite-in-place OK — but must stamp the **watermark of what they render** |

Three version stamps travel together and are never conflated: `version` (publication sequence),
`registry_version` (formula/metric definition), `calculation_version` (pipeline logic). Without
that split you cannot answer *"did this number move because new data arrived, or because we
changed the formula?"* — and a series whose points came from silently different formulas is not a
series, it is a trap.

### P3 — Sources are adapters, never stores

Every data source — Summer League, FIBA, AAU, college, NBA — is an **adapter** whose only job is to
translate its raw feed into canonical assertions on the shared backbone. No source keeps a parallel
store. Everything downstream is generic and source-blind.

This is what makes each new dataset *deepen* the moat rather than sit beside it.

### P4 — Freshness means source currency, never process time

A user-facing "as of" answers *"how current is the information?"* — not *"when did our job run?"*
Process/scheduler timestamps are operational telemetry and never surface to users. When currency
cannot be established, the product **degrades visibly** rather than rendering attractively over
stale data.

---

## Build practices

Rules this retrospective earned. They are about *how* to change the system, not what it contains.

**Abstract from consumers, not producers.** An abstraction whose shape is dictated by a consumer
that exists **today** is safe at N=1. An abstraction generalized from a single producer is a guess
wearing a framework's clothes. The Event Desk is framework-shaped with one instance; the stat
engine's neutral inputs are defined by the engine's real requirements. Same instinct, opposite
risk.

**Promote, don't rebuild.** When a structure already exists and has survived production contact,
lift it rather than designing a generic equivalent from scratch. Proven-and-namespaced beats
clean-and-hypothetical.

**Keep the hub thin and the spokes fat.** Only the shared glue must be clean. Source-specific
normalization, provider clients, and event detail belong in their spoke and should stay there.

**Prefer composition to inheritance.** Shared columns via mixins, shared behavior via protocols.
No polymorphic ORM hierarchies across domains — a uniform core plus a thin per-source extension.

**Encode principles as types.** A mixin or value object that makes the correct shape the default
outperforms a principle someone must remember. P2 was violated partly because nothing in the code
made the rule visible.

**Operational acceptance does not defer past merge.** For features whose main risks are
operational, deferring the operational proof *is* the defect. Large test counts and high patch
coverage are not evidence of correct behavior across real schedules, real provider lag, and real
deploys.

**Partition by latency class.** Work with different latency profiles must not share a critical
section. A slow backbone job must never be able to starve a fast user-facing one.

---

## The map

| Doc | Answers |
|---|---|
| **this doc** | the principles, and how the pieces fit |
| `global-player-journey-graph.md` | **the data model** — hub-and-spoke, assertions vs. projections, identity/affiliation/participation. The canonical backbone; includes a where-it-lives-in-code table |
| `journey-graph-domain-vocabulary.md` | **the shared vocabulary in code** — value objects per backbone layer |
| `summer-league-journey-graph-alignment.md` | **how Summer League maps onto the backbone** — promotion plan, namespacing, domain types |
| `summer-league-stat-engine-reuse-spec.md` | **the compute layer** — one source-agnostic engine, metric registry, capability model, dated materialization |
| `summer-league-desk-simplification-spec.md` | **the operational layer** — freshness contract, latency partitioning, verification bar |
| `summer-league-simplification-backlog.md` | **the work queue** — prioritized redundancy removal, sequencing, app-wide anti-pattern audit |
| `programmatic-code-discipline.md` | **the enforcement layer** — AST checkers, runtime guards, and import contracts that make these principles automatic under deadline pressure |
| `summer-league-desk-history.md` | **the failure record** — what happened and why. Required context before further Desk work |
| `player-longitudinal-evidence-layer-pitch.md` | **the product payoff** — the Player Development Ledger the backbone makes possible |

---

## Where things stand

- **The backbone is further along than it looks.** Participation and supersession-first
  affiliations have shipped. The canon-entity and provenance layers exist too — namespaced under
  `summer_league_`.
- **One blocker gates the second spoke:** the organization → team/program model. Until it ships,
  `player_affiliations.team_program_id` stays reserved and no non-NBA source can assert an
  affiliation at all.
- **One anti-pattern violates P2** in an otherwise longitudinal-first codebase: the Summer League
  metrics rebuild destroys history on every run.
- **One subsystem needs simplification before generalization:** the Event Desk — good product,
  fragile plumbing, framework-shaped at N=1.

---

## Review questions for new work

For anything net-new or data-shaped (mechanical fixes are exempt):

1. **Which backbone layer is this**, and which domain types does it speak?
2. **Does it feed the canonical record, or start a parallel store?** (P3)
3. **Is anything computed here already computed elsewhere?** (P1)
4. **Does it retain history, or overwrite it?** If it overwrites — is it genuinely a regenerable
   cache? (P2)
5. **What watermark does it carry, and does everything it displays share one?** (P4)
6. **Is any abstraction here shaped by a real consumer, or generalized from one producer?**
7. **What is the operational proof**, and is it available before merge — not after?
