# Product Discovery: Event Environment Intelligence

**Status:** Directional pitch / discovery · **Date:** 2026-07-17

**Companion framework:**
`docs/plans/event-environment-comparison-and-outcomes-framework.md` defines when each
reference frame—including NBA context—is appropriate, and scopes the longer-term
“what happened next?” player-outcomes direction.

**Longitudinal companion:**
`docs/plans/player-longitudinal-evidence-layer-pitch.md` describes the reusable player
evidence timeline that connects observations at events to later stages of a career.

> This replaces the narrow idea of an Explorer-only “Summer League versus NBA”
> comparison. The comparison remains useful, but it is one optional lens inside a
> broader event-intelligence product.

## The reframe

DraftGuru should help a visitor answer two different questions:

1. **What happened to this player/team/game?** The Explorer, box scores, leaders, and
   player pages already do much of this work.
2. **What kind of environment was this event or season?** This is the missing layer.

An event is more than a table of player lines. It has a field, format, pace, shot diet,
style, variance, historical position, and data-confidence level. Those facts make the
existing player data interpretable, and they are useful whether or not a visitor ever
compares Summer League to the NBA.

Call the shared layer **Event Environment Intelligence**. It is a profile of one
competition edition, built from the facts DraftGuru already collects. A comparison is a
user-selected operation over two profiles—not a hard-coded feature destination.

## What we already collect

| Existing fact | Current source/spoke | Environment question it can answer |
| --- | --- | --- |
| Competition year, venue, dates, game/round/status, data quality | `SummerLeagueCompetition`, `SummerLeagueGame` | What was the event, how long did it run, and how complete is it? |
| Team entries, records, scores, bracket | team entries + games/schedule | How large was the field, what was the format, and how much did games vary? |
| Team box totals plus pace/ratings | `SummerLeagueTeamGameLog` | How fast, efficient, assist-heavy, turnover-prone, or three-heavy was play? |
| Player box lines and advanced materializations | player game logs + player seasons | Who carried the field; how concentrated was production; what did the distribution look like? |
| Shot events and play-by-play | shot/PBP spokes | Where did shots come from, how did they convert, and what is the assist/unassisted mix? |
| Draft slot, class, age, country, position, college, status | canonical player/profile/participation data | Who was in the field: rookies, returners, drafted/undrafted, positions, origins? |
| Participation and affiliation assertions | journey-graph backbone | Which players/teams were actually present, even before box-score appearance? |
| Cohort percentiles and Desk facts | existing Desk/metrics projections | How unusual were individual performances relative to relevant peers? |

## Product model

### 1. Competition and season profiles: two intentional scopes

The primary detail grain is a **competition edition**: for example, *2025 Las Vegas
Summer League*. It should not be silently blended with Las Vegas, California Classic,
or Salt Lake City when a visitor is asking about a specific event.

But **Summer League season** is also a valid, first-class scope: for example, *2025 NBA
Summer League — all competitions*. It answers a different, useful question: “What did
Summer League basketball look like this year, and how did that change from 2020–2025?”
It is a pooled season profile built from all included completed games, with its component
competitions named and linked. It is not an average of venue percentages.

The same five sections apply to either scope:

An Event Profile has five stable sections:

| Section | Question | Example facts |
| --- | --- | --- |
| **Identity & format** | What was this event? | dates, venue, teams, games, round structure, bracket/round coverage |
| **Field composition** | Who played in it? | rookie/returner split, drafted vs. undrafted, draft-slot bands, positions, age, international/college mix, announced vs. played |
| **How it played** | What was the game environment? | pace, ORtg, turnover rate, assisted-FG rate, 3PA share, rim/mid/three diet and conversion, score-margin/OT distribution |
| **Performance landscape** | How was output distributed? | top-end versus median production, minutes/usage concentration, team offensive spread, cohort performance distribution |
| **Data confidence** | How much can we trust each conclusion? | completed-game count, box/shot/PBP coverage, unresolved-player count, freshness and calculation version |

The profile should state what is observed and its sample, not pretend an event of five
games per team is a stable talent forecast.

For a season profile, game/team totals pool across included competitions; field-composition
counts deduplicate canonical player identities and disclose players who appeared in more
than one competition. This preserves a truthful total Summer League view while retaining
the venue-level profile for event-specific interpretation.

### 2. Competitions tab: a new meta grain, not a widget

The Explorer’s current subjects—Players, Teams, Games—answer entity-level questions.
Add a fifth **Competitions** subject as the environment-level grain. It has an explicit
scope selector:

```text
View: [Summer League seasons — all competitions] [Individual competitions]
```

- The season view has one row per year and supports time-series questions such as “How
  did Summer League pace, rim finishing, or three-point volume change from 2020–2025?”
- The competition view has one row per competition edition and supports event-shape
  questions such as “Which Las Vegas editions were fastest?” or “Which competitions
  were most rookie-heavy?”
- Selecting a row opens the corresponding Competition or Season Profile drilldown within
  Explorer, with a shareable URL. It does not force a user through a separate product.

