"""DeskPayload — the typed UI contract the desk read service and templates consume.

Pins the shape of a rendered Summer League Desk (behavior spec §1-§7) early so
downstream tickets (#508 desk read service, #509 Desk states UI, #511 Class Tracker
UI) can author templates against fixtures before the read service exists. These are
plain, immutable dataclasses — not SQLModel tables or Pydantic API models — because
nothing here is persisted or exposed as a JSON API response; it's an internal
read-model assembled from `event_desk_state` + T2 (`summer_league_desk_player_grades`)
+ T4 (`summer_league_desk_slate`) + the Class Tracker read service, and handed
straight to Jinja.

One `DeskPayload` == one fully-resolved render of the module for the event's current
`daily_state` (behavior spec §1: exactly one of Morning Card / Live Desk / The Ledger
renders — there is no user-facing state switcher). Field groups map 1:1 to the
mockup's sections:

* `hero` — Morning marquee / Live key matchup / Ledger performance-of-the-night /
  quiet-slate fallback (behavior spec §4).
* `slate` — "The Rest of Tonight's Slate" (behavior spec §5).
* `live_board` — the Live Desk's all-games board (behavior spec §1 "Live tick board").
* `ledger` — The Ledger's top-performers list (behavior spec §1).
* `tracker` — the pinned Class Tracker (behavior spec §7).
* `freshness` — the last-tick/next-tick stamp (behavior spec §2 "Refresh").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class DeskFreshness:
    """The freshness/staleness stamp shown on every Desk render (behavior spec §2)."""

    # Naive UTC; None before the first tick has ever run for this event.
    last_tick_at: Optional[datetime]
    next_tick_eta: Optional[datetime]
    # Pre-rendered ET display stamp, e.g. "as of 4:12pm ET" — computed once by the
    # read service so templates never do timezone math.
    as_of_et_label: str


@dataclass(frozen=True)
class DeskHero:
    """The single featured hero for the current `daily_state` (behavior spec §4).

    `kind` distinguishes which selection rule produced this hero: `"marquee"`
    (Morning face-off), `"live_duel"` (Live key matchup), `"performance_of_night"`
    (Ledger), or `"quiet_slate"` (the always-force-a-headline fallback promoting the
    class leader / biggest mover-to-date when nothing today clears a storyline
    threshold).
    """

    kind: str
    game_id: Optional[int]
    subject_player_id: Optional[int]
    # Second subject for a two-person face-off/duel hero; None for a single-subject
    # hero (behavior spec §4: "degrades to a single-subject hero").
    subject_player_id_2: Optional[int]
    headline: str
    tagline: Optional[str]
    # Rendered commentary-engine Fact records (§11) backing `headline`/`tagline`,
    # kept as plain dicts here since the Fact dataclass itself belongs to the
    # commentary engine ticket, not this framework-level contract.
    facts: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class DeskSlateRow:
    """One row of "The Rest of Tonight's Slate" (behavior spec §5)."""

    game_id: int
    matchup_label: str
    status: str
    tip_datetime: Optional[datetime]
    weight: float
    read: Optional[str]


@dataclass(frozen=True)
class DeskLiveBoardRow:
    """One row of the Live Desk's all-games board (behavior spec §1 "Live tick board").

    `top_performer_*` fields are the highest-live-GmSc tracked player in the game so
    far; both are None pre-tip (rendered as an em-dash per behavior spec §1).
    """

    game_id: int
    matchup_label: str
    status: str
    home_score: Optional[int]
    away_score: Optional[int]
    top_performer_player_id: Optional[int]
    top_performer_gmsc: Optional[float]
    read: Optional[str]


@dataclass(frozen=True)
class DeskLedgerRow:
    """One row of The Ledger's performers list (behavior spec §1)."""

    game_id: int
    player_id: int
    gmsc: float
    pctl: float
    grade: str
    read: Optional[str]


@dataclass(frozen=True)
class DeskTrackerRow:
    """One Class Tracker row (behavior spec §7).

    `identity_label` swaps format by cohort: draft-slot cohorts render
    `"#5 · NOP · G"`; the Undrafted cohort renders `"Undrafted · LAL"`.
    `stat_columns` holds the curated, stat-view-dependent middle block (Box /
    Per-36 / Per-100 / Advanced — behavior spec §7's column taxonomy table); the
    fixed frame (player/GP/MIN/GmSc/grade) is this dataclass's other fields.
    """

    player_id: int
    display_name: str
    identity_label: str
    gp: int
    minutes: float
    gmsc: Optional[float]
    grade: Optional[str]
    stat_columns: dict[str, Optional[float]] = field(default_factory=dict)


@dataclass(frozen=True)
class DeskTrackerSection:
    """The pinned Class Tracker: active cohort/stat-view toggle state + rows (behavior spec §7).

    `cohort` is one of `"lottery"` / `"round1"` / `"round2"` / `"full_class"` /
    `"sophomores"` / `"undrafted"`; `stat_view` is one of `"box"` / `"per36"` /
    `"per100"` / `"advanced"`. Both are server-round-trip query-param state (behavior
    spec §7 "Toggle interaction model"), not client-side.
    """

    cohort: str
    stat_view: str
    rows: list[DeskTrackerRow] = field(default_factory=list)
    # True when the cohort exceeded the 30-row cap and rows were truncated to the
    # top 30 by the active sort (behavior spec §7 "Variable length, capped at 30").
    truncated: bool = False


@dataclass(frozen=True)
class DeskPayload:
    """The full Desk read-model one render is assembled from (behavior spec §1-§7).

    `daily_state` is the resolver's verdict (`"preview"` / `"live"` / `"recap"`,
    mirroring `event_desk_state.daily_state`'s enum values) and determines which of
    `hero`/`slate`/`live_board`/`ledger` the template actually renders — all four
    sections are always present on the dataclass (so downstream code never branches
    on missing attributes) but only the sections relevant to `daily_state` carry
    real rows; the rest are empty. `tracker` and `freshness` render unconditionally
    on every state (the Class Tracker is pinned; the freshness stamp always shows).
    """

    daily_state: str
    is_home_owner: bool
    hero: DeskHero
    slate: list[DeskSlateRow]
    live_board: list[DeskLiveBoardRow]
    ledger: list[DeskLedgerRow]
    tracker: DeskTrackerSection
    freshness: DeskFreshness
