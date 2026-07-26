# Desk Simplification Spec (doc #3)

**Status:** Design spec. Strategy document — no code changes proposed inline.

**Part of the five-doc set** (see `summer-league-simplification-backlog.md` doc #1 for the map).
This doc owns: the freshness contract, latency-class decoupling, the remaining mega-transaction,
layer collapse, and the verification bar the Desk must clear.

**Required reading first:** `docs/summer-league-desk-history.md` — the failure record. This spec
is the forward-looking answer to it and does not restate its chronology.

**Governed by** the principles in doc #1: **P1** (one canonical record; projections are thin
readers) and **P2** (longitudinal-first; destructive rebuild is the exception).

## The product is not in question — the plumbing is

The Desk stays. Its value is established and deliberate:

- the **three-state machine** (Morning / Live / Ledger, plus Class Tracker) is a genuinely
  reusable shape for any multi-day event;
- it gives visitors a reason to **check in several times a day**, which is the strongest retention
  mechanic the product has.

Nothing in this spec proposes narrowing that promise. What follows is entirely about the layers
underneath it, which are what failed. The distinction matters because the postmortem's open
question — *"is the Desk still intended to be an hourly live product?"* — is hereby answered
**yes**, and the plumbing is redesigned to actually support it rather than the promise being
trimmed to fit fragile plumbing.

## Do not re-fight these — already fixed at HEAD

Verified against HEAD `c78af8d`. The postmortem predates all of these; build on them.

| Fixed | Evidence |
|---|---|
| Gemini/embedding calls no longer inside the writer lock — identity resolution split into a lock-free *prepare* pass and small locked write batches (`RESOLUTION_BATCH_SIZE = 8`) | `player_resolution.py:719-723`; `app/cli/summer_league_ingest_runner.py:656-728` |
| The 87-minute whole-venue transaction is chunked — shot/PBP normalization runs `EVENT_BATCH_SIZE = 8` games per `db.begin()`, releasing the lock between batches | `app/cli/summer_league_ingest_runner.py:548-653` |
| Desk lock wait bounded to 30s — the Desk can no longer be starved for an hour; it times out and retries | `sl_desk_tick.py:267,1005-1009` |
| Priority-intent signal — a waiting Desk tick makes the low-priority ingestor back off | `write_lock.py:146-179` |
| Network I/O runs with no transaction open, lock reacquired before writes | `sl_desk_tick.py:1096-1102,1177-1178` |
| "No-op advances freshness" cut — dormant path returns early without invoking the controller or materializing snapshots; `content_refreshed_at`/`next_tick_eta` gated on `content_updated` | `sl_desk_tick.py:1061-1086`; `controller.py:67-78` |

**Implication for planning:** the acute incidents in the failure record are addressed. What
remains is *structural* — the design still permits the same class of defect to recur, because the
number of clocks and the coupling of unrelated workloads were reduced but not eliminated.

---

## 1. The freshness contract

### The problem restated precisely

Six timestamp families still coexist and feed one card:

1. `lifecycle_observed_at` — always `now` (`controller.py:57`)
2. `content_refreshed_at` + `next_tick_eta` — user-facing "as of", gated on `content_updated`
   (`controller.py:67-78`; read at `desk_read.py:540-541`)
3. `source_freshness_tick_at` / `_next_tick_eta` — copied onto each render snapshot
   (`event_desk_render_snapshot.py:119-123`), re-judged against request `now` (`desk_read.py:516`)
4. Request-time live-overlay `now` (`desk_read.py:1135-1240`)
5. Six `summer_league_pipeline_states` process columns (`pipeline_state.py:110-149`)
6. The dormant-tick clock — already cut

**The error is not "too many timestamps."** These are genuinely different events and most deserve
to be recorded. The two real errors are: **(a) the wrong one is shown to users**, and **(b) a
single card is assembled from fields carrying different watermarks.**

### The contract

Exactly **two watermarks are user-facing semantics**, and one bucket is explicitly not:

