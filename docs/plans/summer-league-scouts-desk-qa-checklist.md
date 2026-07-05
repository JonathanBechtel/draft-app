# Summer League Scout's Desk — QA Checklist

**Sources:**
- Product pitch: `docs/plans/summer-league-scouts-desk-pitch.md`
- Annotated mockup (source of truth for layout + selection rules): `mockups/draftguru_sl_scout_desk.html`

**Sibling artifact:** test plan at `summer-league-scouts-desk-test-plan.md`

This checklist defines product-level behaviors QA should verify before considering the
Scout's Desk complete. The Desk is a home-page (`/`) module, public, no login. It is
**time-aware**: which state renders depends on the SL calendar and the day's game
schedule, so QA must exercise each state explicitly (via seeded schedule data or a
test-only state override), not just "whatever state today happens to be."

## State Machine (the feature's core contract)

- The module renders the **Morning Card** state before the day's first scheduled tip.
  - Verify: seed a day with games all in the future; load `/`.
  - Expected: storyline slate + marquee visible; no Desk Wire ticker; no live board; freshness shows last completed ingest, not a fake "live" claim.
  - Evidence: screenshot + absence of ticker element in DOM.

- The module renders the **Live Desk** state while any game today is in progress or between games on a game day after first tip.
  - Verify: seed one final, one in-progress, one upcoming game for today; load `/`.
  - Expected: Desk Wire ticker present; tick header shows the last ingest time ("as of …") and expected next tick; all three game statuses render in the live board with a scouting read each.
  - Evidence: screenshot; stamp text matches the seeded ingest timestamp exactly.

- The module renders the **Ledger** state after the day's last final, through the next morning.
  - Verify: seed all of today's games final; load `/`.
  - Expected: top performers table + "Priors, Updated" echoes for last night; morning slate for the *next* day appears once the schedule rolls over.
  - Evidence: screenshot.

- Outside an SL window the module collapses to the single archive strip.
  - Verify: set date outside the configured event window; load `/`.
  - Expected: one strip above the news hero (title, one summary stat, archive CTA); none of the full Desk sections render; the rest of the home page is unchanged from pre-feature behavior.
  - Evidence: screenshot + DOM check.

- A mid-event **off day** (window active, no games scheduled today) renders sensibly.
  - Verify: seed an in-window date with zero games.
  - Expected: no empty "Tonight's Storylines" skeleton — the module shows the Ledger/tracker view with a "next games <date>" note; no 500, no blank panels.
  - Evidence: screenshot.

- All state boundaries are computed in the **event's timezone** (PT for Vegas), displayed consistently (ET or user-local — pick one and stick to it), and are not broken by the server running UTC.
  - Verify: seed a 7:00 PM PT game; check state at 01:30 UTC (= 6:30 PM PT, pre-tip) and 03:30 UTC (in-progress window).
  - Expected: Morning Card at the first check, Live Desk at the second.
  - Evidence: rendered state at each mocked clock time.

## Core User Behaviors — Morning Card

- A user can see today's slate ranked by storyline weight, with the marquee game visually distinct.
  - Verify: seed a day where one game has a Debut+Duel pairing and others have single storylines.
  - Expected: the Debut+Duel game gets the marquee treatment; remaining games ordered by storyline weight; each card carries its badge(s) (Debut / Duel / Stakes / Streak / Contract watch / 2nd look).
  - Evidence: DOM order matches expected ranking; screenshot.

- Storyline badges are assigned by the deterministic rules only.
  - Verify: for each rule — Debut (no prior SL log), Duel (two top-N picks in one game), Stakes (tournament math), Streak (active multi-game run), Contract watch (qualifying overperformer scheduled), 2nd look (returner tracking above/below prior SL) — seed one positive and one near-miss case.
  - Expected: badge appears for the positive case, absent for the near-miss; no badge type ever appears without its rule firing.
  - Evidence: rendered badges vs seeded fixtures.

- Marquee expectation rows show pre-game context computed from real sources.
  - Verify: marquee headliner with KNN comps and prior cohort data.
  - Expected: "comp cohort avg in SL debuts: X GmSc" matches a hand computation from the comps' actual SL debut logs; a player with no comps/cohort data gets a graceful omission, not a blank or NaN.
  - Evidence: hand calculation; screenshot of the no-data fallback.

- Every player name/chip on the slate deep-links to that player's SL page; games link to the game page.
  - Verify: click through each chip/card.
  - Expected: 200s, correct targets; unresolved players (no canonical id) render as plain text, never a broken link.
  - Evidence: link audit.

## Core User Behaviors — Live Desk + Desk Wire

- The Desk Wire ticker flows scores and storyline tallies during game windows only.
  - Verify: load `/` in Live Desk state and Morning state.
  - Expected: present and animating in the former, absent in the latter; content matches the latest tick data; no market/stock vocabulary anywhere (no ▲/▼ deltas, no "stock").
  - Evidence: screenshots both states; text audit of ticker items.

