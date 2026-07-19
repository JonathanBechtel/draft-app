# Product Pitch: Summer League Context Benchmarks

> **Superseded for planning by** `docs/plans/event-environment-intelligence-pitch.md`.
> The NBA comparison remains a useful optional lens, but the broader Event Environment
> Intelligence profile is now the parent product direction. Preserve this document as
> the source for the initial NBA-comparison research and metric definitions.

**Feature:** `summer-league-context-benchmarks` · **Status:** Pitch (Step 1 of chain)
· **Date:** 2026-07-17

> Scope: an editorially honest, reusable league-context layer for Summer League—not
> a one-off article spreadsheet.

## The reader request

A writer wants to explain what a Summer League stat *means* by comparing the event
environment to the NBA: shooting at the rim and from three, turnover and assist rates,
pace, and scoring efficiency. The existing Explorer can expose player, team, and game
facts, but it deliberately has no league-average row and no NBA reference context.

This is both a useful editorial artifact and a product gap: a player’s 31% three-point
shooting means something different in a 31.3% three-point environment than it does in a
36.0% NBA environment.

## Audience and job to be done

- **Writers and analysts:** cite a compact, sourced answer without hand-assembling
  disparate NBA Stats queries.
- **Fans:** understand why Summer League box scores look different before treating
  them as direct NBA translations.
- **DraftGuru:** give every event page an interpretable baseline that later works for
  G League, college, international, and (eventually) NFL Draft surfaces.

## Recommended first release

Add a fifth Explorer subject: **League Context**. It sits beside Players, Game Finder,
Teams, and Matchups—not on a separate season page—and uses the Explorer's existing
year/venue filters and shareable URL state. A compact “how this event played” preview
can also appear above the Teams results when the query is pinned to that same event,
but the Context tab is the primary, discoverable surface.

The card is competition-scoped. Its default Summer League reference is **Las Vegas**,
not a synthetic all-venues “Summer League” total: the California Classic, Salt Lake
City, and Las Vegas events share teams/players and occur in different competitive
contexts. A reader can explicitly choose another competition where coverage is full.

The Context tab requires one pinned Summer League competition (one year + one venue).
It pairs that competition with the NBA regular season immediately preceding it and
labels both scopes and season dates in plain language—for example, “2025 Las Vegas
Summer League vs. 2024–25 NBA regular season.” A broad Explorer query instead renders
an intentional empty state: “Choose one season and venue to see a comparable league
baseline,” rather than silently blending incompatible events.

| Metric | 2025 Las Vegas SL | 2024–25 NBA | Difference | Reader-safe interpretation |
| --- | ---: | ---: | ---: | --- |
| At-rim FG% | 62.8% | 66.4% | -3.5 pp | Finishing was materially less efficient. |
| 3P% | 31.3% | 36.0% | -4.7 pp | Perimeter conversion lagged the NBA substantially. |
| 3PA share of FGA | 43.3% | 42.1% | +1.2 pp | The shot mix was similarly three-heavy. |
| Assisted-FG rate | 60.6% | 63.7% | -3.1 pp | A smaller share of made baskets came from recorded assists. |
| Turnover rate | 16.7% | 12.6% | +4.1 pp | Possessions ended in turnovers much more often. |
| Pace, per 48 minutes | 105.2 | 98.8 | +6.4 | Summer League played faster after normalizing its 40-minute games. |
| Offensive rating | 103.2 | 114.6 | -11.4 | The faster environment still produced much less efficiently. |

**Definitions that must appear with the Context table:**

- “At rim” is NBA Stats’ `Restricted Area` zone.
- Three-point percentage and attempt share use team box-score totals.
- The group-level assist metric is **assisted-FG rate** (`AST / FGM`), not player
  `AST%`; calling it “assist rate” without that definition would be ambiguous.
- Turnover rate is `TOV / (FGA + 0.44 × FTA + TOV)`.
- Pace uses the project’s opponent-adjusted possession estimate, normalized to 48
  minutes; it is intentionally not raw points per 40-minute game.