| Name | Meaning | Derived from |
|---|---|---|
| **`source_as_of`** | The state of canonical basketball data the content reflects — e.g. "complete through the 9:41 PM final" | Max event-time of canonical source rows included in the projection |
| **`projection_built_at`** | When this projection was computed, **paired with the `source_as_of` it read** | The projection builder |
| *operational telemetry* | Job start/completion/outcome/image/whether data advanced | `pipeline_states` — **never user-facing** |

**Three invariants:**

1. **User-facing "as of" shows `source_as_of`, never process time.** This is the single most
   important inversion in this spec. A visitor asks *"how current is the basketball information?"*
   — not *"when did your job last run?"* Today's badge answers the second question, which is
   precisely how a fresh-looking stamp sat over ~23-hour-old content.
2. **Every user-visible assertion on a card — number, rank, prose sentence, status — carries the
   same `(source_as_of, projection_version)` pair.** If a coherent pair cannot be produced, the
   card degrades explicitly (§4) rather than mixing.
3. **"Next update" is honest.** Derived from the schedule *and* the last run's outcome. When a
   run is overdue or failed, the UI says so instead of projecting a confident future time.

### What this retires

- The request-time single-sentence prose rewrite (`desk_read.py:1171-1184`), which patches *one*
  sentence to agree with a live value while the surrounding selection rationale stays tick-aged.
  It is a symptom-level fix for a structural problem; §3 removes the need for it.
- Any user-facing use of `lifecycle_observed_at`. It remains valid telemetry.

---

## 2. Latency-class partitioning

### The problem — stated correctly

**Hourly is the right cadence and is not in question.** The defect is that *the hourly update did
not reliably happen* — and it failed most often **while games were live**.

That correlation is the whole problem. Live games are when ingestion has the most new data to
process, so contention peaks at exactly the moment the Desk is most valuable and its staleness
most visible. The system was least reliable precisely when it mattered most.

Mechanically: the hourly product's critical path is coupled to work with wildly different latency
profiles. The failure record's clearest instance is an ~88-minute venue ingest starving a Desk
tick that itself takes ~38 seconds. Bounded waits (already shipped) stop the Desk from *hanging*,
but they do not make it *land* — it times out and skips the interval, which from the visitor's
side is indistinguishable from being broken.

**So the goal of this section is reliability, not speed.** Partitioning exists to guarantee the
hourly tick completes under peak load. Going faster than hourly is explicitly a non-goal.

### Success metric

> The percentage of scheduled hourly ticks that complete with advanced source data, measured
> **specifically within live-game windows** — not averaged across the day.

A daily average hides the failure: off-peak ticks succeed easily and mask the live-window misses
that are the entire user-visible problem. This metric must be reported for the live window
separately, and it is the acceptance signal for this section.

### The partition

Three producers, three schedules, no shared global writer lock:

| Class | Job | Cadence | Latency budget | Touches |
|---|---|---|---|---|
| **Fast** | Live score/box poller for in-window games only | Minutes | Seconds | A narrow set of live game/box rows |
| **Medium** | Desk projection builder | ~Hourly, or on input change | < 1 min | Reads canonical, writes the Desk projection |
| **Slow** | Backbone ingest — file discovery, PBP/shot normalization, identity resolution, metric rebuild | Hours / off-peak | Unbounded | Broad canonical + materialized metrics |

**Rules:**

- The **fast** path never waits on the slow path. It writes a narrow, well-scoped set of canonical
  rows and takes no global lock.
- The **medium** path is a *pure reader* of canonical data plus a writer of its own projection. It
  should not need the backbone writer lock at all — its only writes are to Desk projection tables
  it exclusively owns.
- The **slow** path may take as long as it needs. Its cost must be invisible to the other two.
- **Identity resolution stays out-of-band** (already true — keep it that way). The live path uses
  whatever identities are already resolved and degrades gracefully for the rest; it never blocks
  on resolution.

The Desk render remains a **pure reader** over the latest coherent projection.

---

## 3. Layer collapse — and why partitioning enables it

### The problem

Today a card can be assembled from: a persisted hourly snapshot, a request-time live overlay that
refreshes selected values, and prose generated on a third cadence. That is three provenances in
one visual assertion.

### Why the splicing exists

Request-time splicing was introduced because **rebuilding the projection was expensive** — it was
entangled with heavy backbone work. Given that constraint, patching a few fields at request time
was a rational move.

