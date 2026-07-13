# Summer League Desk — Behavior Specification

Companion to the pitch (`summer-league-scouts-desk-pitch.md`), QA checklist, and test plan.
Source-of-truth mockup: `mockups/draftguru_sl_scout_desk.html`.
Its Morning / Live / Ledger controls are a **review-only preview device**; production must
render exactly one state and must not ship those controls.

This document pins the behavior the mockup only *implies*. Compute definitions below are
now pinned so implementation tickets can proceed without inventing statistical policy.

**Positioning & voice (naming decision Jul 8):** the product is the **Summer League Desk**,
not "Scout's Desk." DraftGuru surfaces **data and tooling to follow basketball through a
scout's lens — it is not a scout and never voices unattributed hot takes.** Every line is a
computed comparison with its cohort/source; no first-person scouting persona. (Reinforces
the no-editorial rule throughout.)

---

## 1. The module & its three states

One home-page module that renders **one** of three **states** (Morning Card / Live Desk /
The Ledger). **The three states are internal accounting — selected automatically by the
state machine — NOT user-facing tabs.** The visitor sees only the current state; there is no
manual state switcher. (The mockup shows all three at once via a **review-only** tab device;
production renders the single current state.) A pinned **Class Tracker** sits below.
No market framing, no editorial — every sentence is template-rendered from a computed
comparison.

| State | Question it answers | Hero |
|-------|--------------------|------|
| Morning Card | "What should I watch today?" | Marquee face-off (tale of the tape) |
| Live Desk | "What's the scoop as of the last tick?" | Live key-matchup duel + running lines |
| The Ledger | "How should I update my priors?" | Performance of the Night (single subject) |

**Live tick board:** below the Live hero, an all-games board — one row per game with its
status, live score, a **Top Performer column (headshot + live GmSc)** = the highest-GmSc
tracked player in that game so far (em-dash before tip), and a one-line read at the current
tick.

---

## 2. State machine & timing ✅ game-status driven

The module's current state is derived from **game status** wherever game status can
distinguish the states (ET used only for display stamps + the one schedule-driven flip
below).

- **Live** — whenever ≥1 SL game on today's schedule is `in_progress`. **Live always
  wins** — if any game goes live, state = Live regardless of clock.
- **Ledger** — from the day's **last final**; **persists overnight and through the
  morning**. This is the morning-after digest, so it must not hand off early.
- **Morning Card** — becomes the default at `max(today_first_tip − LEAD, MORNING_FLOOR)`.
  Priors: `LEAD = 6h`, `MORNING_FLOOR = 09:00 ET`. Tunable.

