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
    """The freshness/staleness stamp shown on every Desk render (behavior spec §2).

    `state` is the honest verdict this stamp represents -- `"missing"` (no tick
    has ever run for this event), `"fresh"` (within the documented cadence), or
    `"stale"` (older than the documented cadence multiple; see
    `app.services.summer_league.desk_read.FRESHNESS_STALE_AFTER`). Compose logic
    lives entirely in `desk_read._freshness_for` so this dataclass never has to
    be re-derived/guessed by a template: a render must NEVER say "as of now"
    when `last_tick_at` is `None`.
    """

    # Naive UTC; None when no tick has ever run for this event ("missing").
    last_tick_at: Optional[datetime]
    next_tick_eta: Optional[datetime]
    # Honest, pre-rendered ET display stamp -- "freshness unavailable ..." when
    # `state == "missing"`, "as of 4:12pm ET" when fresh, "as of 4:12pm ET --
    # stale" when stale. Computed once by the read service so templates never
    # do timezone/staleness math themselves.
    as_of_et_label: str
    # One of "missing" / "fresh" / "stale". Defaults to "fresh" only so
    # call sites that predate this field (tests fixturing a plain "as of"
    # stamp) keep compiling -- `desk_read._freshness_for`, the one place this
    # is actually computed, always sets it explicitly.
    state: str = "fresh"
    # Pre-rendered "next update ~5:00pm ET" hint, or `None` when there's no
    # known next-tick ETA (e.g. `state == "missing"`). Meant to render AT MOST
    # ONCE per page -- callers must not also fold this into `as_of_et_label`.
    next_tick_eta_label: Optional[str] = None


@dataclass(frozen=True)
class DeskHeroLine:
    """One Live hero subject's tonight's running box line (#541).

    Populated only for the Live hero (`kind == "live_duel"`); every field is
    `None` pre-tip (the subject hasn't logged tonight's game yet) or when the
    subject didn't play -- the template renders `None` as an em dash, NEVER a
    zero or a career/event total (behavior spec: "pretip/missing values use
    em dashes, never event totals or zeros"). `gmsc` is tonight's single-game
    Hollinger Game Score (`app.services.summer_league.metrics.game_score_from_row`),
    not the event-aggregate GmSc T2 grades carry.
    """

    pts: Optional[int]
    reb: Optional[int]
    ast: Optional[int]
    gmsc: Optional[float]


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
    # Both subjects' tonight's running box line (#541) -- only populated for the
    # Live hero (`kind == "live_duel"`); `None` for every other hero kind, and
    # `subject_line_2` stays `None` whenever there's no second subject (a
    # single-subject Live hero, per the one-subject degradation rule above).
    subject_line: Optional[DeskHeroLine] = None
    subject_line_2: Optional[DeskHeroLine] = None


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
    pts: Optional[int] = None
    reb: Optional[int] = None
    ast: Optional[int] = None


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