**Once §2 decouples latency classes, that constraint disappears.** The Desk projection is small
and cheap to rebuild once it is no longer coupled to ingestion. So:

> **Rebuild the projection; never splice fields onto a stale one.**

If live data is newer than the current projection, the medium path rebuilds a *coherent* snapshot
— new numbers, new selection, new prose, one `source_as_of` — rather than the read path grafting a
value onto old content and rewriting one sentence to match.

This is the structural cure for the entire prose-vs-metric contradiction class. It is not a
promise to be more careful; it removes the mechanism that produces the contradiction.

**Corollary:** the render-snapshot variants remain a legitimate cache (doc #1 Appendix A,
cache-exempt) — but they must stamp the watermark of the projection they render, which they
already do (`source_freshness_tick_at`). Keep that discipline.

---

## 4. Explicit degraded state

The failure record's sharpest product observation: the page *"often rendered attractively instead
of clearly stating that its source data, projection, or narrative was stale."* Opaque degradation
erodes trust faster than visible degradation.

Required behaviors:

- When `source_as_of` is older than a defined threshold, the Desk **says so plainly** — it does
  not render a confident card with a quiet timestamp.
- When a scheduled run is overdue or failed, the "next update" line reflects that rather than
  projecting a fresh estimate.
- When a coherent `(source_as_of, projection_version)` pair cannot be assembled, the card renders
  a reduced but *honest* state rather than mixing provenances.