**The Ledger → Morning flip is the one transition game status cannot drive** — nothing is
live on either side of it — so it uses the **schedule** (today's first tip), not an
arbitrary wall-clock time. Consequences:
- **Off-day (no games today): no first tip → the flip never fires → the Ledger persists
  all day.** This is exactly the quiet-slate behavior (§4) with zero special-casing.
- The naive trigger "tomorrow's slate exists in the DB" is **wrong** — the full event
  schedule is published days ahead, so it would flip to Morning the instant tonight's last
  game ends and kill the morning-after read.

**The module renders exactly the current state** — a mid-game visitor sees Live; a morning
visitor sees the Ledger. There is **no user-facing state switcher**; the states are internal
accounting. The schedule-relative flip changes *which single state is shown*, not a tab.
(Last night's Ledger is simply what shows through the morning until the flip to Morning Card.)

- **Off-window** (outside the SL calendar window): the seasonal module renders nothing;
  the standard news-first homepage returns. Summer League remains discoverable through
  its evergreen stats pages and normal navigation. **The window itself is the SL event's *lifecycle*** —
  see `event-desk-framework.md`. SL is **event-instance #1**: the daily Morning/Live/Ledger
  machine here is the framework's *inner* machine; the on/off boundary is the *outer*
  lifecycle (Dormant→Announced→Warm-up→Active→Wind-down→Archived). SL's window config:
  schedule-driven (nba_stats + config override), one contiguous July window (gap-bridge
  absorbs CA→SLC→Vegas), priors `pre_roll 3d / gap_bridge 4d / post_roll 2d`. "SL mode"
  toggles the **home-page takeover only** — the Explorer/Leaders/game/player stat pages are
  evergreen, never gated by the window.
- **Refresh:** existing hourly Fly cron; freshness stamp = last successful tick (ET).
  A faster tick later changes only the stamp cadence, not layout.

### Resolution & data prerequisites (pinned for implementation)

- **State is resolved at REQUEST TIME** by the pure resolver over `(now, today's tip
  times, last-known game statuses)`. `event_desk_state` stores **data + freshness** (last
  tick, hero refs) — it is an *input* to the resolver, never the state verdict. The
  clock-driven Ledger→Morning flip must never be quantized to hourly tick times.
- **Scheduled-tip fallback:** game statuses are only as fresh as the last tick. If
  `now ≥ today_first_tip` and not all of today's games are final, render **Live** even if
  no game is yet *marked* in-progress (stale tick) — with the honest last-tick freshness
  stamp. Prevents "page says Morning while games are underway."
- **Data prerequisites (existing-table migration, not create_all):**
  `summer_league_games` currently stores only a `game_date` (date) and its status enum is
  `SCHEDULED/FINAL/UNKNOWN`. The Desk requires: **(a)** a `tip_datetime` column (UTC;
  displayed ET) and **(b)** an `IN_PROGRESS` status value. **(c)** Job B gains a **step 0
  schedule/scoreboard ingest**: fetch today's (and tomorrow's) SL games from the
  stats.nba.com scoreboard — game ids, `gameTimeUTC`, live status codes — and upsert them
  *before* normalize. Morning Card and the flip cannot exist without tip times.

---

## 3. Storyline engine ✅ deviation-first · prospect-only

**Principle: the Desk never frames SL as a competition.** SL wins/losses and tournament
standings are meaningless and are *never* a storyline or a headline. Scores appear only as
context to locate a player's line in a game — never as stakes. Every trigger is about an
individual prospect's development signal.

A fixed rule library — no editorial. **Five** trigger types (Stakes dropped), each
emitting a typed badge:

- **Debut** — player has no prior SL game log.
- **Duel** — two prominent prospects share the floor. Prominent = DraftGuru consensus
  rank ≤14, falling back to draft slot ≤14 when consensus is missing.
- **Streak** — an active run of ≥3 straight SL games with each game at or above the
  player's cohort-median GmSc and the run's average percentile ≥65.
- **Status heat** — an undrafted or 2nd-round player is tracking ≥85th percentile vs his
  status cohort. This replaces the earlier "Contract watch" label; V1 has no contract
  type/outcome source, so copy must not imply two-way, Exhibit-10, signing, or roster data.
- **2nd look** — a returning yr-2/3 player tracking meaningfully above/below his prior SL.
- ~~**Stakes**~~ — **DROPPED.** SL competitive results don't matter; no tournament-math
  trigger, and no competitive framing anywhere in copy.

### Weighting  `game_weight = Σ ( base[type] × magnitude[instance] )`

- `base[type]` — how intrinsically headline-worthy the *kind* of story is (priors below;
  one config constant = the site's editorial voice without a nightly editor).
- `magnitude[instance]` — how strong this instance is, driven by **prospect prominence**
  = a function of DraftGuru **consensus rank** (our canonical board; reuses the journey-graph
  consensus assertion source), falling back to draft slot when no consensus rank exists.
  So Debut/Duel *are* rank-weighted: #1 debut ≫ #45 debut; #1-vs-#2 duel ≫ two 2nd-rounders.

| Type | base (0–100, prior) | magnitude driver |
|------|--------------------|------------------|
| Duel | 90 | combined prominence of the two prospects |
| Debut | 80 | prominence of the debutant |
| 2nd look | 70 | pctl deviation vs his *own* prior SL |
| Streak | 65 | streak length × avg pctl over the run |
| Status heat | 60 | pctl deviation above status cohort |

**Deviation-first per state:**
- **Morning (pre-tip):** weight = additive sum of *entering* trigger weights
  (base × expected magnitude — prominence, entering-streak heat). Tiebreak = best
  consensus rank among tracked players.
- **Live:** cards **re-rank by realized deviation** — how far tonight's live lines sit
  above/below cohort baselines. Finished games sink below in-progress ones.

A game may carry multiple badges (weights add). Base weights ship as priors; tune
post-Vegas against engagement.

---

## 4. Hero selection per state ✅

- **Morning marquee** = the single highest expected-interest game. Two-subject
  face-off when the top game has a natural pairing (Duel/Debut-vs-star); **degrades to a
  single-subject hero** when there's no counterpart.
- **Live key matchup** = highest-weighted game currently `in_progress` (not a finished
  one). Re-selected each tick — if the marquee ended, a live game takes over.
- **Ledger Performance of the Night** = single top performer by **cohort percentile**
  (not raw GmSc), so an undrafted 98th-pctl night can beat a lottery pick's 90th.
  Ties broken by raw GmSc.
- **Quiet slate ✅ always force a headline.** No games today, or nothing clears a
  storyline threshold → promote the **class leader / biggest mover-to-date** into the
  hero ("Dybantsa still leads the class at 20.7 GmSc"). The front page is never dead.

---

## 5. "The Rest of Tonight's Slate" — selection, ranking, limit ✅

- **Selection:** every SL game on today's schedule **minus the hero game**
  (`slate = games_today − 1`). No game is dropped — completeness matters (every game is
  someone's team).
- **Ranking:** storyline weight descending (§3). Morning = expected-interest;
  Live = realized deviation, finals sink below in-progress.
- **Relevance tail:** games with zero tracked-prospect triggers sink to the bottom.
- **Functional limit:** no hard editorial cap. On large early-Vegas days (10–11 games)
  the no-signal tail **collapses behind a "show all N games" toggle** so the fold stays
  clean. Exactly one hero, always.
- **Edge cases:** 1-game day → empty slate + compact note, hero carries it;
  0-game day → §4 quiet-slate rule.

---

## 6. Cohort & percentile methodology ✅ all-venue 2017–25 blend

The "vs cohort" percentile shown on every row and hero:

- **Baseline population = all-venue, 2017–25 SL blend** (matches the existing SL advanced
  default) with a minimum-minutes gate. Consistent with the rest of the SL surface.
- **Metric:** event-aggregate GmSc percentile within the subject's cohort (per-game
  variants available via the stat-view toggle but the cohort metric stays GmSc).
- **Slot-cohort window rule:** lottery players use a ±3-pick window clamped to picks 1–14
  (`slot:1-4`, `slot:2-5`, ..., `slot:11-14`); non-lottery first-rounders use `round:1_late`
  for picks 15–30; second-rounders use `round:2` for picks 31–60; undrafted players use
  `status:undrafted`. Grounds on the existing model: `draft_pick` is within-round, so
  lottery = R1 & pick ≤14.
- **Debut bar** = cohort **mean** GmSc for that slot/status cohort's first-ever SL games.
- **Mid-event calibration:** reuse the shipped **adaptive gate ladder** (2/60 → 1/20 →
  1/0). Early in the event, percentiles on 1-game samples are under-calibrated; the board
  walks the same ladder rather than surfacing noisy percentiles.

---

## 7. Class Tracker mechanics ✅

- **Cohorts:** Lottery (R1 pick ≤14) · Round 1 (1–30) · Round 2 (31–60) · Full class
  (all drafted) · Sophomores (prior-year draftees who returned) · Undrafted
  (no draft pick in the current class, ≥ min minutes).
- **Scope = ALL Summer League games in the event cluster (all venues), NOT tournament-only.**
  Sample size beats purity given the tiny per-player game count; the subject value and the
  cohort baseline both aggregate a player's full SL body of work (consistent with the
  all-venue 2017–25 baseline, §6).
- **Variable length, capped at 30:** show as many rows as qualify; when a cohort exceeds
  30, show the **top 30 by the active sort** (GmSc default), not by draft position.
- **Undrafted cohort:** identity column swaps draft-slot (`#5 · NOP · G`) → status
  (`Undrafted · LAL`); "vs slot cohort" → "vs **status** cohort."
  ⚠ **Contract type (two-way / Exhibit-10 / unsigned) is NOT in the data model** — only
  `players_master.draft_*` exists, so V1 classifies **drafted vs undrafted only** (a player
  with no `draft_pick` in the current class = Undrafted). Finer contract granularity needs a
  new `contract_status` source (NBA transactions/roster feed) — **deferred**; do not label
  two-way in copy until it exists. (Also weakens the §8 contract-outcome kicker.)
- **Sort:** GmSc desc default; every column sortable within the cohort.
- **Stat view — one shared "box family" set + one advanced set** (reuses the SL Explorer
  read service; curates the display). Box / Per-36 / Per-100 are *the same columns
  rescaled* — counting stats change per mode; shooting %s are rate-invariant. Advanced is
  its own set. The **fixed frame is constant in all four modes** — the mode only swaps the
  middle block, so the board always sorts by **GmSc** and shows the same cohort grade:

  | Slot | Columns |
  |------|---------|
  | Fixed (all modes) | Player · GP · MIN · **GmSc** · vs cohort (grade) |
  | Box family (Box / Per-36 / Per-100) | PTS · REB · AST · STL · BLK · TOV · FG% · 3P% · FT% |
  | Advanced | TS% · eFG% · USG% · AST% · TOV% · REB% · 3PAr · FTr · **WS/82** · **BPM** |

  Fields map to existing Explorer columns (`ts_pct`, `efg_pct`, `usg_pct`, `ast_pct`,
  `tm_tov_pct`, `reb_pct`, `fg3ar`, `ftr`, `ws82`, `bpm`). SL pace is per-48; blended
  Per-100 extrapolates the 2017 gap. This curated set is the **canonical tracker column
  taxonomy** — reuse it across surfaces (parity with the player-page advanced table).
  BPM is already available on `SummerLeaguePlayerSeason`; render it only for rows with an
  advanced-eligible event aggregate and show an em-dash otherwise.
- **GP=0 rostered players** (e.g. "debuts tonight") appear with em-dashes across stat and
  rate columns; each row deep-links to the player's SL page.
- **Toggle interaction model = server round-trip via query params** (the SL Explorer's
  existing pattern), NOT preloading all 6 cohorts × 4 stat views client-side. Keeps the
  low-JS architecture and the perf budget honest; the active cohort/stat-view renders
  server-side.
- **Measurement:** every Desk deep-link (tracker rows, hero, slate, live board, ledger)
  carries a `?ref=sl-desk` query param so home→SL CTR (the pitch's success signal) is
  attributable in existing request logs. No new analytics infra.

---

## 8. Data provenance & open risks

Every displayed sentence must trace to a query — no editorial.

**✅ Commentary audit COMPLETE (Jul 8).** Every line on the mockup was traced to one of:
- **(a) computable today** — raw logs, `game_score_line()`, `player_seasons`, existing
  advanced fields, `draft_*`, consensus rank (box lines, GmSc, TS%/TOV%, "SL debut", etc.).
- **(b) computable via the specced projections** — T1 cohort baselines + Job B live
  aggregates + game-log scans over 2017–25 history: **all** percentiles, "best start by a
  #1 pick / ahead of 2025 Flagg", streaks, "8-rookie club", "most by a #2 pick since 2019",
  self-vs-prior-summer deltas, "leads all rookies tonight", cohort medians. Reproducible;
  nothing hand-waved.
- **(c) removed as non-reproducible:** the "McDonald's game" reference (HS-game history we
  don't track) → restated "first SL floor"; flavor phrases ("every game is an audition",
  "playing for contracts", "career-best pace") are banned unless backed by a future sourced
  transaction/contract model.

**Contract / transactional data — DROPPED for V1 (low impact, decided Jul 8).** No
contract-outcome history ("signed a deal within a year") and no contract type
(two-way/Exhibit-10) — the Undrafted cohort is drafted-vs-undrafted only, with no historical
contract kicker. Revisit only if a `contract_status`/transactions source is later added.

**Standing rule:** any *new* copy must trace to a query — i.e. a fact detector in the
commentary engine (§11) — before it ships.

---

## 9. This is event-instance #1 of the Event Desk framework

**Decided:** the desk is built as a generic **Event Desk** (`event-desk-framework.md`), with
SL as instance #1. V1 builds the framework seams (two nested state machines, `events`
registry, `event_desk_state`, single-owner-by-priority controller, a content-provider
interface SL implements) but **registers only SL**. Event #2 (FIBA / AAU / U17 / March
Madness) is then config, not a refactor. SL's content-projection tables (§10 T1–T4) stay
SL-namespaced until a 2nd event reveals the common shape. See the framework doc for the
lifecycle machine, overlap precedence, and the SL event config.

