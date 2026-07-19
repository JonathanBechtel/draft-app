# Product Direction: Player Longitudinal Evidence Layer

**Status:** Product/data discovery · **Date:** 2026-07-18

**Related directions:**

- `docs/plans/global-player-journey-graph.md`
- `docs/plans/event-environment-intelligence-pitch.md`
- `docs/plans/event-environment-comparison-and-outcomes-framework.md`

## The compounding product capability

As DraftGuru adds combines, college/international seasons, Summer League, and NBA data,
the durable product is not a collection of one-off comparisons. It is a player’s
versioned, time-aware body of evidence:

```text
combine measurement → pre-draft production → Summer League role/performance
                   → NBA year 1 → NBA year 2 → later career evidence
```

Call the public expression of this capability the **Player Development Ledger**. It
allows a visitor to see what was observed at each stop, how that observation compared
with an appropriate peer group, and—where definitions permit—how the same metric family
changed at later levels.

This is an extension of the Global Player-Journey Graph, not a parallel player-history
store. Canonical event facts remain in their domain spokes. The Ledger is a rebuildable
read projection over those facts.

## The product question changes by comparison type

There are two distinct experiences that should not be conflated.

| Experience | Visitor question | Correct output |
| --- | --- | --- |
| **One player across time** | “What did we observe from this player at each stage?” | A sourced timeline with raw facts, sample, role, and appropriate relative context. |
| **Historical cohort to later outcomes** | “What tended to happen next for players like this?” | A distribution, sample count, comparables, and uncertainty—not an individual forecast. |

The first helps a visitor understand a player’s actual journey. The second is the
responsible way to make the journey predictive. A jump from a player’s Summer League
box score to an NBA league average answers neither question well.

## What is comparable—and what is only contextual

The Ledger needs a metric-family registry with explicit comparison semantics. Matching
labels alone are insufficient.

| Metric family | Example | Product treatment |
| --- | --- | --- |
| **Direct measurement** | height, wingspan, weight | Show raw values and a direct delta when source/method are compatible. Retain measurement date and method. |
| **Shared-rate metric** | 3PA share, 3P%, turnover rate, assist rate, FTr | Show raw rate by stage only when formulas and denominators match. Pair each point with event/league context and sample size. |
| **Role-dependent volume** | points, assists, rebounds | Prefer per-possession or per-minute views plus games, minutes, usage, and starts. Do not present raw per-game deltas as development on their own. |
| **Competition-relative composite** | PER, BPM, in-house percentile score | Show the value within its native competition and cohort. Never chart it as a common cross-level scale without a validated translation study. |
| **Event/context attribute** | draft-slot mix, tournament record, roster volatility | Attach as context for the observation; do not portray it as a player trait that changed over time. |

Every published Ledger metric therefore needs, at minimum:

```text
metric_key, metric_family, unit, denominator, definition_version,
comparison_semantics, allowed_reference_kinds, minimum_sample_rule,
coverage_requirement, interpretation_note
```

The registry is a guardrail: it prevents a future page from implying that a Summer
League PER of 22 is “better than” an NBA PER of 16 merely because both fields are named
PER.

## The player-facing experience

On a player page, a compact Development Ledger should offer stage cards or a horizontal
timeline. A visitor can select a metric family, rather than receiving one crowded chart
with unrelated scales.

```text
2024 Combine        2024 College        2024 Las Vegas SL        NBA Y1       NBA Y2
measurements         production/role     production + event       role/output  role/output
                                           environment context
```

Each observation should disclose:

- the season/event and source link;
- sample: games, minutes, possessions, attempts, or measurements as applicable;
- raw value and its metric definition;
- native reference frame: e.g. same Summer League draft/status cohort or NBA player
  season distribution;
- data coverage/freshness and calculation version;
- a link back to the Event Profile that explains the environment in which it occurred.

A visitor may then compare two selected stages. The UI should adapt to the metric
semantics: direct deltas for compatible measurements/rates; relative percentile movement
and contextual notes for competition-relative measures; no false precision from a single
universal “translation score.”