### Article-ready framing

> The 2025 Las Vegas Summer League did not look like a scaled-down NBA offense. It
> was faster (105.2 possessions per 48 minutes versus 98.8), but less connected and
> less efficient: turnovers consumed 16.7% of shooting possessions, and the event
> converted 31.3% from three and 62.8% at the rim. The preceding NBA regular season
> was at 36.0% from three and 66.4% at the rim. The difference is not merely talent;
> it is a different, short-sample developmental environment with unfamiliar lineups.

The wording should describe the observed environment, not assert a causal explanation
that the aggregate data cannot prove.

## Data and methodology

The initial figures above were reproduced from NBA Stats’ team game logs and team
shooting-by-zone feeds, using all 152 Las Vegas team-game rows (76 games) and all 2,460
NBA regular-season team-game rows (1,230 games). DraftGuru’s live 2025 Las Vegas event
also confirms complete 30-team, 76-game coverage.

- Summer League source: NBA Stats `LeagueID=15`, season `2025`, regular season;
  [DraftGuru’s Las Vegas 2025 event](https://draft-app-prod.fly.dev/stats/summer-league/2025/las_vegas).
- NBA comparator: [NBA.com Teams Shooting, 2024–25](https://www.nba.com/stats/teams/shooting?Season=2024-25)
  and regular-season team game logs.

Production implementation must preserve the source response/snapshot metadata,
coverage counts, calculation version, and retrieval time. The Context tab must suppress
its comparison with a clear coverage message when a competition lacks complete team
boxes or zone data; it must never manufacture a baseline from partial shots.

## Journey-graph alignment

This advances the Global Player-Journey Graph without creating a parallel player store.
The canonical inputs remain the existing Summer League competition/game/team-box and
shot-event spokes, plus a future NBA league-season stat spoke with its own provenance.
The comparison card is a **replaceable, versioned derived projection** at the
`competition ↔ benchmark-season` grain. It carries coverage and formula provenance and
can be recomputed when a source correction lands. That is the same
assertion-and-projection distinction the backbone requires, and it gives future spokes
(G League, college, international, NFL) one reusable context contract.

## Scope boundaries

**In for v1**

- One row per fully covered Summer League competition and paired NBA regular season.
- An Explorer `context` subject, its pinned-scope empty state, the seven metrics above,
  definitions/tooltips, source links, CSV fields, and a compact preview on the Teams
  subject for the same pinned scope.
- Offline/scheduled computation; public page reads one materialized projection and never
  makes a live NBA Stats request.

**Out for v1**

- Player “translation” models or claims that a player will carry a fixed percentage to
  the NBA.
- Combining overlapping Summer League venues into one pseudo-league.
- NBA playoff comparisons, opponent-quality adjustments, and possession-level play-type
  analysis.
- Rebuilding the existing Summer League raw-ingestion system.

## Success signal

A writer can cite a shared DraftGuru URL or CSV and reproduce each displayed value from
the named source and formula. A fan sees the environment context before interpreting an
individual leader board. Adding a new competition/league later requires a new source
spoke and projection input, not a new one-off page or player-data store.

## Proposed implementation slices

1. **Benchmark contract and snapshot ingestion:** define the canonical source snapshot
   and a versioned competition-benchmark projection, with formula and coverage tests.
2. **Summer League aggregation:** derive team-box, shot-zone, and possession values from
   the existing raw/normalized spoke; gate it on complete coverage.
3. **NBA comparator ingestion:** fetch and persist the same aggregate inputs/snapshots
   for the paired NBA regular season; do not query NBA Stats on requests.
4. **Read surface:** Explorer Context subject, route/template, CSV fields, a Teams-view
   preview, and transparent definitions and empty/partial states.
5. **Verification:** integration coverage for source pairing/gates/calculations; route
   query-budget and index checks; visual review of the card and its small-screen layout.