---

## 10. Data model & compute (ticket-ready)

Most infrastructure already exists — reuse, don't rebuild:

| Reuse | Role |
|-------|------|
| `summer_league_player_seasons` | per-player, per-event aggregate (GP, minutes, box totals) — the **subject value** source |
| `game_score_line()` | canonical GmSc |
| `summer_league_metric_models` | the **versioned, offline-fit** pattern the baselines copy (`*_version`, `is_active`) |
| `summer_league_metric_contexts` | per `(competition, year, venue)` context (pace, `adv_eligible`) |
| draft slot (`players_master.draft_*`), consensus rank, `get_blended_leaders`, adaptive gate ladder | slot, prominence, venue-blend, mid-event calibration |
| `deploy/fly/fly.cron.roster.stage.toml` | hourly Fly cron template to clone |

**Naming:** existing `summer_league/cohort.py` = *roster* cohort. Ours is the **slot-cohort
baseline** (draft-slot comparison group) — keep the names distinct.

Everything new below is a **projection** (rebuildable from raw logs / draft / consensus
assertions; safe to drop & recompute). All SQLModel tables in `app/schemas/`; register in
`tests/integration/conftest.py`; new tables via `create_all`/`drop_all` per the migration
workflow. State/freshness lives in the framework-level `event_desk_state`; do **not** add
a separate Summer-League-specific state table.

