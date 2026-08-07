# Summer League Desk: Canonical History and Failure Record

**Status:** Historical record, not a remediation plan

**History cutoff:** `origin/main` at `c0555bd` (merged PR #652), 2026-07-21 UTC

**Production observation cutoff:** 2026-07-21 01:27 UTC

**Purpose:** Preserve what the Desk promised, what shipped, what failed, what each
subsequent change attempted, and what production proved afterward.

This document exists because the Desk accumulated patches faster than the project
accumulated a reliable explanation of the system. It is intended to be required context
before another implementation attempt. It does not assign blame to a person or agent, and
it does not claim that another patch is the answer.

## How to read this record

The record distinguishes among:

- **Documented fact:** directly supported by a committed specification, issue, pull
  request, commit, code path, deploy record, or production log.
- **Production observation:** what the deployed application or its scheduled machines
  actually did at a recorded time.
- **Inference:** a conclusion drawn from several facts. Inferences are labeled and should
  not be mistaken for a root cause proven by instrumentation.
- **Unresolved question:** something the available history cannot establish.

“Passed tests” means only that the recorded checks passed. It does not mean that the
production behavior was correct. Conversely, this record does not allege that the checks
were fabricated; it shows that they repeatedly failed to exercise the operational and
cross-path semantics that users encountered.

## Executive summary

The Summer League Desk was conceived as a small, trustworthy, hourly companion built on
existing Summer League ingestion. It became a large distributed feature spanning schedule
discovery, a state machine, historical normalization, live scoring, player identity,
advanced metrics, narrative generation, persisted render variants, homepage reads, two
scheduled Fly machines, and deployment workflows.

The first implementation merged as a 104-file, roughly 35,000-line change. Within hours,
production and pre-production work exposed an inert bootstrap window, a circular schedule
dependency, invalid hero selection, blank rows, fact-chip overload, missing pagination,
stale cron images, stopped cron machines, lock contention, incomplete advanced metrics,
stale cached tracker variants, incorrect rate fallbacks, stale live snapshots, prose/value
contradictions, hour-plus cron starvation, identity-resolution calls inside the writer
lock, and missing live scores when the upstream game-log source lagged.

The recurring problem was not one isolated bug. The system had several competing notions
of “current”:

1. canonical source data written by ingestion;
2. hourly Desk projections and persisted render snapshots;
3. request-time state resolution;
4. request-time live overlays;
5. narrative text generated on a different cadence;
6. a freshness timestamp that could advance during an off-window no-op.

Later changes often repaired one path while leaving another path on a different clock.
That is how a page could show a newly refreshed Game Score beside prose describing an old
Game Score, or show a fresh “as of” badge over content whose underlying data had not been
updated.

The process amplified the technical risk. The initial project merged while its master
coordination issue, original QA gate, production deployment ticket, and production-like
performance-verification ticket remained open. Later starvation work merged while the
manual/staging concurrency proof and post-deploy production verification were explicitly
deferred. Large test counts and high patch coverage supplied confidence about isolated
behaviors, but not about the behavior of the real providers, two overlapping cron jobs,
deploy-time machine state, or consistency across all of the Desk's clocks.

## The original contract

The initial planning artifacts were merged in [PR #480](https://github.com/JonathanBechtel/draft-app/pull/480)
on July 5, followed by the pinned behavior and QA artifacts in
[PR #521](https://github.com/JonathanBechtel/draft-app/pull/521) on July 8.

The [product pitch](plans/summer-league-scouts-desk-pitch.md) and
[behavior specification](plans/summer-league-scouts-desk-behavior-spec.md) promised:

- one automatically selected Morning, Live, or Ledger state, plus Class Tracker;
- a useful July 9 launch built on the existing hourly ingestion cadence;
- no new pipeline required for V1;
- an honest freshness stamp representing the last successful tick in Eastern Time;
- a predictable next-update estimate;
- deterministic, source-grounded commentary rather than editorial invention;
- complete disappearance outside the competition window;
- simple, trustworthy reads derived from the existing Summer League backbone.

The [QA checklist](plans/summer-league-scouts-desk-qa-checklist.md) and
[test plan](plans/summer-league-scouts-desk-test-plan.md) made the freshness promise more
specific: the stamp should equal the actual last successful tick, and stale execution
must not fabricate a time.

Those promises are the baseline for this history. Later behavior should not be judged only
against the most recent patch; it should be judged against this original product contract.

## Chronology

### July 5–8: concept, specification, and orchestration

- **PR #480 — planning artifacts.** Defined the Desk as a compact daily companion and
  assumed the existing hourly ingestion was sufficient.
- **PR #521 — behavior framework and QA.** Pinned the three-state model, deterministic
  commentary, state resolution, freshness semantics, and verification plan.
- **Issue #500 — master project.** The coordinating implementation issue remained open
  after the feature shipped.
- **Issue #510 — original QA gate.** The end-to-end QA issue remained open after the
  feature shipped.
- **Issue #536 — deployment.** The ticket required a human checkpoint for real
  staging/production provisioning and confirmation. It remained open.
- **Issue #548 — performance.** Production-like `EXPLAIN` and latency verification were
  deferred to a human and remained open.

This matters because the unfinished issues were not optional cleanup. They represented the
integrated acceptance, deployment, and production-performance proof for a feature whose
main risks were integrated, operational, and production-specific.

### July 9–12: the initial build

The work introduced the core pieces over a sequence of commits:

- `fdb180f`: tick orchestration;
- `cf77d59`: read service and homepage integration;
- `cdf2a4c`: state-specific Desk UI;
- `5c66e11`: Class Tracker;
- `7f28470`: pre-anchor scoreboard bootstrap;
- `0944a61`: canonical schedule source;
- `0f40745`: render-snapshot persistence;
- `f5d389b`: debut and Ledger percentile logic;
- `77acff6`: live steps and status resolution;
- `491b9ab`: realized deviation and running lines;
- `8b78588`: readiness-gated deployment;
- `8944684`: operational state and freshness;
- `96c3ca9`: 72 materialized snapshot variants;
- `859e7ca`: bulk-loaded tick and a homepage Desk query budget.

The implementation merged in [PR #526](https://github.com/JonathanBechtel/draft-app/pull/526)
on July 12 as commit `210b269`. The pull request reported 2,528 passing tests, 97% patch
coverage, clean pre-commit and mypy checks, cross-ticket end-to-end coverage, mobile QA,
and no critical or major test-quality findings. Production-like query-plan verification
was still deferred.

The merge was unusually broad:

- 104 files changed;
- 35,018 insertions and 39 deletions;
- 21 implementation tickets combined into one delivery;
- new schemas, migrations, services, state machine, tick orchestration, read model,
  ingestion behavior, templates, CSS, JavaScript, scheduled deployment, and tests.

That scale is not itself proof of a defect. It is material historical context: no small
follow-up could validate every interaction introduced in the same change, and failures
subsequently appeared at nearly every boundary the pull request crossed.

### July 12: the first hours after merge

- [PR #563](https://github.com/JonathanBechtel/draft-app/pull/563) populated the
  competition date window. Without it, the opening-morning bootstrap was inert.
- [PR #564](https://github.com/JonathanBechtel/draft-app/pull/564) documented launch-day
  data dependencies that the original “existing pipeline” assumption had obscured.
- [PR #565](https://github.com/JonathanBechtel/draft-app/pull/565) redesigned the recap
  around top-performer cards.
- [PR #568](https://github.com/JonathanBechtel/draft-app/pull/568) moved schedule refresh
  into ingestion. The initial design had a circular bootstrap dependency: the Desk needed
  schedule data to leave dormancy, but schedule fetching lived inside the Desk flow and was
  gated by the Desk not being dormant.
- [PR #567](https://github.com/JonathanBechtel/draft-app/pull/567) changed Class Tracker
  tabs from full homepage reloads to fragment fetches.

The circular schedule dependency is the earliest clear example of an implementation that
worked in isolated state-machine tests but could not initialize itself under the real
ordering of production data.

### July 13: product correctness and content-quality failures

- [PR #569](https://github.com/JonathanBechtel/draft-app/pull/569) addressed roughly 40
  cryptic fact chips, blank scheduled rows, and missing tracker pagination.
- [PR #570](https://github.com/JonathanBechtel/draft-app/pull/570) prevented a DNP roster
  shell from taking the hero and prevented an in-progress game with no box-score data from
  being presented as the headline performance.

These were not cosmetic details. They affected whether the product communicated useful,
credible basketball information at all. The original tests proved that components could
render; they did not prove that the selected subjects and generated facts would be sensible
with sparse or partially arrived production data.

### July 14–16: deployment state, contention, and analytics drift

- [PR #574](https://github.com/JonathanBechtel/draft-app/pull/574) fixed routine deploys
  leaving the Desk cron on an old image because its update was gated by
  `enable_desk_cron=false`.
- [PR #579](https://github.com/JonathanBechtel/draft-app/pull/579) addressed an active
  deadlock between ingestion and the Desk while source players were being normalized. A
  deploy had also interrupted a running Desk tick. The change added advisory-lock
  coordination and repaired several live-presentation edge cases.
- [PR #580](https://github.com/JonathanBechtel/draft-app/pull/580) repaired the advanced
  metrics refresh. Production had zero complete Las Vegas games even though more than 150
  players were otherwise eligible.
- [PR #587](https://github.com/JonathanBechtel/draft-app/pull/587) repaired deploy behavior
  after `--skip-start` left the scheduled machine stopped. Four production deploys left the
  cron idle for more than eight hours, and the homepage remained stale until manual restart.
- [PR #586](https://github.com/JonathanBechtel/draft-app/pull/586) refreshed stale Class
  Tracker render variants after rebuilding advanced metrics.
- [PR #589](https://github.com/JonathanBechtel/draft-app/pull/589) stopped pool-calibration
  incompleteness from hiding otherwise available player rates.
- [PR #592](https://github.com/JonathanBechtel/draft-app/pull/592) switched to NBA-source
  rates after local fallback calculations proved incorrect with incomplete team Advanced
  rows.

This sequence revealed three independent stale-data mechanisms: the cron could run an old
image, the cron machine could be stopped, and fresh source/metric data could exist while a
materialized presentation variant remained old.

### July 17–18: recovery state and live/snapshot divergence

- [PR #602](https://github.com/JonathanBechtel/draft-app/pull/602) added persisted
  deferrals, recovery state, and telemetry for cron coordination. Its associated incident
  described long external fetches and transactions causing silent multi-cycle staleness.
- [PR #603](https://github.com/JonathanBechtel/draft-app/pull/603) added PER to Class
  Tracker.
- [PR #604](https://github.com/JonathanBechtel/draft-app/pull/604) overlaid current
  canonical data onto stale persisted Desk snapshots at request time, repaired identity
  links, reselected subjects, and refreshed minute-sensitive live values. The pull request
  records that full integration files were stopped after approximately 11 and 20 minutes,
  while the targeted new test passed.

The overlay made selected numbers more current without making the entire render projection
current. It therefore introduced another freshness boundary: values could now be generated
at request time while the surrounding prose and selection rationale remained products of
the older tick.

### July 19–20: starvation incident and the second large refactor

The committed [starvation specification](plans/summer-league-cron-desk-starvation-spec.md)
records the July 19 incident:

- full ingestion began around 03:09 UTC;
- the venue phase took 5,261,671 ms, approximately 87.7 minutes;
- it processed 10,155 shot events across 70 games before continuing into play-by-play;
- the ingestion connection remained idle in a transaction while holding the advisory lock;
- the Desk was blocked for approximately 79 minutes;
- once the lock cleared, the Desk itself completed in approximately 38 seconds.

The investigation identified a venue-wide transaction and lock, row-by-row identity work,
repeated flushing/upserts, whole-venue replay, blocking acquisition, and deploy-image drift
without automatic reconciliation.

- [PR #620](https://github.com/JonathanBechtel/draft-app/pull/620) repaired a visible
  contradiction reported as recurring in production: request-time GmSc values refreshed,
  while “read at this tick” prose remained frozen at the hourly value.
- [PR #634](https://github.com/JonathanBechtel/draft-app/pull/634) implemented the main
  starvation project across 36 files with 7,360 insertions and 247 deletions. It reported
  1,732 unit tests, real-Postgres integration coverage, greater than 200x reduction in one
  measured critical section, and 94% patch coverage.
- The manual/staging test under real concurrent ingestion and the post-deploy production
  digest verification required by the QA ticket were explicitly deferred.
- [PR #647](https://github.com/JonathanBechtel/draft-app/pull/647) closed a gap left by the
  starvation work: Gemini candidate search for identity resolution still ran while the
  writer lock was held. The pull request identifies it as another contributor to July 19
  starvation.

The second large refactor reduced a measured lock duration, but it did not receive the very
production-like concurrency proof that defined its user-visible acceptance criterion:
fresh Desk updates while a long ingestion run is actually occurring.

### July 20–21: missing live rows and source fallback

[Issue #633](https://github.com/JonathanBechtel/draft-app/issues/633) documented a game
that remained scheduled with null scores and no player logs after tip. PR #604's request-time
overlay could refresh existing canonical rows, but it could not create source rows that had
never arrived.

[PR #652](https://github.com/JonathanBechtel/draft-app/pull/652) added a TeamStats live-score
fallback because the LeagueGameLog source lagged. A follow-up commit, `b9a6312`, was needed
to keep the fallback from overwriting existing scores. PR #652 is the history cutoff for
this document; it had merged but was not part of the deployed image observed below.

## Production observation: July 20 stale and internally inconsistent Desk

This was a read-only observation of production, not a modification.

At the time of inspection:

- production release `v169` had been created at 2026-07-19 22:39:24 UTC and used image
  commit `bba2986`;
- the latest substantive Desk tick visible in logs completed at
  2026-07-20 02:46:19 UTC (July 19, 10:46 PM Eastern), grading 374 players and writing 72
  render variants;
- at 2026-07-21 00:52:37 UTC (July 20, 8:52 PM Eastern), an off-window/dormant run reported
  a no-op but still materialized 72 variants under a force-mode override;
- the homepage then displayed “as of 8:52 PM ET” and “next update ~9:52 PM ET,” even though
  the substantive Desk content was approximately 23 hours old;
- ingestion began another large run around 01:15 UTC, found 76 games and 380 files, and was
  still issuing identity/embedding requests after 01:27 UTC;
- another Desk tick began around 01:18 UTC and had not completed at the 01:27 UTC
  observation cutoff; the displayed next-update estimate was 01:52 UTC.

The supplied homepage capture also showed an internal contradiction on the featured card:

- the visible metric showed Jahmi'us Ramsey at **21.4 GmSc** with 20 points;
- the sentence below said **“His 7.9 Game Score ranks 210th of 488…”**

That is the same defect class PR #620 claimed to repair: the numerical display and its prose
were produced from different freshness paths.

### Why the freshness badge advanced

The current tick implementation describes off-window execution as inert, but the dormant
path still invokes the generic Event Desk controller. That controller writes
`freshness_tick_at = now` and `next_tick_eta = now + interval`; the tick then materializes
snapshot variants under a force-mode override and summarizes the execution as a no-op.

This means the visible freshness marker can represent **the time a scheduler/controller ran**,
not **the time the underlying Desk data successfully became current**. That behavior
contradicts the original behavior specification and QA requirement that freshness reflect
the last successful data tick and never fabricate currency.

Relevant implementation paths at the history cutoff:

- [`app/cli/sl_desk_tick.py`](../app/cli/sl_desk_tick.py)
- [`app/services/event_desk/controller.py`](../app/services/event_desk/controller.py)
- [`app/services/sources/summer_league/desk_read.py`](../app/services/sources/summer_league/desk_read.py)
- [`app/services/sources/summer_league/desk_commentary.py`](../app/services/sources/summer_league/desk_commentary.py)

## Recurring failure modes

### 1. “Freshness” had no single product meaning

The system used one user-facing concept to describe several different events:

- source ingestion completed;
- the Desk projection tick completed;
- render variants were materialized;
- a request-time overlay refreshed some values;
- a dormant scheduler/controller invocation completed.

**Inference:** The absence of separate, explicit watermarks made stale content appear fresh
and made readiness checks capable of passing when the product had not actually updated.

### 2. Multiple clocks produced one card

Selection, headline metrics, cohort ranks, prose, game status, and the displayed timestamp
could originate from different layers and times. PRs #586, #604, and #620 each corrected a
different manifestation of this problem.

**Inference:** As long as a single user-visible assertion is assembled from independently
refreshed projections without a shared version/watermark, local repairs can continue to
create cross-field contradictions.

### 3. The bootstrap path was circular

Schedule data determined whether the Desk was active, but schedule acquisition originally
lived behind the state that required schedule data. PR #568 moved refresh responsibility
to break that cycle.

**Inference:** State-machine unit tests began from already populated fixtures and therefore
did not represent a real launch from empty or lagging production data.

### 4. Live presentation depended on non-live work

The hourly product's critical path became coupled to full historical file discovery,
shot/play-by-play normalization, player identity resolution, external Gemini requests,
advanced-metric rebuilds, snapshot generation, and tracker projection.

**Inference:** The architecture treated all backbone maintenance as one freshness domain.
An expensive or lagging historical task could therefore starve the much smaller job needed
to keep the homepage credible.

### 5. Deployment success did not imply scheduled execution

Production experienced both an old cron image and a stopped cron machine. A healthy web
deployment was insufficient evidence that the scheduled system was running the same code,
running at all, or meeting its cadence.

### 6. Missing or partial provider data was treated as an edge case

Production repeatedly supplied legitimate intermediate states: scheduled games after tip,
scores without player logs, player shells with no minutes, incomplete team Advanced rows,
and one endpoint lagging another. These states produced false live labels, invalid heroes,
hidden metrics, incorrect fallback rates, or absent rows.

**Inference:** Test fixtures mostly represented complete snapshots or hand-selected missing
fields, not the asynchronous arrival order and disagreement of real NBA endpoints.

### 7. Patches added alternate paths faster than they removed ambiguity

Schedule refresh became “belt-and-suspenders.” Live values gained request-time overlays.
Rates gained fallbacks and later preferred-source behavior. Live scores gained a second
provider fallback. Render snapshots could be force-materialized outside the active window.

Some redundancy is defensible, but each additional path created precedence, provenance,
and timestamp questions. The history does not show a corresponding simplification into one
authoritative read contract.

### 8. Verification emphasized volume over environmental fidelity

The two largest delivery PRs reported thousands of passing tests and high patch coverage.
Nevertheless, the following escaped:

- startup from an unpopulated competition window;
- cron overlap lasting longer than the update interval;
- external identity requests inside a writer lock;
- deploys leaving a scheduled machine stopped or stale;
- provider endpoints arriving in different orders;
- persisted prose disagreeing with request-time metrics;
- a no-op advancing user-visible freshness.

**Inference:** The tests were strongest at local behavior and weakest at temporal semantics,
process lifecycle, real-provider lag, and consistency across layers.

### 9. Operational acceptance was deferred past merge

The original master QA/deploy/performance issues remained open. PR #526 deferred
production-like query verification. PR #634 deferred the real concurrent-ingestion test
and the post-deploy production digest check.

This is a process fact, not a speculation about code quality: the evidence needed to prove
the most failure-prone properties did not exist before the relevant changes were declared
ready.

### 10. Complexity grew beyond a reviewable unit

At the history cutoff, just six central Desk files total approximately 7,677 lines:

| File | Approximate lines |
|---|---:|
| `app/services/sources/summer_league/desk_read.py` | 2,597 |
| `app/cli/sl_desk_tick.py` | 1,404 |
| `app/services/sources/summer_league/desk_storylines.py` | 1,358 |
| `app/services/sources/summer_league/desk_commentary.py` | 916 |
| `app/services/sources/summer_league/desk_facts.py` | 727 |
| `app/services/sources/summer_league/desk_fact_queries.py` | 675 |

This excludes the generic Event Desk framework, schemas, migrations, ingestion services,
templates, CSS/JavaScript, deployment workflows, and tests.

**Inference:** Reviewers and agents were asked to reason about a distributed temporal system
through files and pull requests too large to hold as one reliable mental model. Subsequent
changes were therefore likely to validate their local invariant while missing a distant
consumer.

## Product consequences

The failures were not merely operational inconvenience:

- **False currency:** a recent “as of” time implied that old content was current.
- **Internal contradiction:** the same card could state two incompatible Game Scores.
- **Incorrect emphasis:** DNP shells or games without box data could become the hero.
- **Broken expectations:** the “hourly companion” missed multiple update intervals.
- **Opaque degradation:** the page often rendered attractively instead of clearly stating
  that its source data, projection, or narrative was stale.
- **Trust erosion:** every visible contradiction made the next apparently correct update
  less believable.

For this product, freshness and internal consistency are not polish. They are core
correctness requirements because the Desk's value proposition is a trustworthy summary of
what is happening now.

## Technical consequences

- The write lock became a coordination point for unrelated workloads with very different
  latency profiles.
- Projection, snapshot, and request-time overlay layers obscured data provenance.
- Readiness could reflect process execution rather than useful data availability.
- Recovery required manual inspection and restart in incidents that should have been
  visible as explicit degraded state.
- Each fallback expanded the state space future changes needed to test.
- Service files and orchestration code grew large enough that isolated refactoring tickets
  became necessary merely to make future reasoning tractable.

## What future work must not assume

This section is a historical guardrail, not an instruction to implement another fix.

Any future proposal must begin without assuming that:

- a successful cron exit means the Desk data changed;
- a fresh snapshot timestamp means its source rows are fresh;
- a request-time metric and its sentence share the same inputs;
- the NBA endpoints agree or arrive atomically;
- schedule data exists before state resolution;
- ingestion completes within the nominal hourly interval;
- the deployed web image matches the scheduled-machine image;
- a running Fly machine has executed recently;
- unit/integration fixtures reproduce production arrival order;
- high patch coverage proves time-dependent system behavior;
- a fallback is safe merely because it fills a missing value;
- an open QA, deployment, or performance gate can be treated as post-launch cleanup.

Before anyone claims that the Desk is recovered, the evidence must identify, separately:

1. the source-data watermark;
2. the projection/snapshot watermark;
3. the provenance and watermark for every displayed number and sentence;
4. the scheduled job's last start, completion, outcome, image, and useful data change;
5. behavior across several real overlapping ingestion and Desk cycles;
6. behavior when each upstream source is late, partial, contradictory, or absent;
7. the exact degraded-state presentation when those checks fail.

A screenshot of one correct render, one manually forced tick, a targeted test, a clean CI
run, or a large test count is not sufficient evidence for those claims.

## Unresolved questions at the cutoff

- How many production cycles, before and after the recorded incidents, served a freshness
  time newer than their substantive source data?
- Which fields on each Desk state are persisted, overlaid at request time, or regenerated,
  and do they all carry compatible source watermarks?
- Did PRs #634, #647, and #652 receive a production deployment and survive several natural
  overlapping cycles after this observation cutoff?
- What is the intended product behavior outside the competition window: disappear entirely,
  show a final Ledger, or render a force-materialized snapshot? The specification and
  observed behavior are not aligned.
- Which open acceptance issues are still authoritative, and who is responsible for closing
  them with actual evidence rather than implementation claims?
- Is the Desk still intended to be an hourly live product, or should its promise be narrowed
  to a cadence and consistency level the backbone can support?

## Evidence index

### Product and behavior contract

- [Summer League Desk pitch](plans/summer-league-scouts-desk-pitch.md)
- [Summer League Desk behavior specification](plans/summer-league-scouts-desk-behavior-spec.md)
- [Summer League Desk QA checklist](plans/summer-league-scouts-desk-qa-checklist.md)
- [Summer League Desk test plan](plans/summer-league-scouts-desk-test-plan.md)
- [Desk deployment runbook](plans/summer-league-desk-536-deploy-runbook.md)
- [Desk performance verification](plans/summer-league-desk-548-perf-verification.md)

### Starvation investigation

- [Cron/Desk starvation specification](plans/summer-league-cron-desk-starvation-spec.md)
- [Cron/Desk starvation QA checklist](plans/summer-league-cron-desk-starvation-qa-checklist.md)
- [Cron/Desk starvation test plan](plans/summer-league-cron-desk-starvation-test-plan.md)
- [Issue #622](https://github.com/JonathanBechtel/draft-app/issues/622)
- [Issue #623](https://github.com/JonathanBechtel/draft-app/issues/623)
- [Issue #630](https://github.com/JonathanBechtel/draft-app/issues/630)

### Key delivery and remediation pull requests

- [#526 initial implementation](https://github.com/JonathanBechtel/draft-app/pull/526)
- [#563 competition window](https://github.com/JonathanBechtel/draft-app/pull/563)
- [#568 schedule bootstrap](https://github.com/JonathanBechtel/draft-app/pull/568)
- [#569 content and pagination](https://github.com/JonathanBechtel/draft-app/pull/569)
- [#570 hero selection](https://github.com/JonathanBechtel/draft-app/pull/570)
- [#574 stale cron image](https://github.com/JonathanBechtel/draft-app/pull/574)
- [#579 ingestion/Desk contention](https://github.com/JonathanBechtel/draft-app/pull/579)
- [#580 advanced metrics refresh](https://github.com/JonathanBechtel/draft-app/pull/580)
- [#587 stopped cron machine](https://github.com/JonathanBechtel/draft-app/pull/587)
- [#586 stale tracker variants](https://github.com/JonathanBechtel/draft-app/pull/586)
- [#589 tracker rate gating](https://github.com/JonathanBechtel/draft-app/pull/589)
- [#592 source rates](https://github.com/JonathanBechtel/draft-app/pull/592)
- [#602 cron recovery state](https://github.com/JonathanBechtel/draft-app/pull/602)
- [#604 request-time live overlay](https://github.com/JonathanBechtel/draft-app/pull/604)
- [#620 live prose consistency](https://github.com/JonathanBechtel/draft-app/pull/620)
- [#634 starvation refactor](https://github.com/JonathanBechtel/draft-app/pull/634)
- [#647 identity work outside lock](https://github.com/JonathanBechtel/draft-app/pull/647)
- [#652 TeamStats live-score fallback](https://github.com/JonathanBechtel/draft-app/pull/652)

### Open coordination and acceptance issues at the audit point

- [#500 master implementation project](https://github.com/JonathanBechtel/draft-app/issues/500)
- [#510 original QA gate](https://github.com/JonathanBechtel/draft-app/issues/510)
- [#536 production deployment](https://github.com/JonathanBechtel/draft-app/issues/536)
- [#548 production-like performance verification](https://github.com/JonathanBechtel/draft-app/issues/548)
- [#622 starvation refactor tracker](https://github.com/JonathanBechtel/draft-app/issues/622)
- [#623 starvation project specification](https://github.com/JonathanBechtel/draft-app/issues/623)
- [#630 starvation QA gate](https://github.com/JonathanBechtel/draft-app/issues/630)

## Maintenance rule for this history

Future Desk work should append a dated entry only after recording:

- the observed production symptom or explicit product requirement;
- the evidence establishing the cause, with uncertainty labeled;
- exactly which data clock or execution path changed;
- the verification performed in the environment where the failure occurred;
- the result across natural scheduled cycles, not only a forced execution;
- any new fallback, cache, projection, lock, or source-precedence rule introduced.

Do not rewrite earlier incidents to make a later implementation appear inevitable or
successful. Preserve failed hypotheses and superseded fixes: they are the most important
context for preventing another confident local change from producing a new system-level
defect.
