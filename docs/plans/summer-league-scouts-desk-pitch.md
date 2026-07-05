# Summer League Scout's Desk — Product Pitch

> A three-state home-page module that turns DraftGuru into the daily companion for Summer League: before games it tells you what to watch, while they run it gives an hourly scouting read, and afterward it tells you how to update your priors — all computed, no editorial.

**Mockup:** `mockups/draftguru_sl_scout_desk.html` (annotated: three states, pinned spine, methodology-TBD flags)
**Upstream feature:** SL stats archive (shipped; see `docs/plans/summer-league-stats-pitch.md`) — this is its front door.

**Launch target:** July 9, 2026 (Vegas tip-off). V1 runs entirely on the existing hourly ingestion crons; nothing in scope requires new pipelines.

## Problem

Summer League is the single highest-attention window in DraftGuru's calendar between draft night and the college season — and the home page currently doesn't acknowledge it's happening. The SL data layer is feature-complete (box + advanced metrics, hourly game ingestion, 2,887 archived player-seasons), but it's all *destination* pages a visitor must seek out. Meanwhile ESPN/NBA.com own scores and highlights; nobody answers the question SL actually exists to pose — **"is this guy good, relative to who he was drafted to be?"** — on any cadence, let alone daily.

## Audience

- **Primary:** Draft-heads and team-focused fans who will check in on their rookie 1–3× per day during July 9–19 and currently stitch that picture together from box scores + Twitter.
- **Secondary:** Fantasy/dynasty players tracking the class as a portfolio; casual visitors arriving from shared "cohort echo" cards ("best start by a #1 pick since 2017").

## Hypothesis

If the home page renders a **daily-rhythm, cohort-contextualized view of Summer League** (distinct before / during / after states plus an always-current class tally), then returning visitors will open DraftGuru **multiple times per day during the event** — establishing the daily-habit engagement tier the site lacks (see `project_draft_calendar_pipeline_strategy`) and converting July's traffic spike into retained users rather than one-shot sessions.

## Core User Flow (one Summer League day)

1. **Morning:** open DraftGuru → **Morning Card**: today's slate ranked by computed storylines (Debut / Duel / Stakes / Streak / Contract watch / 2nd look), a marquee game with expectation context ("Peterson's comps averaged 11.2 GmSc in SL debuts").
2. **Evening, games running:** the **Desk Wire** ticker flows scores + storyline tallies; the **Live Desk** shows the latest hourly tick — key-matchup running tally, a scouting read per game, "since the last tick" notes. Honest freshness stamps throughout ("as of 9:02 PM · next tick ~10:00 PM").
3. **Next morning:** the **Ledger** — top performers of the night (all statuses, not just rookies) and "Priors, Updated": cohort-echo sentences only nine years of SL history can generate.
4. **Any time:** the pinned spine — **Class Tracker** (lottery/round-1/full-class/sophomores standings board), **Contract Watch** (undrafted/two-way overperformers), **The Second Summer** (returners vs their own prior SL) — every row deep-linking into the existing SL archive pages.

## Headline Features (Advertisables)

- **Three time-aware states** of one module — what to watch, what's happening, what it means — swapped automatically by schedule state.
- **Class Tracker:** the draft class as a standings board, graded by percentile vs draft-slot cohorts across 2017–25 SL history.
- **Contract Watch:** the undrafted/second-round audition board, with historical conversion kickers ("5 of the last 6 to sustain this signed NBA deals").
- **The Second Summer:** year-2/3 returners measured against their own previous SL and the typical year-2 jump.
- **Desk Wire:** a scoreboard-style ticker in scouting language — scores, duels, streaks, auditions — no market framing.
- **Storyline engine:** a fixed, deterministic rule library (debut, duel, stakes, streak, contract, 2nd-look) ranks the slate; zero human editorial.
- Every number is a computation over data DraftGuru already holds: SL archive baselines, KNN comps, consensus ranks, player-status history.

## Scope Boundaries

- **Not doing:** true live/push updates (websockets, PBP-driven in-game feeds).
  - **Why:** hourly crons already exist and the unit of value is the *revised read*, not the play. A faster tick later changes freshness stamps, not layout. Keeps V1 inside the low-JS/no-build architecture.
- **Not doing:** market/stock framing (tickers with price deltas, "risers/fallers" as finance metaphor).
  - **Why:** user decision (Jul 3): clean analytical scouting voice conveys importance without inventing a trust-sensitive index metric; the draft-stock-market concept remains a separate, later product question.
- **Not doing:** editorial content of any kind — every sentence on the module is template-rendered from a computed comparison.
  - **Why:** aggregator positioning (`project_positioning`); it's also the only version a small team can operate daily.
- **Deferred to P2 (ship only if the three states land early):** pre-event **Roster Wire** (filtered feed of notable roster adds, Jul 5–9).
  - **Why:** data already flows from the roster poll cron, but most adds are marginal; nice-to-have, not mission-critical.
- **To resolve during spec, before build:** storyline weights, and the slot-cohort window rule (±3 picks for lottery vs round buckets). Both are flagged in the mockup; neither blocks layout work.

## Success Signal

- **Primary:** during July 9–19, returning visitors average **≥2 sessions/day** (vs ~1 baseline), with home-page CTR into SL destination pages materially up — evidence the before/during/after loop creates appointment behavior.
- **Secondary:** Ledger "cohort echo" lines appearing as screenshots/quotes on draft Twitter (trackable via referrers + share-card usage) — the leading indicator that the analytical voice, not the scores, is the draw.

## Competitive / Context

- ESPN/NBA.com own real-time scores; Tankathon owns draft-order liveness. Nobody maintains a cohort-contextualized class tracker or a data-driven contract-watch board — this is the differentiated surface.
- Fits `project_live_prospect_feed_idea` (this is its V1, on proven ingestion) and `docs/plans/summer-league-stats-pitch.md` (the archive is the destination; the Desk is the front door).
- `feedback_analytical_voice`: lead with cohort/historical insight, not game reporting — the Ledger is that principle productized.