## Data model: facts first, evidence projection second

Do **not** build a generic entity-attribute-value table as the source of truth for all
basketball data. Combine, Summer League, college, and NBA data have distinct grains,
provenance, and correction behavior.

Instead, extend the Journey Graph pattern:

```text
canonical player identity
        ↓
affiliation and participation assertions
        ↓
domain facts: combine / college / international / Summer League / NBA
        ↓
versioned Player Development Ledger projection
        ↓
player timeline, Explorer longitudinal cohorts, historical outcome studies
```

The logical Ledger observation points to a canonical source fact and records the
presentation-safe metadata needed to compare it:

```text
player_id, observed_at/season, level, event_or_competition_id,
source_fact_locator, metric_key, raw_value, denominator/sample,
native_reference, percentile_or_band, coverage, calculation_version
```

This projection is replaceable. Corrections to a game log, a new metric definition, or a
better cohort model can rebuild it without overwriting the factual spokes.

Existing `MetricDefinition` / `MetricSnapshot` concepts can help represent cohort
definitions and distributions, but they should not replace domain stat tables as the
canonical source of event facts.

## NBA is a required later spoke, not a cosmetic comparison

DraftGuru already has useful NBA identity links and debut/status metadata. Credible
longitudinal analysis requires a proper NBA fact spoke before the player-facing NBA
stages can ship:

```text
NBA identity link → season/team/stint participation → NBA player game logs
                                               ↓
                                     versioned player-season aggregates
```

It must retain source snapshots, season/team/stint semantics, coverage, and calculation
versions. A single current career total or manually-maintained “NBA outcome” field is
not enough: it loses the difference between year one, year two, a team change, and a
missed season.

NBA aggregate environment data is a useful earlier, narrower companion. It can power
certified Event Profile comparisons before NBA player-season facts exist, but it does
not substitute for the individual longitudinal spoke.

## Longitudinal Explorer and outcome calibration

Once observations are reliable, Explorer can add a longitudinal mode rather than merely
adding more player columns. It should support questions such as:

```text
2021–2024 first-round picks with 20+ Summer League MPG,
top-quartile turnover rate, and their NBA minutes in years 1–2
```

The first public outcome view should be historical cohort calibration:

- define an anchor observation and eligibility cohort;
- show NBA debut, games, minutes, and defined production outcomes over named windows;
- include players who did not reach the NBA, not only survivors;
- disclose `n`, active-player censoring, missing coverage, and uncertainty;
- provide the comparable-player list behind each distribution.

This allows the product to say “what happened next for this historical group,” not “this
player will become X.” Any forecast or translation model is a later research product and
must prove calibration on completed cohorts before it becomes a public score.

## Delivery sequence

1. **Metric semantics audit.** Classify existing combine and Summer League facts by
   comparison type, denominator, coverage, and allowed reference frame.
2. **Event Environment Profiles.** Establish the shared metric registry, coverage
   contract, and native event context from current Summer League data.
3. **Ledger foundation without NBA outcomes.** Materialize sourced combine/Summer League
   observations and native cohort context on a small set of player pages.
4. **NBA aggregate context.** Add only certified league-season environment measures for
   Event Profile comparisons.
5. **NBA player-season (then game-log) spoke.** Ingest historical facts keyed by the
   canonical NBA identity bridge, with snapshot/provenance discipline.
6. **Retrospective study and outcome definitions.** Validate anchors, windows, and
   cohort thresholds on completed draft classes.
7. **Public player ledger and cohort outcomes.** Launch timeline stages and transparent
   “what happened next?” distributions; add Explorer longitudinal filtering after the
   underlying definitions are stable.

## Why this advances the journey-graph backbone

This gives every new dataset a repeatable answer to the same question: “How does an
observation at this point in a player’s journey relate to later evidence?” It reuses
canonical identity, time-aware affiliation/participation, provenance-bearing raw spokes,
and replaceable projections. Summer League is the first rich proving ground; the design
extends naturally to college, international play, the G League, future draft-calendar
events, and eventually other sports without claiming that all leagues share one naive
statistical scale.