### T1 · `summer_league_cohort_baselines`  (new — the precomputed distribution)
The expensive-but-stable artifact. Versioned like `summer_league_metric_models`.
- `id` PK · `baseline_version` str · `is_active` bool
- `cohort_key` str (e.g. `slot:1-4`, `round:1_late`, `round:2`, `status:undrafted`, `debut:1-4`)
- `cohort_kind` enum: `slot_window` | `round_bucket` | `status` | `debut`
- `slot_low` / `slot_high` Optional[int] (null for status)
- `metric` str (`gmsc`) · `grain` enum: `event` | `game` | `debut`
- `venue_scope` str (`all` | venue_slug) · `season_range` str (`2017-2025`)
- `min_minutes` float (eligibility gate, e.g. 40) · `n_members` int (sample size → gate ladder)
- `breakpoints` json (percentile→value map for O(1) ranking) · `mean_value` / `median_value` float
- `computed_at` datetime
- **Refresh trigger:** rare — new-history ingest / window-rule change / manual. Job A. Never on the tick.

### T2 · `summer_league_desk_player_grades`  (new — the per-event percentile, sidecar)
Keeps the canonical aggregate clean; one row per active player per event per baseline_version.
- `id` PK · `player_id` FK · `competition_id` FK · `baseline_version` str
- `cohort_key` str (which cohort they were ranked in)
- `subject_value` float (event-agg GmSc) · `pctl` float (0–100) · `grade` enum (`hot`/`warm`/`mid`/`cold`)
- `n_cohort` int · `gated` bool (gate ladder suppressed a confident pctl)
- `facts` json (selected Fact objects + rendered strings for this player — §11 storage)
- `computed_at` datetime
- **Index:** `(competition_id, cohort_key)` for tracker reads. **Refresh:** hourly tick (Job B).