The familiar Player/Team/Game subjects can then show a compact **environment context
strip** when their filters resolve to one event: “2025 Las Vegas · fast / low-efficiency
environment · 30 teams · full shot coverage.” That gives interpretation at the point a
user is reading a player line without turning every table into a comparison dashboard.

### 3. Comparison is a lens

On a specific Event Profile, a user may choose a comparison target:

1. **Same competition, prior editions** — the default and most trustworthy first lens.
2. **Another Summer League competition/venue** — explicit side-by-side, never silently
   pooled.
3. **All historical Summer League** — a clearly labeled historical reference distribution.
4. **NBA regular season** — an optional external benchmark once source snapshots and
   definitions are established.

The NBA comparison requested by Mike fits naturally here, but no longer dictates the
schema, Explorer navigation, or home-page story. A visitor can choose it when the
question is “how different is this from NBA basketball?” and skip it when the question is
“what changed in Las Vegas versus last year?”

The comparison policy and later player-outcome direction are intentionally detailed in
the companion framework, rather than embedded as a one-size-fits-all NBA comparison.

## Where it appears

| Surface | Role |
| --- | --- |
| **Explorer → Competitions** | Discovery, filtering, sorting, and shareable Competition or all-competitions Season Profiles. |
| **Explorer → Players/Teams/Games** | Small context strip only when one event is selected; link to that Event Profile. |
| **Venue/tournament page** | Primary “Event at a glance” module: identity, field, style, confidence, and a link into the Explorer profile. |
| **Season hub** | A portfolio of event cards, not a blended style average; shows how each venue/event differed and links into each profile. |
| **Landing page** | Latest-event snapshot with data-confidence/freshness; avoids duplicating the full profile. |
| **Summer League Desk** | Reuses event profile facts for computed, sourceable context; it does not invent an editorial layer. |

## Important aggregation rules

- **Competition edition is the default detail denominator; the all-competitions season is
  an explicit aggregate scope.** Pool its underlying game/team totals to calculate rates;
  never average venue rates. Name included competitions and preserve drilldown access.
- **Rates recompute from totals.** Never average team percentages, player percentages,
  or event rates unweighted.
- **Data confidence is metric-specific.** A competition may support box-style facts but
  not shot-diet or assisted-FG facts. Missing coverage is a product state, not a zero.
- **Comparison requires shared definitions.** Each profile records formula version and
  source coverage; a comparison only renders metrics comparable across both inputs.
- **Tournament standings are context, not a player-development thesis.** Format and game
  distribution can be informative; wins/losses should not become an unsupported scouting
  conclusion.

## Data architecture: profile first, comparison second

Build a generic, versioned **Competition Environment Profile** projection from existing
raw/stat spokes. It should retain aggregate inputs, coverage, source references, and a
calculation version. A second projection or read-time adapter can compare two profiles.

This order matters:

```text
competition facts + game/team/player/shot/PBP spokes
                         ↓
          versioned Competition Environment Profile
                         ↓
        Explorer Events + tournament/season-home modules
                         ↓
          optional comparison lens (NBA is one target)
```

The profile is a replaceable projection; the canonical facts retain their existing
provenance. This directly advances the Global Player-Journey Graph’s hub-and-spoke,
assertion-and-projection model. It also gives later college, international, G League, and
NFL event spokes a consistent public context contract without forcing identical sport
metrics.

## Phased discovery roadmap

### Phase 0 — Metric inventory and semantic audit

Classify every candidate fact by grain, source, denominator, historical coverage, and
whether it can be compared across editions. Define the minimum viable profile separately
for box-only, shot-chart, and PBP-complete events.

### Phase 1 — Summer League Event Profile from current facts

Ship identity/format, field composition, core team environment, and data confidence for
each full Summer League competition. This is valuable without NBA ingestion.

### Phase 2 — Explorer Events subject and home-page reuse

Let users discover/sort events; add compact profile modules to venue/tournament and
season pages. Keep the season hub as a portfolio of events, not a false blended league.

### Phase 3 — Comparison lenses

Add historical same-event comparisons first. Add NBA context as a source-backed option
only after its snapshot/definition contract is in place. Other leagues can follow.

### Phase 4 — Derived insights, carefully

Once profiles accumulate, introduce transparent percentile/trend annotations such as
“fastest Las Vegas edition since 2019” or “largest returning-player share in the tracked
era.” Every insight links to its profile, source, and method.

## Decisions to make before ticketing

1. Which field-composition facts are sufficiently complete across historical Summer
   League to be v1 profile metrics?
2. Should an Event Profile be public for partial/box-only history with a reduced set of
   sections, or only for fully covered events?
3. What should count as the first comparison baseline: prior edition, trailing historical
   distribution, or both?
4. Which home-page modules deserve a compact profile now versus simple links until the
   Explorer Events subject is proven?

## Why this is worth the wider umbrella

It makes the site more interpretable at every layer. A writer gets the NBA comparison
they asked for; a fan gets a useful “what kind of tournament was this?” answer; and the
product gains a reusable event-level projection rather than a bespoke response to one
email.
