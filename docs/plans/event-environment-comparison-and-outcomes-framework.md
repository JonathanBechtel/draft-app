# Event Intelligence: Comparison and Subsequent-Outcome Framework

**Status:** Product/data discovery · **Date:** 2026-07-18

**Parent direction:** `docs/plans/event-environment-intelligence-pitch.md`

**Longitudinal companion:**
`docs/plans/player-longitudinal-evidence-layer-pitch.md` defines the reusable
player-observation and later-outcome layer that extends beyond Summer League.

## The decision is not “compare to the NBA or not?”

Comparison is useful only when it answers the visitor’s actual question and the two
numbers share a defensible definition. The product should choose the reference frame
based on the question, rather than presenting the NBA as a universal benchmark.

| User question | Best reference | Why |
| --- | --- | --- |
| “What kind of tournament was this?” | Same competition’s prior editions and historical distribution | Controls for the event’s purpose, roster churn, schedule, and game length. |
| “How unusual was this player’s Summer League?” | Draft-slot/status/age/returner cohort in Summer League | Compares a player to the realistic peers competing for the same opportunity. |
| “How did this tournament play compared with pro basketball?” | NBA regular-season context | Useful for system-level pace, shot mix, efficiency, and turnover/assist context. |
| “Did this player’s Summer League predict later NBA value?” | Historical players with the same pre-event cohort and subsequent NBA outcomes | This is a longitudinal relationship, not a direct NBA-average comparison. |

## When NBA context belongs in the product

Use an NBA benchmark for **environment-level, aggregate measures** when all conditions
below are true:

1. The metric is defined identically in both sources and can be recomputed from stored
   totals (not merely copied from a provider display).
2. The units are normalized where required—e.g. pace per 48 rather than raw possessions
   in 40-minute Summer League games.
3. The comparison is framed as descriptive context: “this event was faster / less
   efficient / more three-heavy,” not as a player-translation factor.
4. Both sides carry coverage, season, source, and calculation-version metadata.

Good first NBA-comparison metrics:

- pace per 48 and offensive rating;
- 3PA share, 3P%, rim share, rim FG%, and other source-aligned shot zones;
- turnover rate and assisted-FG rate;
- free-throw rate, offensive-rebound rate, and score-margin distribution once the
  denominator/source policy is pinned.

The comparison should be available as an optional **“NBA regular season” lens** on an
Event Profile. It should never be a default that hides more relevant historical Summer
League context.

## When NBA context does not belong

Do not compare a figure directly to NBA league average when it primarily reflects an
event’s special conditions or when the comparison invites a false conclusion:

- **Individual raw box lines:** Summer League role, short sample, lineup instability, and
  40-minute game length make “his PPG versus NBA PPG” a poor evaluation tool.
- **Player PER/BPM and other pool-calibrated composites:** these are centered/re-fit to a
  competition and are not a common NBA scale without a carefully designed translation
  study.
- **Draft-status/field composition and roster volatility:** those are event attributes;
  compare to prior editions or draft cohorts, not the NBA population.
- **Tournament record/bracket outcome:** it may describe format but should not be used as
  evidence of prospect quality.
- **Incomplete historical slices:** no NBA or historical benchmark can repair missing
  shot/PBP/identity coverage; render the available metric set and its confidence instead.

## Reference-frame policy in the UI

Every Event Profile should lead with its **native baseline** and make reference choice
visible:

```text
Event Profile: 2025 Las Vegas Summer League
Reference: [Historical Las Vegas ▾]  [2024 Las Vegas]  [All Summer League]  [NBA 2024–25]
```

Rules:

- Default to a trailing same-event historical distribution when enough complete editions
  exist; show median and percentile/range rather than a single “average” where possible.
- Offer direct prior-edition comparison when the user is asking a year-over-year question.
- Offer NBA only for metrics certified comparable by the profile’s metric registry.
- Carry the reference in the URL, share card, CSV, and visible label.
- If a selected reference cannot support a metric, show an em dash plus its coverage
  explanation—not an interpolated number.