### T3 · `summer_league_desk_storylines`  (new — trigger instances)
One row per fired trigger per game (a game may have several).
- `id` PK · `game_date` date · `competition_id` FK · `game_id` FK
- `trigger_type` enum: `debut` | `duel` | `streak` | `status_heat` | `second_look`
- `subject_player_id` FK (· `subject_player_id_2` for duel)
- `base_weight` float · `magnitude` float · `weight` float (= base × magnitude)
- `realized_deviation` Optional[float] (null pre-tip; filled live)
- `computed_at` datetime · **Index:** `(game_date, competition_id)`

### T4 · `summer_league_desk_slate`  (new — per-game rollup for cheap reads + share)
Sum of T3 weights per game + ranking; one row per game per day, upserted each tick.
- `id` PK · `game_date` date · `competition_id` FK · `game_id` FK
- `total_weight` float · `rank` int · `is_hero` bool
- `facts` json (selected Facts + rendered read/headline strings for this game — §11 storage)
- `computed_at` datetime

### Existing-table change · `summer_league_games`  (migration, `op.*` not create_all)
- ADD `tip_datetime` (UTC datetime, nullable — legacy rows have none)
- ADD `IN_PROGRESS` to `summer_league_game_status_enum`
Required by §2's data prerequisites; populated by Job B step 0.

