# First Release Outline: Competition Context in Explorer

**Status:** Product planning · **Date:** 2026-07-18

**Parent direction:** `docs/plans/event-environment-intelligence-pitch.md`

## Release objective

Add a fifth **Competitions** tab to the existing Summer League Explorer. It should let a
visitor understand the environment behind the player, team, and matchup data already in
Explorer—both for one competition edition and for Summer League as a whole in a named
year.

This is an environment-analysis release. It does not ship NBA comparison, player-career
outcomes, or a translation/projection score.

## Two row grains

The tab must make the scope explicit rather than treating “Summer League” as one
ambiguous object.

| View selector | One row represents | Best question it answers |
| --- | --- | --- |
| **Summer League seasons** | A calendar year across all included competitions | “How did Summer League play change from 2020–2025?” |
| **Individual competitions** | One named competition edition, such as 2025 Las Vegas | “What kind of environment was this particular event?” |

Both modes share the same profile schema where possible. A season row identifies its
included competitions and links to their detail rows. Team/game totals are pooled before
rates are calculated; field-composition people counts use canonical-player de-duplication
and disclose multi-competition participants.

## First-release row and drilldown elements

### Identity and coverage

- season or competition name; venue for individual competitions;
- dates; completed and scheduled/postponed game counts; distinct teams;
- included competition count for season rollups;
- box-score, shot-chart, and PBP coverage badges; calculation/freshness date.

### Field composition

- distinct players who appeared, plus roster/participation count when available;
- rookie/returner split and Summer League appearance number;
- drafted versus undrafted share; first/second-round and lottery share;
- draft-class, age, position-group, college/international origin distributions;
- teams represented and repeat participants across venues in season scope.

These fields turn the page into useful context for later player filters, rather than only
a league-average stat table.

### How the basketball played

The initial certified environment metrics should come from recomputed aggregate inputs:

- games, points per team game, possessions and pace per 48;
- offensive rating; score-margin and overtime distribution;
- 3PA share, 3P%, free-throw rate, and offensive-rebound rate;
- rim share and rim FG% where shot coverage supports it;
- turnover rate;
- **assisted field-goal rate** (`AST / FGM`) rather than a mislabeled player assist
  percentage. Player `AST%` remains a player-role metric, not the event-level answer to
  “how assisted was the offense?”

Each label must expose its formula, denominator, and coverage. The reader’s requested
rim finishing, three-point shooting, turnovers, and assists are all present in the first
release under accurate names.

### Performance landscape

Keep this small initially:

- team offensive-rating spread and scoring distribution;
- minutes/points concentration among players;
- a short leaders strip linking into the existing filtered Players Explorer.

This gives an event its shape without making tournament wins or a noisy small-sample
leaderboard into a scouting conclusion.

## Explorer controls

### Required controls

- **View:** Summer League seasons / Individual competitions.
- **Year range:** enables an annual 2020–2025 table and trend chart in season view.
- **Venue/competition:** available only in individual-competition view.
- **Minimum completed games:** protects against early-event noise.
- **Coverage:** all / box complete / shot-chart complete / PBP complete.
- **Display:** table and one selected metric trend across the filtered year range.
- **Metric thresholds:** pace, ORtg, 3PA share, 3P%, rim share, rim FG%, turnover rate,
  assisted-FG rate, score margin, field-composition shares, and coverage.

### Useful second-wave controls

- competition type or format/round structure once consistently normalized;
- field filters: rookie share, drafted share, lottery share, median age, team count;
- game-length/era policy if historical sources introduce nonstandard formats;
- direct prior-year or historical-same-venue comparison.

Do not expose an NBA selector in this first release. It belongs after the metric registry
certifies a shared definition and season coverage, and it should remain an optional
reference lens rather than a default.

## Connections to the rest of DraftGuru

Every profile should become a reusable context object, not a page-only calculation:

```text
Competition / Summer League season profile
      ├── Explorer Competitions row and detail
      ├── context strip on filtered Players, Teams, and Matchups results
      ├── tournament/venue page “at a glance” module
      ├── Summer League season hub trend/card
      └── sourceable context for Summer League Desk and later player timelines
```

Links should preserve scope: a user can move from a 2024 all-competitions season profile
to the same scope in Players, Teams, or Matchups; from a Las Vegas profile to only Las
Vegas rows; or from a season profile to one component competition. This is how the
feature feeds existing discovery rather than becoming a fifth isolated table.

## Aggregate rules that must be settled before implementation

1. A season profile pools numerator and denominator totals across included final games;
   it never averages competition-level rates.
2. The default “all competitions” membership is every normalized Summer League
   competition in that calendar year; labels list the included venues and data freshness.
3. Player field counts are distinct canonical players, while participation and player-game
   counts remain separately labeled—one player can appear at multiple venues.
4. Each metric is independently nullable when its input coverage is incomplete. A missing
   shot-chart rate is not zero and does not invalidate box-derived pace or turnover rate.
5. Trend charts render only consistently defined, sufficiently covered years; gaps remain
   visible with an explanation.
