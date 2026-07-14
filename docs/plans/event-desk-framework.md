# Event Desk Framework — general event-based state machine

The Summer League Desk is not "the Summer League module" — it is **event-instance #1** of a
generic, repeatable **Event Desk**: a home-page takeover that any basketball event
(Summer League, FIBA, AAU, U17 Worlds, March Madness, …) plugs into. This doc specs the
event-agnostic framework; the SL specifics live in
`summer-league-scouts-desk-behavior-spec.md` (that daily machine is the *inner* machine
below).

**Roadmap fit:** this is the draft-calendar pipeline made real, sitting on the
journey-graph backbone — each event is a **participation-grain spoke**, and a desk is a
**projection** filtered to whichever event is Active. Cohort bases reuse the graph's
affiliation/participation assertions.

---

## Two nested state machines

### Outer — Event Lifecycle (per event, event-agnostic)

```
Dormant → Announced → Warm-up → Active → Wind-down → Archived
```

| Phase | Meaning | Home page |
|-------|---------|-----------|
| Dormant | far off / long over | standard home |
| Announced | on the calendar within `ANNOUNCE_HORIZON` | optional teaser/countdown (growth lever) |
| Warm-up | within `pre_roll_days` of first game | teaser + participant/bracket reveals |
| Active | games happening | **runs the inner machine** |
| Wind-down | last final + `post_roll_days` tail | final recap persists |
| Archived | tail elapsed | seasonal module disappears; evergreen stat pages remain |

Transitions come from each event's **calendar + window priors** — the per-event knobs that
replace SL's would-be hard-coded window:
`{ announce_horizon_days, pre_roll_days, gap_bridge_days, post_roll_days, morning_lead_h, morning_floor_et }`.

`announce_horizon_days` fires **Dormant → Announced** — how far out the event first appears
as a passive teaser/countdown (vs `pre_roll_days`, the tighter Warm-up window with actual
reveals). A **growth lever**, and per-event (March Madness warrants a longer runway than an
AAU weekend).

`gap_bridge_days` keeps a multi-sub-event cluster (e.g. CA Classic → SLC → Vegas) as **one
contiguous Active window** — gaps ≤ threshold don't drop to Wind-down and flicker back.

### Inner — Daily Coverage (only during Active)

```
Preview → Live → Recap
```

This *is* the SL Morning / Live / Ledger machine, renamed event-neutral. Fully specified
(game-status driven; schedule-relative Preview flip; quiet-slate rule) in the SL behavior
spec §2 — those rules are generic and lifted here. Every event has this shape; only the
content differs (March Madness Preview = today's bracket slate; Live = games in progress;
Recap = who advanced + bracket-busters).

---

## The seam: an Event registry

The machines are shared; a registered **Event object** is what varies:

| Shared (framework) | Per-event (registered object) |
|--------------------|-------------------------------|
| Both state machines + transition logic | **calendar_source**: `schedule` (data-driven) or `config` (dates) |
| Home-page controller + precedence | **calendar_ref**: schedule filter or config date range |
| Tick / freshness plumbing | **window priors** (above) |
| Off-window fallback | **content_providers**: hero, storyline triggers, spine boards, daily-state renderers |
| | **cohort_basis**: how "vs cohort" is computed (SL=draft-slot; March Madness=seed/round; FIBA/U17=age-group/national-team; AAU=circuit) |
| | **priority**: home-page precedence weight |

The framework supports **both calendar sources per event** — schedule-driven where we
ingest a feed (SL via nba_stats), config-dates where we don't (early AAU/U17).

### Entity mapping guardrail

`events` is a desk-control registry, **not** a parallel sports data model. For Summer League
V1, the registered `events` row points at the existing Summer League `competition_id` via
`calendar_ref` and all content projections keep storing `competition_id` / `game_id`.
Future journey-graph work should map Event Desk rows to the canonical
`competition -> edition -> game -> team_entry -> participation` spine rather than creating
new event-only identity, schedule, or participation records.

---

## EventDesk controller (each tick)