### Framework state · `event_desk_state`
The generic Event Desk state table is the thin freshness/state stamp (~1 row per event/tick)
and is the only state source:
- `event_id` FK · `as_of` datetime · lifecycle phase · daily state (`preview`/`live`/`recap`)
- `is_home_owner` bool · `hero_ref` json (`game_id` / `player_id` / surface kind)
- `freshness_tick_at` datetime · `next_tick_eta` datetime · upserted each tick.

### Compute jobs
**Job A — `scripts/build_sl_cohort_baselines.py`** (offline, rare): read historical
player-seasons + draft slot + venue context → apply min-minutes gate → group by `cohort_key`
per the window rule → compute breakpoints/mean/median → write new `baseline_version`, flip
`is_active`. Recalibrate per season/venue (bottom-up).

**Job B — `sl_desk_tick`** (hourly during window, appended after the existing
`normalize_summer_league` step; dormant off-window):
0. **schedule/scoreboard ingest** — fetch today's + tomorrow's SL games from the
   stats.nba.com scoreboard (`curl_cffi` impersonation per the existing scraper): game ids,
   `gameTimeUTC` → `tip_datetime`, live status → `IN_PROGRESS`/`FINAL`. Upsert
   `summer_league_games` before anything else.
1. (existing) scrape + normalize new box scores → update `summer_league_player_seasons`.
2. per active player → subject value → pick `cohort_key` (window rule + slot/status) → rank
   vs active baseline (T1) → write T2 (apply gate ladder when `n_cohort`/GP too low).
3. evaluate storyline triggers for today's games → magnitudes (prominence = consensus rank;
   realized deviation from T2) → write T3 + T4 rollup (rank, `is_hero`).
4. upsert `event_desk_state` (state from game status, hero, freshness stamp).

The tick **never rebuilds a distribution** — it ranks against cached T1. That's what keeps it
inside "hourly cron + simple reads." Per-request the page reads T2/T4/`event_desk_state`
directly.

---

## 11. Commentary generation — fact → angle → phrase

The engine that turns stats into sentences. A **deterministic 3-stage pipeline** — **not**
a runtime LLM. Same data → same sentence (this is exactly what the §8 audit verifies).

### Stage 1 — Fact detectors (the fact library)

A fixed registry of detectors, each `(subject, context) -> Fact | None`. **Each detector is
ONE verifiable query — the audit unit.** A Fact is a typed record:

```
Fact {
  kind:       cohort_rank | percentile | streak | self_delta | leads_field
              | debut_vs_bar | count_club | first_since
  subject:    player / player-event ref
  metric:     gmsc | ast | tov_pct | ...
  cohort:     cohort_key (slot-window | status | debut | field:tonight | ...)
  values:     { value, gp, pctl, rank, of, runner_up{who,value}, delta, count, since_year }
  notability: float 0–1                       # selection score (Stage 2)
  provenance: { detector_id, baseline_version, cohort_key }   # reproducibility / audit
}
```

