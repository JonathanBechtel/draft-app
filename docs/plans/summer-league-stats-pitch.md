# Summer League Stats — Product Pitch

> A basketball-reference-grade statistical archive for NBA Summer League, built into DraftGuru so prospects can be followed from pre-draft consensus through their first competitive NBA-adjacent reps.

**Tech spec:** `docs/summer_league_stats_plan.md`

**Launch target:** A few days before NBA Summer League 2026 tips off (Salt Lake / California Classic open ~July 5–6, Vegas ~July 10 historically). Plan the full feature end-to-end; prune to fit the date during ticket generation rather than pre-splitting the vision here.

## Problem

NBA Summer League is the first place draft picks face NBA-quality opponents, but the public data surface is broken: Basketball-Reference's SL coverage is thin and inconsistent, NBA.com Stats has the data but a clunky, unlinkable UI, and no site stitches SL performance back to pre-draft consensus rank. Draft-curious fans currently lose the thread on a prospect the moment the draft ends — DraftGuru ranks them in June and goes quiet until November.

## Audience

- **Primary:** Analytics-minded NBA draft fans who already use DraftGuru's player and consensus pages and want a single trustworthy place to evaluate how this year's class is actually performing in July, with the same cross-linked exploration they expect from Basketball-Reference.
- **Secondary:** Draft Twitter / content creators looking for shareable SL takes ("the lottery's biggest over- and underperformer this week"), and prospect researchers digging into historical SL résumés of second-rounders and undrafted internationals.

## Hypothesis

If we ship a comprehensive, cross-linked Summer League section anchored on prospect pages, then **return visits during July and time-on-site per session** will increase materially because draft-class followers now have a reason to come back daily during SL — and the rest of the year, the explorer + leaderboards become an evergreen long-tail traffic source against a genuinely underserved query space.

## Core User Flow

1. User opens a prospect page during or after Summer League.
2. Above the fold they see the familiar pre-draft consensus + bio; immediately below, a new **Summer League** section shows that player's SL stat line with sample-size context and a cohort-percentile chip.
3. User clicks a game date and lands on the box score, where every teammate and opponent name is a link.
4. User pivots to **/stats/summer-league/{year}** to see the whole class's SL performance sorted by pick, then opens the **Explorer** to filter ("undrafted players, 2018–2024, 20+ MPG, TS% above league avg").
5. User shares an Explorer URL or a leader-board card to social; the share encodes the query so the recipient lands on the same view.

## Headline Features (Advertisables)

- Every NBA Summer League game from ~2015 forward, with raw box, advanced stats (TS%, USG%, AST%, on/off where PBP allows), and clear sample-size warnings.
- A **draft-class SL page** that sorts the current class by pick and lets you see at a glance who's outplaying their consensus rank.
- A **Stats Explorer** with shareable URLs — answer questions like "best SL games ever by an undrafted player" or "rookies who outscored their lottery class."
- Per-prospect SL section on every player page, so a single profile tells the full pre-draft → SL story.
- Sticky tables, heat-shaded cells, per-36 / per-100 / advanced toggles, sample-size badges — the information-design quality that makes the data trustworthy at a glance.

## Scope Boundaries

- **Not doing:** G-League, Drew League, EuroLeague, Olympics, or any non-NBA-Summer-League competitions.
  - **Why:** SL is the natural draft-adjacent dataset; mission-creep into general early-career tracking dilutes the niche and is deferred to a separate future feature.
- **Not doing:** Custom scouting content, SL-game recaps, or editorial takes on performances.
  - **Why:** DraftGuru is an aggregator, not a content producer (see `project_positioning`). The product is the data layer; editorial is left to existing voices.
- **Not doing:** Guaranteed coverage of pre-2015 Summer Leagues at launch.
  - **Why:** Pre-2015 NBA.com Stats coverage is unreliable; backfill is gated on an API probe and will ship opportunistically with explicit "data quality: limited" badges rather than blocking launch.
- **Not doing:** Composite metrics calibrated to the NBA regular season (BPM, VORP, Win Shares) presented without caveats.
  - **Why:** SL pace/context invalidates the calibration; we'd rather omit or heavily caveat than ship misleading numbers.
- **Not doing:** Player-tracking-derived stats (defensive matchups, contested-shot rate, etc.).
  - **Why:** Tracking data isn't available for SL at any era; not worth building affordances for data that doesn't exist.

## Success Signal

- **Primary:** Measurable lift in July session count and pages-per-session vs. the prior off-season baseline, driven by repeat visits to player SL sections and `/stats/summer-league/{year}`.
- **Secondary:** Inbound traffic to **/stats/summer-league/explorer** URLs (shared queries) showing up in referrer logs from Twitter/Reddit during and after SL — a leading indicator that the explorer is doing the share-driven SEO job we want it to.

## Competitive / Context

- `docs/competitor_analysis.md` — competitive landscape; SL coverage is a genuine gap among analytics aggregators (CraftedNBA, To The Mean).
- `docs/BUSINESS_MODELS.md` — SL traffic during July strengthens the sportsbook/DFS affiliate funnel exactly when summer prop markets are live.
- `project_positioning` memory — DraftGuru is an aggregator; SL is data work, not editorial.