- Partial provider data is a **normal state, not an edge case** (failure mode #6): games scheduled
  after tip, scores without player logs, player shells with no minutes, incomplete team advanced
  rows, one endpoint lagging another. Each has a defined presentation.

The product rule: **the Desk's value is a trustworthy summary of what is happening now.** A
visibly stale Desk is honest and survivable; a confidently wrong Desk is not.

---

## 5. The remaining mega-transaction

The largest surviving critical section, and the leading structural suspect for transaction
timeouts still observed in production:

`app/cli/summer_league_ingest_runner.py:1308-1348` wraps in **one** `db.begin()` under the writer lock:

1. `rebuild_sl_metrics(db)` — a **full unscoped table wipe** (`metrics.py:1443-1446` deletes all
   of `SummerLeaguePlayerSeason` / `MetricContext` / `MetricModel`), then
2. `materialize_desk_render_snapshots(db)` — 72 variants, then
3. `refresh_environment_profiles_for_year(db, ...)`.

The coupling is stated as an invariant in
`app/services/event_desk/snapshot_materialization.py:1-7`: *"Every full metrics rebuild must
therefore replace the render-snapshot matrix before its transaction commits, or direct stat
surfaces and the homepage can disagree."*

**That invariant is only necessary because the rebuild is destructive.** With the dated
version-flip publish from doc #2 (P2), the sequence becomes:

1. Build the new metrics version **outside** any lock, writing new rows alongside the current ones
   (nobody reads them yet — `is_current` still points at the old version).
2. Materialize the render variants for the new version, still outside the hot path.
3. **Flip the current pointer atomically** — a tiny transaction.

Readers see the old coherent version until the instant they see the new coherent version. Stat
surfaces and the homepage cannot disagree, and no lock is held across the expensive work.

**One change retires three problems** (doc #1): the operational fire, the reproducibility gap, and
the missing longitudinal time axis. This is the highest-value item in this spec and the natural
place to start if the timeouts are still live.

**Also:** stop deleting `SummerLeagueMetricModel`, which is versioned and auditable by design.

### 5a. Observed confirmation and a new amplifier — incident #669 (July 22–23)

After this spec was first drafted, the failure class was observed live, with a corrected
attribution. A production deploy 500ed DB-backed public routes for the duration of a blocking
chain: a ~96-minute **full-ingestion** transaction held the writer lock; the Desk sat *waiting*
on it (the "96-minute Desk transaction" reading was wrong — after acquiring the lock the Desk
resolved dormant and exited normally); the deploy's **non-concurrent `CREATE INDEX`** migration
queued behind the ingestion transaction; and DB-backed public routes 500ed on web-pool
exhaustion. `/health` stayed green throughout because it exercises no database query.

**One mechanism claim needs correction before it hardens into lore.** The incident record
attributes the read outage to reads queuing behind the migration's "requested exclusive lock,"
but a bare `CREATE INDEX` requests a `SHARE` lock — it blocks *writes*, not ordinary reads
(`ACCESS SHARE` is compatible) — and the migration in question (`2c78f642217c`) executes exactly
one `op.create_index`. The observed facts stand (the blocked deploy, the pool exhaustion, the
500s); the precise mechanism by which *reads* backed up is **not established**, and confirming
it is part of the roadmap Phase 1 entry gate.

Three contributing details from the incident record are new obligations for this spec:

1. **The writer lock is transaction-scoped**, so an overly broad transaction is *also* an overly
   broad lock lifetime. The §5 version-flip shrinks both at once — this is now observed
   motivation, not inferred.
2. **Deploy migrations are an amplifier.** Release migrations must set a short `lock_timeout` so
   a blocked migration fails fast instead of camping in the lock queue, and index builds on large
   tables must use `CREATE INDEX CONCURRENTLY` — which in this repo means an
   `op.get_context().autocommit_block()`, since `alembic/env.py` runs migrations inside a
   transaction and PostgreSQL rejects `CONCURRENTLY` there (existing precedent:
   `e7c75f3063ec`). Both hold regardless of which exact lock interaction backed reads up
   (discipline §1.7).
3. **A DB-exercising health signal is required.** A health check that never touches the database
   cannot notice a database outage; §4's degraded states need an operational counterpart that
   actually fails when reads fail.

The incident also corroborates a scoping subtlety: the metrics rebuild's "scoped" mode limits
which projection rows are **persisted**, but `compute()` still loads and recalculates the entire
historical dataset — so scoping bounds neither compute cost nor transaction length. Only the
version-flip does.

---

## 6. Decomposition

Six central Desk files total ~7,677 lines — beyond a reviewable unit, which the failure record
identifies as a root cause of confident local changes producing distant defects.

| File | Lines |
|---|---|
| `app/services/summer_league/desk_read.py` | ~2,623 |
| `app/cli/sl_desk_tick.py` | ~1,439 |
| `app/services/summer_league/desk_storylines.py` | ~1,358 |
| `app/services/summer_league/desk_commentary.py` | ~916 |
| `app/services/summer_league/desk_facts.py` | ~727 |
| `app/services/summer_league/desk_fact_queries.py` | ~675 |

**Sequence matters:** decompose *after* §1 and §3, not before. Splitting files while the layering
is still ambiguous distributes the ambiguity across more files. Once there is one freshness
contract and no request-time splicing, the natural seams are obvious:

- **read/query** (fetch canonical rows) — **project** (compute the Desk view) — **render**
  (shape for templates);
- `sl_desk_tick.py` splits along the §2 latency classes rather than being one orchestrator.

---

## 7. Reusability posture — what to generalize, and what not to yet

The Event Desk framework is currently **framework-shaped at N=1**: every `app/services/event_desk/`
module imports `app.schemas.summer_league`, with one registered event, a bespoke
`_SummerLeagueCalendarProvider`, and hard-coded `SUMMER_LEAGUE_WINDOW_PRIORS`.

- **Do not generalize the framework further in the abstract.** Abstraction validated by one
  implementation is that implementation with extra indirection. The true seams appear when a real
  second event forces them.
- **Do design these generically now**, because they are event-independent and are the parts that
  failed: the **freshness contract** (§1), the **latency-class partition** (§2), and the
  **degraded-state vocabulary** (§4). These carry to any event without guessing.
- The **state machine itself** is the reusable product asset and should be preserved as-is.

When the second event arrives, harvest the abstraction from the pair — do not pre-freeze it.

---

## 8. Verification contract

The failure record is unambiguous that verification, not just code, is what failed: large test
counts and high patch coverage coexisted with defects at nearly every boundary, and operational
acceptance was repeatedly deferred past merge.

**These are acceptance criteria, not suggestions. Evidence must identify, separately:**

1. the **source watermark** — what state of canonical data the content reflects;
2. the **projection watermark** — when it was built and which source state it read;
3. the **provenance of every displayed number and sentence** on a card, and that they agree;
4. the scheduled job's **last start, completion, outcome, image, and whether data actually
   changed**;
5. behavior across **several real overlapping ingestion and Desk cycles** — not one forced tick;
6. behavior when each upstream source is **late, partial, contradictory, or absent**;
7. the exact **degraded-state presentation** when those checks fail.

**Explicitly insufficient as evidence:** a screenshot of one correct render, one manually forced
tick, a targeted passing test, a clean CI run, or a large test count.

### Test-fidelity requirements

Failure mode #8 was that tests were strongest at local behavior and weakest at temporal semantics,
process lifecycle, provider lag, and cross-layer consistency. New tests must cover:

- **bootstrap from empty** — a launch with no populated competition window (the original circular
  dependency);
- **overlapping cycles** — ingestion running longer than the Desk interval;
- **provider disagreement** — endpoints arriving in different orders, one lagging another;
- **cross-layer consistency** — the same fixture asserting number == rank == prose == status all
  carry one watermark;
- **deploy lifecycle** — scheduled machine stopped or on a stale image (both occurred in
  production; both passed web-deploy health checks);
- **browser-executed frontend** — the test suite must *execute* page JS, not assert markup: the
  heat-shading regression (an ES `export` in a classic `<script>` tag) painted zero cells while
  49 unit tests, 121 integration tests, and a QA gate passed, because integration tests only
  asserted the `data-*` attributes existed. A Playwright paint-level check (the repo's
  `make visual` harness) is the floor for any UI-bearing change (discipline §3.5).

### Process rule

**Operational acceptance does not get deferred past merge.** The failure record shows the initial
build merging with its QA, deployment, and performance gates open, and the starvation refactor
merging with its concurrency proof and post-deploy verification deferred. For a feature whose main
risks are operational, that is the decisive process defect — not a code-quality one.

---

## Phasing

Ordered by value and risk. Each phase is independently shippable and verifiable.

**Phase 1 — atomic publish (§5).** Convert the metrics rebuild to a dated version-flip and remove
the mega-transaction. Highest value: retires the live operational risk, the reproducibility gap,
and unlocks longitudinal history. Coordinates with doc #2 Issue B.

**Phase 2 — freshness contract (§1).** Define `source_as_of` / `projection_built_at`, invert the
user-facing badge to show source currency, make "next update" honest. Ship with the degraded-state
vocabulary (§4), since an honest badge is only useful if staleness renders honestly.

**Phase 3 — latency partition (§2).** Separate fast poller / medium projection builder / slow
backbone, so the hourly tick lands reliably during live games. Prerequisite for Phase 4. **This
addresses the most user-visible failure** — missed updates while games were in progress — so it
may warrant moving ahead of Phase 2 if reliability during the next event matters more than badge
honesty.

**Phase 4 — layer collapse (§3).** Remove request-time splicing; rebuild coherent projections
instead. Only possible once Phase 3 makes rebuilds cheap.

**Phase 5 — decomposition (§6).** Split the god files along the now-unambiguous seams.

Phases 1–2 are the ones worth doing soon regardless of what else is scheduled. Phases 3–5 are the
structural work that makes the next event's Desk cheap.

## Product decisions (settled)

1. **Off-window behavior — SETTLED.** Outside the competition window the Desk **disappears
   entirely** and the homepage reverts to its normal news layout. This is the current default and
   it is correct. Implications: when dormant there is **no freshness badge and no next-update
   estimate** — nothing ticks, so nothing can lie. No final-Ledger card, no force-materialized
   snapshot. This also removes any need for the dormant path to touch the controller at all.

2. **Cadence — SETTLED: hourly.** Hourly is the intended promise and is sufficient. Sub-hourly
   near-live updating is **explicitly out of scope**. The work in §2 is aimed entirely at making
   the hourly promise *reliable under peak load*, not at increasing frequency.

## Open questions

1. **Staleness threshold** (deferred — not currently important). At what age does content trigger
   explicit degraded state, and does the threshold differ by state (Live vs Morning vs Ledger)?
   Worth settling when §4 is implemented; a reasonable starting assumption is a tighter threshold
   during Live than during Morning, since nothing has happened yet in the morning.