1. For every registered event → compute its lifecycle phase from `calendar + game status`.
2. Collect **home-eligible** events (phase ∈ {Warm-up, Active, Wind-down}).
3. **Home owner = highest `priority` among home-eligible** (single owner). Others stay
   reachable via nav; they do **not** take the hero.
   - Tie-break: phase (Active > Warm-up > Wind-down), then nearest game.
4. Render the owner's phase-appropriate content via its providers. If none home-eligible →
   standard home page.

### Overlap precedence ✅ single owner by priority

When events overlap (July: SL + FIBA U17; spring: March Madness + AAU), exactly one owns
the takeover per `priority`. One clear headline; no secondary strip / rotation in V1.

`priority` = a static integer per event encoding draft-relevance × audience. Example scale:
March Madness 100 · Summer League 80 · Combine 70 · FIBA senior 65 · U17/U19 55 · AAU/EYBL 40.

**Principle:** prefer live/active content over teasers — a merely *Announced* big event
should not bury an *Active* smaller one — with static `priority` deciding among comparable
events. **Moot in V1** (SL unopposed); the exact cross-phase tie-break is deferred to
event #2, when a real overlap makes it concrete.

---

## V1 scope ✅ design seams now, wire only SL

**Build now (generic):**
- The two state machines + transition logic.
- An `events` registry + `event_desk_state` (the only state/freshness table).
- The EventDesk controller with single-owner-by-priority (trivial with one event, but the
  seam exists).
- A thin **content-provider interface** that SL implements.

**Register now:** Summer League only (`priority` unopposed).

**Deferred until event #2 (don't over-abstract from one example):**
- Generalizing the *content-projection* tables — SL's cohort-baseline / storyline / slate /
  grade tables (SL spec §10 T1–T4) stay SL-namespaced as event #1's provider projections
  until a 2nd event reveals the common shape.
- Config-only calendar handling beyond SL's needs.
- Any secondary-strip / rotation rendering (not chosen).

---

## SL as event instance #1 (concrete config)

```
Event {
  key:            "summer_league",
  event_type:     "pro_summer",
  calendar_source: "schedule",           # nba_stats SL games + config override
  window priors:  { announce_horizon_days: 14, pre_roll_days: 3, gap_bridge_days: 4,
                    post_roll_days: 2, morning_lead_h: 6, morning_floor_et: "09:00" },
  content_providers: { hero: [marquee | live-duel | perf-of-night],
                       storyline_triggers: [debut, duel, streak, status_heat, second_look],
                       spine: [class_tracker], daily_states: [preview, live, recap] },
  cohort_basis:   "slot_window",
  priority:       100,                    # unopposed in V1
}
```

- **Window source** = schedule-driven with a config force-on/off & date override.
- **Window scope** = one contiguous July window (`gap_bridge_days` absorbs CA→SLC→Vegas gaps).
- **Pre-roll** = a slim "SL tips off [date]" teaser in Announced/Warm-up — **P2**, consistent
  with Roster Wire being P2; the full inner machine takes over once the first slate is set.
- **"SL mode" toggles the home-page takeover only** — the Explorer / Leaders / game / player
  stat pages are evergreen and never gated by the window.

---

## Data model delta (framework-level, on top of SL spec §10)

Two new **generic** tables; SL's content projections (T1–T4) unchanged.

### `events` (registry)
- `id` PK · `key` str (stable series key, e.g. `summer_league`) · `name` · `event_type` enum
- `calendar_source` enum (`schedule`|`config`) · `calendar_ref` json
- `window_priors` json · `cohort_basis` str · `priority` int · `is_active` bool

### `event_desk_state`
- `id` PK · `event_id` FK → events · `as_of` datetime
- `lifecycle_phase` enum (`dormant`|`announced`|`warmup`|`active`|`winddown`|`archived`)
- `daily_state` Optional enum (`preview`|`live`|`recap`) — only in Active
- `is_home_owner` bool · `hero_ref` json · `freshness_tick_at` · `next_tick_eta`
- upserted each tick by the EventDesk controller.

---

## Open (framework-level)

- Exact cross-phase precedence tie-break (Active-beats-teaser is the principle; the precise
  rule waits for event #2's real overlap). Moot in V1.
- SL's Announced-phase teaser is **P2** — `announce_horizon_days` is set (14) but the teaser
  UI ships only if the main states land early.
