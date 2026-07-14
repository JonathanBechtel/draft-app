# Summer League Desk — QA Checklist

**Sources (source of truth):**
- Behavior spec: `docs/plans/summer-league-scouts-desk-behavior-spec.md`
- Event Desk framework: `docs/plans/event-desk-framework.md` (SL = event-instance #1)
- Annotated mockup (layout): `mockups/draftguru_sl_scout_desk.html` (review-only controls;
  production renders one state)
- Product pitch: `docs/plans/summer-league-scouts-desk-pitch.md`

**Sibling artifact:** test plan at `summer-league-scouts-desk-test-plan.md`

Defines the product-level behaviors QA verifies before the Summer League Desk is "done."
The Desk is a home-page (`/`) module — public, no login. It is **state-driven**: which
state renders depends on the SL calendar + the day's live game status, so QA must exercise
each state explicitly (seeded schedule/status + a mocked clock or test-only override), not
"whatever state today happens to be."

> **Regenerated Jul 8** against the 3-state design + Event Desk framework. Supersedes
> the prior checklist. Removed from scope (do not test): the Desk Wire **ticker**, the
> **Stakes** storyline, **Second Summer**, **Roster Wire**, **Contract Watch** section, the
> "Priors, Updated" **echo panel**, and any **two-way / contract-outcome** copy.

---

## A. Event lifecycle & state machine (the core contract)

- **Outer lifecycle — the window turns the home takeover on/off.** In-window → the Desk owns
  the home hero; out-of-window → the seasonal module disappears and the standard news-first
  homepage returns.
  - Verify: dates in Dormant / Warm-up / Active / Wind-down / Archived (seed schedule + config).
  - Expected: takeover only in Warm-up/Active/Wind-down; no Desk or seasonal stub otherwise; the
    Explorer/Leaders/game/player **stat pages render identically regardless of window** (never gated).
  - Evidence: screenshot per phase + DOM check that stat routes are unchanged off-window.

- **Inner state = game-status driven.** Live whenever ≥1 today game is `in_progress`; **Live
  always wins**.
  - Verify: seed {one in_progress + one final + one upcoming}; then {all final}; then {all upcoming}.
  - Expected: Live / Ledger / Morning respectively.
  - Evidence: rendered state per seed.

- **Ledger → Morning is schedule-relative, not clock-arbitrary.** Ledger persists overnight;
  Morning becomes default at `max(today_first_tip − 6h, 09:00 ET)`.
  - Verify: all games final last night; today's first tip 8:00 PM ET; check rendered state at
    03:00 ET (Ledger), 08:00 ET (Ledger — before floor & lead), 14:00 ET (Morning — 6h before tip).
  - Expected: transitions exactly at the computed boundary; no hand-off the instant the last game ends.
  - Evidence: rendered state at each mocked clock.

- **Off-day (in-window, no games today) → the Ledger persists all day** (no first tip → no flip).
  - Verify: in-window date, zero games scheduled.
  - Expected: last completed day's Ledger stays; no empty Morning skeleton; no 500/blank panels.

- **The module renders exactly ONE state; there is NO user-facing state switcher** (states
  are internal accounting). The schedule-relative flip changes *which single state* shows.
  - Verify: seed each state; load `/`.
  - Expected: the temporally-correct single state renders; no preview controls / no Morning-Live-Ledger
    switcher anywhere in the DOM.

- **Timezone correctness** — boundaries computed in the event TZ, server runs UTC.
  - Verify: seed a game at a known local time; probe state at UTC instants either side of tip.
  - Expected: correct state at each; no UTC-off-by-hours bug.

- **Overlap precedence (framework, single-owner-by-priority)** — with only SL registered, SL
  always owns the takeover.
  - Verify: SL active; confirm it owns `/`. (Multi-event precedence is out of V1 scope.)

---

## B. Morning Card

- Slate is **every game today minus the hero** (`games_today − 1`), ranked by storyline weight;
  the marquee is visually distinct.
  - Verify: seed a day with one Debut+Duel game and others with single/zero triggers.
  - Expected: Debut+Duel game becomes the hero; remaining cards ordered by descending weight;
    each carries its badge(s) from the **five** triggers (Debut / Duel / Streak / Status heat / 2nd look).
  - Evidence: DOM order matches weight order; screenshot.

- **No competitive framing anywhere** — no "clinches / semifinal / win-and-advance / Stakes"
  copy; scores appear only as context.
  - Evidence: grep the rendered DOM for banned terms → none.

- Hero **degrades to a single subject** when the top game has no natural pairing (a lone Debut).
  - Verify: seed a day where the top game is a single-prospect Debut.
  - Expected: single-subject hero (no empty "VS" slot).

- **Relevance tail** — a game with zero tracked-prospect triggers still appears, at the bottom;
  on a large slate (10–11 games) the no-signal tail collapses behind "show all N games."

---

## C. Live Desk

- **Live hero** = highest-weighted **in-progress** game; re-selected each tick (if the marquee
  ended, a live game takes over). Shows both subjects' running lines + live score.
  - Verify: seed the marquee final and another game in progress.
  - Expected: hero is the in-progress game, not the finished marquee.

- **Tick board** — one row per game with status, live score, a **Top Performer column
  (headshot + live GmSc)**, and a one-line read.
  - Verify: seed final/in-progress/upcoming games with box lines.
  - Expected: Top Performer = the **highest-GmSc tracked player in that game so far**;
    **em-dash before tip**; upcoming rows show tip time, no performer.
  - Evidence: screenshot; the named top performer is actually the max-GmSc player in that game's fixture.

- **Freshness stamp** = last successful tick (ET) + next-tick ETA; never a fake "live/seconds" claim.

---

## D. The Ledger

- **Single full-width top-performers table** (no echo/"Priors Updated" panel).
  - Evidence: one table, no second column; no echo card in DOM.

- **Performance of the Night hero** = top performer by **cohort percentile, not raw GmSc**;
  ties broken by raw GmSc.
  - Verify: seed an undrafted 98th-pctl night and a lottery pick at 90th-pctl with higher raw GmSc.
  - Expected: the 98th-pctl undrafted player is the hero.

- Each row's "vs cohort" read is a computed comparison; the row deep-links to the player's SL page.

---

## E. Class Tracker (pinned under all states)

- **Six cohorts filter correctly:** Lottery (R1 & pick ≤14) · Round 1 (1–30) · Round 2 (31–60)
  · Full class (all drafted) · Sophomores (prior-year draftees returned) · Undrafted (no `draft_pick`).
  - Verify: seed players spanning all buckets incl. a #14 (lottery) vs #15 (R1-not-lottery),
    a #31 (R2), an undrafted, and a returning sophomore.
  - Expected: each cohort toggle shows exactly its membership.

- **Scope = ALL Summer League games (all venues), not tournament-only.**
  - Verify: seed a player with games across CA Classic + Vegas incl. non-tournament games.
  - Expected: GP / aggregates include every SL game, not just a tournament subset.

- **Variable length, capped at 30** — cohort shows as many rows as qualify; a cohort with >30
  members shows the **top 30 by the active sort** (GmSc default).
  - Verify: seed a 35-member Full class.
  - Expected: 30 rows, the top 30 by GmSc; caption states the cap.

- **Stat-view taxonomy** — Box / Per-36 / Per-100 share **one column set rescaled**; Advanced
  is its own set; the **fixed frame (Player · GP · MIN · GmSc · grade) is constant** and the
  board always sorts by GmSc.
  - Verify: toggle each mode.
  - Expected: Box family = PTS·REB·AST·STL·BLK·TOV·FG%·3P%·FT% (counting stats rescale
    per-36/per-100; shooting %s **unchanged** across those three); Advanced =
    TS%·eFG%·USG%·AST%·TOV%·REB%·3PAr·FTr·WS/82·BPM; fixed frame + GmSc sort unchanged in all four.
    BPM is shown from `SummerLeaguePlayerSeason` when the row is advanced-eligible; otherwise
    it renders as an em-dash.
  - Evidence: per-36 PTS ≈ per-game PTS × 36/MIN (spot-check one row); FG% identical Box↔Per-36.

- **Undrafted cohort identity swap** — draft-slot column → status; "vs slot cohort" → "vs status cohort."

- **GP=0 rostered players** render with em-dashes across stat + rate columns (no 0.0 / NaN).

- **Sortable** on every column within the cohort; deep-links per row.

---

## F. Cohort / percentile correctness (highest statistical risk)

- **"vs cohort" grade = event-aggregate GmSc percentile within the slot/status cohort**, against
  the **all-venue 2017–25 baseline** with the min-minutes gate.
  - Verify: hand-compute a small fixture cohort distribution; assert the subject's pctl + grade
    (hot ≥90 / warm 65–89 / mid 40–64 / cold <40).

- **Mid-event gate ladder** — with a 1-game sample the board walks the adaptive ladder rather
  than publishing a confident percentile.
  - Verify: seed a player with 1 game in a thin cohort.
  - Expected: `gated=true` behavior (suppressed/qualified pctl), not a noisy 1-game percentile.

- **Debut bar** = cohort mean GmSc for that slot's first-ever SL games; "beat his 11.2 debut bar"
  reflects the fixture's computed mean.

---

## G. Storyline engine

- **Five triggers only**, each by deterministic rule: Debut (no prior SL log), Duel (two prominent
  prospects share a game; consensus rank ≤14 or fallback draft slot ≤14), Streak (≥3 straight
  games at/above cohort median with average run percentile ≥65), Status heat (undrafted or
  2nd-round player ≥85th percentile vs status cohort), 2nd look (returner above/below his
  prior SL). **No Stakes.**
  - Verify: one positive + one near-miss per rule.

- **Weight = Σ(base × magnitude)**; magnitude driven by **consensus-rank prominence** (fallback
  draft slot). A #1 debut outranks a #45 debut; a #1-vs-#2 duel outranks two second-rounders.
  - Verify: two Debut games differing only in prominence.
  - Expected: higher-prominence game ranks above.

- **Deviation-first per state** — Morning ranks by entering/expected weight; Live re-ranks by
  realized deviation; finished games sink below in-progress.

---

## H. Commentary integrity (fact → angle → phrase)

- **Every rendered sentence traces to a fact detector** (has a `detector_id` / provenance); no
  free-text, no unattributed claims, no hot takes.
  - Verify: for each visible string, assert a backing fact object exists.

- **Deterministic** — same fixture rendered twice → byte-identical copy (no runtime LLM/randomness).

- **Angle chosen by notability** — the highest-extremity fact wins the hero tagline.
  - Verify: fixture where a "rank #1 in cohort" fact and a "96th pctl" fact both apply.
  - Expected: the rank-1 (`cohort_rank`) sentence is chosen; percentile is not duplicated.

- **No banned/unsupported copy** anywhere in the DOM: "McDonald's", "two-way", "signed a deal",
  "audition" as a per-player claim, "career-best", tournament/competitive terms.

---

## I. Interaction, visual, perf

- **No user-facing state controls** — the module renders the single current state (verified via
  seeded state / test override); no Morning-Live-Ledger switcher in the DOM.
- **Visual** (`make visual`): each state's dark hero, the slate grid, the live board + Top
  Performer column, the single-column Ledger, the pinned Class Tracker with the stat-view toggle
  — all match the mockup and the "light retro analytics" style guide; adequate space below the hero.
- **Mobile**: tables scroll inside their card (no page horizontal scroll); hero stacks compactly
  enough that the next section is discoverable in the first viewport.
- **Home-page perf** — `/` query budget holds **in every state**; the Desk reads projections
  (T2/T4/`event_desk_state`), it does **not** recompute cohorts/storylines per request, and it does not inherit
  the known `/` N+1. Budget entry set consciously in `tests/integration/perf/budgets.py`.