This is a reusable **metric registry** requirement. Each metric needs metadata such as:

```text
metric_key, profile_section, unit, aggregation_rule, required_coverage,
eligible_reference_kinds, definition_version, interpretation_note
```

The registry prevents a future UI from accidentally placing a competition-relative BPM
next to an NBA league average merely because both happen to have the same label.

## A separate, higher-value product: “What happened next?”

The most compelling player-facing feature is not “player X’s SL stat compared with NBA
average.” It is a transparent longitudinal question:

> For historical players like this—same entry cohort, Summer League role, and
> performance band—what did their NBA careers look like afterward?

That can appear in two forms.

### 1. Cohort calibration (first responsible release)

On an Event Profile or player’s Summer League season, let a visitor form a historical
cohort, for example:

```text
Las Vegas first-time lottery picks, 2017–2023,
20+ minutes/game, top-quartile Game Score
```

Show subsequent-outcome distributions with sample counts:

- NBA debut rate by year 1 / year 2;
- games and minutes in years 1, 2, and 3;
- rotation threshold share (e.g. ≥1,000 NBA minutes in first three seasons);
- a clearly chosen NBA production metric for players who reached a minutes threshold;
- comparable-player list with links, not an opaque single prediction.

The output says “in this historical sample,” includes `n`, coverage, and uncertainty. It
does not say Summer League performance caused the outcome or promise an individual result.

### 2. Player timeline (later)

For a historical player, the journey page can show:

```text
draft → Summer League participation and event profile → NBA debut → NBA seasons
```

That is a natural expression of the Player-Journey Graph. It becomes especially useful
once a visitor can move from an Event Profile’s cohort chart to the individual careers
behind it.

## Feasibility and current foundation

### Already present

- Canonical `players_master` identities and `player_external_ids` with stable
  `nba_stats` IDs—strong join keys for an NBA stat source.
- NBA debut season/date and current/last-season status on player/profile data.
- Summer League competition, participation, game, team-box, player-box, shot, PBP, and
  derived metric facts with provenance/coverage patterns.
- The journey-graph direction: canonical identity → affiliation/participation →
  game-level facts → replaceable projections.

### Missing before outcome analysis is credible

- An NBA **player-season and preferably game-log stat spoke** connected by canonical
  player ID—not a scraped current-career total pasted onto a player profile.
- Archived NBA source snapshots, coverage/retry policy, and season/team/stint semantics.
- Pinned outcome definitions and eligibility windows: “first NBA season” vs. draft year,
  Summer League year, injuries, players who never debut, and active players whose
  outcomes are still censored.
- A cohort-analysis layer that handles minimum samples, selection bias, era effects, and
  uncertainty rather than optimizing a seductive but unreliable prediction score.

## Recommended sequence

1. **Event Environment Profile, Summer League only.** Build the common metric registry,
   coverage contract, and historical same-event comparisons from existing data.
2. **NBA aggregate context.** Add the narrow NBA league-season input only for certified
   environment metrics. This is an optional lens, not a player-outcome system.
3. **NBA player-season spoke.** Ingest versioned player-season facts (and eventually game
   logs) through `nba_stats` identity links; preserve raw snapshots/provenance.
4. **Outcome definitions and retrospective cohort study.** Validate on completed draft
   classes before publishing a user-facing calibration surface.
5. **What happened next?** Ship historical cohort distributions and timelines, then
   consider more personalized similarity/forecast tooling only if calibration holds.

## Journey-graph alignment

The NBA player-season spoke should use the same backbone pattern as Summer League:
canonical identity and affiliation/participation assertions feed game/season facts, which
feed replaceable event-to-outcome projections. It must not become a parallel “career
outcomes” blob keyed by fuzzy names. This makes the feature useful across Summer League,
college, international tournaments, and future sports while keeping the causal claim
appropriately modest.