SL fact kinds V1 needs — each maps to real mockup copy (from the §8 audit):

| kind | detector | mockup example |
|------|----------|----------------|
| `cohort_rank` | rank subject value in cohort distribution (+ runner-up) | "best start by a #1 pick … ahead of 2025 Flagg (18.9)" |
| `percentile` | subject value's pctl vs cohort (T1) | grade chips; "96th pctl · top-5 cohort" |
| `streak` | scan player game log for a run vs threshold | "three straight 15+ GmSc" |
| `self_delta` | subject's current event vs his prior-year SL | "+5.3 GmSc ahead of his first summer" |
| `leads_field` | rank across the live/event field | "leads all rookies tonight"; "top undrafted performer" |
| `debut_vs_bar` | subject debut game vs cohort debut-mean (T1 debut grain) | "beat his 11.2 debut bar" |
| `count_club` | count historical peers meeting a condition | "8-rookie club since 2017"; "only 12 rookies" |
| `first_since` | most-recent prior occurrence of the feat | "most by a #2 pick in an SL debut since 2019" |

### Stage 2 — Notability & selection (choose the angle)

Each detector sets `notability` per its kind — **extremity/superlatives score highest**
(rank 1 or last, ≥95th / ≤5th pctl, "first/only/most"); mid-pack (~50th pctl) scores low.
So "best #1-pick start ever" beats "96th pctl" beats "three-time 20/65%" — **the angle is
chosen by extremity, not judgment.** Selection: collect all fired facts → sort by notability
→ take top-k, dedup overlapping angles (rank=1 subsumes its own percentile).
- **Notability floor:** below it a fact isn't "worth a sentence" (may still be a chip, or the
  surface uses the quiet-slate fallback).
- **k per surface:** hero tagline k=1; tick notes / Ledger echoes k=few, preferring **fresh**
  facts (changed since last tick); grade chips render regardless (a chip is not prose).

### Stage 3 — Realization (phrase it)

A **template registry** keyed by `fact.kind` (+ variant condition, e.g. `rank==1`). Each
template is a string with slots filled from `fact.values`. Example — `cohort_rank, rank==1`:

> "His **{value}** {metric} average through **{gp}** games is now the best start by a
> **{cohort_label}** in the sample — ahead of **{runner_up.who}** (**{runner_up.value}**)."

- **Variety without randomness:** 2–3 curated variants per kind, picked by a stable key
  (hash of subject) — phrasing varies, output stays deterministic.
- **LLM only offline:** if richer phrasings are wanted later, an LLM *authors the template
  library* once (human-reviewed); runtime only fills slots — never generates. No runtime
  hot takes.
- **Multi-surface realizers:** one Fact → prose (headline), a chip (percentile → grade
  chip), or a share card. Chips and prose can therefore never disagree.

### Where it runs / storage

Facts compute in **Job B (hourly tick)** after grades (§10 T2) exist; selected rendered
strings are written onto the projection rows (`hero_ref` on `event_desk_state`; a `facts`
json on T4 slate / T2 grade rows) so the page **reads strings**, never recomputes. The detector library is a
**projection** (rebuildable) and is **per-event** — SL detectors vs a future March-Madness
set — so it plugs into the Event Desk content-provider interface (§9 / framework doc).

---

## Pinned decisions

1. Storyline base weights ship as priors (§3); magnitude = consensus-rank prominence.
2. Duel prominence cutoff = consensus rank ≤14, fallback draft slot ≤14.
3. Streak = ≥3 straight games at/above cohort median with average run percentile ≥65.
4. Slot cohorts = lottery ±3 clamped to 1–14, late R1 15–30, R2 31–60, undrafted status.
5. Debut bar = mean GmSc.
6. Advanced tracker may include BPM from `SummerLeaguePlayerSeason`; missing/non-eligible
   BPM renders as an em-dash, not a broken column.