- The key-matchup running tally shows both players' current lines with cohort chips and a computed read.
  - Verify: seed a Duel game in progress with partial box lines.
  - Expected: both sides' lines match the seeded box data; percentile chips match hand-computed cohort percentiles; the "read" sentence is one of the template outputs, populated with correct numbers.
  - Evidence: hand calculation vs chips; screenshot.

- The live board lists every game today with status, score, and one scouting read.
  - Verify: seed finals, in-progress, and upcoming games.
  - Expected: statuses/scores correct; each read references a real seeded fact (right player, right number); upcoming games show expectation context instead of a score.
  - Evidence: row-by-row check against fixtures.

- Freshness is honest under cron failure/staleness.
  - Verify: simulate the ingest tick being >90 min old during a game window.
  - Expected: the stamp still shows the true last-tick time (and ideally a visible "data may lag" note); the module never displays a fabricated "as of" time or silently renders stale lines as current.
  - Evidence: stamp text vs actual last ingest timestamp.

## Core User Behaviors — Ledger

- Top performers of the night includes all statuses, not just first-rounders.
  - Verify: seed a night where an undrafted player has the #2 GmSc.
  - Expected: he appears, ranked correctly, with the right status tag (Pick N / Undrafted / Two-way / Sophomore); ordering is by the stated metric.
  - Evidence: table vs fixture ordering.

- "Priors, Updated" echoes are computed comparisons with visible cohort sourcing.
  - Verify: each echo's claim (e.g., "best start by a #1 pick in the sample") against a direct DB query over the historical baselines.
  - Expected: exact agreement; each echo shows its cohort definition line; an echo never renders when its threshold didn't fire.
  - Evidence: SQL cross-check per echo template.

## Pinned Spine — Class Tracker / Contract Watch / Second Summer

- The Class Tracker shows event-to-date lines for the selected population with a cohort-percentile grade per row.
  - Verify: toggle Lottery / Round 1 / Full class / Sophomores; sort a column; pick 2 players and hand-verify GP/MPG/PTS/TS%/GmSc from game logs, and the percentile from the slot-cohort baseline.
  - Expected: populations filter correctly; aggregates exact; percentile matches the spec'd slot-window rule; a 0-GP player shows a "debuts <date>" style placeholder, not zeros or a percentile.
  - Evidence: hand computations; screenshots per toggle.

- Contract Watch surfaces only players passing the selection rule, graded within their status cohort.
  - Verify: seed players around each boundary — status ∈ {undrafted, second-round, two-way, unsigned}, ≥40 event minutes, percentile ranking.
  - Expected: a first-rounder never appears regardless of performance; a 35-minute player is excluded; ordering follows status-cohort percentile; historical kicker lines ("N of the last M signed deals") match a query over past SL + subsequent contract status.
  - Evidence: boundary fixtures in/out; SQL cross-check of the kicker.

- The Second Summer compares returners to their own prior SL and the typical year-2 jump.
  - Verify: seed a returner with known '25 and '26 lines; hand-compute ΔGmSc/ΔTS% and the typical-jump baseline from all returner pairs in the sample.
  - Expected: deltas exact; grade chip (well above / above / flat / below) matches the baseline comparison; a true rookie never appears; a year-2 player with no prior SL minutes is omitted or explicitly annotated, not shown with a bogus delta.
  - Evidence: hand computation; edge-case fixtures.

## Scope, Safety, Performance

- The home page stays within its query budget with the Desk in every state.
  - Verify: `make perf` for `/` in each seeded state (morning / live / ledger / off-window).
  - Expected: within the (consciously set) budget in all states; off-window adds at most 1–2 queries over the pre-feature baseline.
  - Evidence: perf test output per state.

- New Desk queries are index-backed on a prod-like DB.
  - Verify: `make explain ROUTE=/` against the Neon read branch for each new query.
  - Expected: Index Scans on large tables (game logs, player seasons, baselines); no Seq Scan on a large table.
  - Evidence: EXPLAIN output captured.

- The module degrades gracefully with JS disabled and on mobile.
  - Verify: disable JS, load each state; then 390px viewport.
  - Expected: all content server-rendered and readable (ticker may be static or hidden — but never a blank strip); no horizontal scroll on mobile; tables scroll within their own containers.
  - Evidence: JS-off + mobile screenshots.

- Sample-size honesty: tiny samples never render as authoritative.
  - Verify: a 1-GP player in the tracker; a percentile computed off <5 cohort games.
  - Expected: visible sample-size cue (GP column adjacent, or explicit badge per spec); no percentile shown where the cohort base is below the spec'd floor.
  - Evidence: rendered treatment of seeded small-sample cases.

- No editorial or market vocabulary anywhere in the module.
  - Verify: text audit of every rendered template string across states.
  - Expected: every sentence is template-generated from computed values; no "stock", "price", ▲/▼ delta framing.
  - Evidence: grep of templates + rendered-page audit.

- P2 Roster Wire (only if built): only relevance-rule-passing adds appear.
  - Verify: seed 10 adds, 3 passing the rule (drafted / NBA games / prior SL overperformance / top-N consensus).
  - Expected: exactly those 3 render, with the "N adds · showing M" framing; the section is absent entirely outside the pre-event window.
  - Evidence: fixture check.
