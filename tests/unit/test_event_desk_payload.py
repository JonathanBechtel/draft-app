"""Contract tests for the DeskPayload dataclasses (`app/services/event_desk/payload.py`).

These dataclasses are the UI contract downstream tickets (#508 desk read service,
#509 Desk states UI, #511 Class Tracker UI) author templates against before the
read service exists. Constructing one of each here both exercises the module and
locks the field names/defaults in place — a rename here is a deliberate, visible
break to those tickets' fixtures, not a silent one.
"""

from __future__ import annotations

from datetime import datetime

from app.services.event_desk.payload import (
    DeskFreshness,
    DeskHero,
    DeskHeroLine,
    DeskLedgerRow,
    DeskLiveBoardRow,
    DeskPayload,
    DeskSlateRow,
    DeskTrackerRow,
    DeskTrackerSection,
)


def test_desk_freshness_fields() -> None:
    freshness = DeskFreshness(
        last_tick_at=datetime(2026, 7, 9, 20, 0),
        next_tick_eta=datetime(2026, 7, 9, 21, 0),
        as_of_et_label="as of 4:00pm ET",
    )
    assert freshness.last_tick_at == datetime(2026, 7, 9, 20, 0)
    assert freshness.next_tick_eta == datetime(2026, 7, 9, 21, 0)
    assert freshness.as_of_et_label == "as of 4:00pm ET"


def test_desk_freshness_allows_no_prior_tick() -> None:
    freshness = DeskFreshness(last_tick_at=None, next_tick_eta=None, as_of_et_label="—")
    assert freshness.last_tick_at is None


def test_desk_hero_defaults_empty_facts() -> None:
    hero = DeskHero(
        kind="marquee",
        game_id=42,
        subject_player_id=101,
        subject_player_id_2=102,
        headline="A tale-of-the-tape headline.",
        tagline=None,
    )
    assert hero.kind == "marquee"
    assert hero.subject_player_id_2 == 102
    assert hero.facts == []


def test_desk_hero_defaults_no_subject_lines() -> None:
    """#541: a non-Live hero (e.g. Morning marquee) never carries running lines."""
    hero = DeskHero(
        kind="marquee",
        game_id=42,
        subject_player_id=101,
        subject_player_id_2=102,
        headline="A tale-of-the-tape headline.",
        tagline=None,
    )
    assert hero.subject_line is None
    assert hero.subject_line_2 is None


def test_desk_hero_line_carries_nullable_pts_reb_ast_gmsc() -> None:
    """#541 typed hero contract: PTS/REB/AST/GmSc are individually nullable."""
    line = DeskHeroLine(pts=18, reb=6, ast=4, gmsc=21.3)
    assert (line.pts, line.reb, line.ast, line.gmsc) == (18, 6, 4, 21.3)

    pretip = DeskHeroLine(pts=None, reb=None, ast=None, gmsc=None)
    assert (pretip.pts, pretip.reb, pretip.ast, pretip.gmsc) == (None, None, None, None)


def test_desk_hero_live_duel_carries_both_subjects_running_lines() -> None:
    hero = DeskHero(
        kind="live_duel",
        game_id=8,
        subject_player_id=101,
        subject_player_id_2=102,
        headline="Two lottery picks trade buckets in the third.",
        tagline=None,
        subject_line=DeskHeroLine(pts=18, reb=6, ast=4, gmsc=21.3),
        subject_line_2=DeskHeroLine(pts=None, reb=None, ast=None, gmsc=None),
    )
    assert hero.subject_line is not None
    assert hero.subject_line.pts == 18
    assert hero.subject_line_2 is not None
    assert hero.subject_line_2.pts is None  # pretip subject -- em dash at render time


def test_desk_hero_single_subject_has_no_second_player() -> None:
    hero = DeskHero(
        kind="quiet_slate",
        game_id=None,
        subject_player_id=101,
        subject_player_id_2=None,
        headline="Dybantsa still leads the class.",
        tagline=None,
        facts=[{"kind": "leads_field"}],
    )
    assert hero.subject_player_id_2 is None
    assert hero.facts == [{"kind": "leads_field"}]


def test_desk_slate_row_fields() -> None:
    row = DeskSlateRow(
        game_id=7,
        matchup_label="LAL vs BOS",
        status="scheduled",
        tip_datetime=datetime(2026, 7, 9, 22, 0),
        weight=145.0,
        read="Two lottery picks share the floor tonight.",
    )
    assert row.game_id == 7
    assert row.weight == 145.0


def test_desk_live_board_row_pre_tip_has_no_top_performer() -> None:
    row = DeskLiveBoardRow(
        game_id=8,
        matchup_label="NYK vs MIA",
        status="scheduled",
        home_score=None,
        away_score=None,
        top_performer_player_id=None,
        top_performer_gmsc=None,
        read=None,
    )
    assert row.top_performer_player_id is None
    assert row.read is None


def test_desk_ledger_row_fields() -> None:
    row = DeskLedgerRow(
        game_id=8,
        player_id=101,
        gmsc=24.5,
        pctl=96.0,
        grade="hot",
        read="Best debut by a #1 pick since 2019.",
    )
    assert row.grade == "hot"
    assert row.pctl == 96.0


def test_desk_tracker_row_defaults_empty_stat_columns() -> None:
    row = DeskTrackerRow(
        player_id=101,
        display_name="Test Player",
        identity_label="#5 · NOP · G",
        gp=3,
        minutes=72.0,
        gmsc=18.2,
        grade="warm",
    )
    assert row.stat_columns == {}


def test_desk_tracker_section_defaults() -> None:
    section = DeskTrackerSection(cohort="lottery", stat_view="box")
    assert section.rows == []
    assert section.truncated is False


def test_desk_payload_assembles_every_section() -> None:
    freshness = DeskFreshness(
        last_tick_at=datetime(2026, 7, 9, 20, 0),
        next_tick_eta=datetime(2026, 7, 9, 21, 0),
        as_of_et_label="as of 4:00pm ET",
    )
    hero = DeskHero(
        kind="live_duel",
        game_id=8,
        subject_player_id=101,
        subject_player_id_2=102,
        headline="Two lottery picks trade buckets in the third.",
        tagline=None,
    )
    tracker = DeskTrackerSection(
        cohort="undrafted",
        stat_view="advanced",
        rows=[
            DeskTrackerRow(
                player_id=201,
                display_name="Undrafted Player",
                identity_label="Undrafted · LAL",
                gp=2,
                minutes=40.0,
                gmsc=15.0,
                grade="warm",
                stat_columns={"ts_pct": 0.58},
            )
        ],
    )
    payload = DeskPayload(
        daily_state="live",
        is_home_owner=True,
        hero=hero,
        slate=[
            DeskSlateRow(
                game_id=9,
                matchup_label="CHA vs ORL",
                status="in_progress",
                tip_datetime=datetime(2026, 7, 9, 23, 0),
                weight=60.0,
                read=None,
            )
        ],
        live_board=[
            DeskLiveBoardRow(
                game_id=8,
                matchup_label="NYK vs MIA",
                status="in_progress",
                home_score=44,
                away_score=41,
                top_performer_player_id=101,
                top_performer_gmsc=19.5,
                read="Leads all rookies tonight.",
            )
        ],
        ledger=[],
        tracker=tracker,
        freshness=freshness,
    )
    assert payload.daily_state == "live"
    assert payload.is_home_owner is True
    assert payload.hero is hero
    assert len(payload.slate) == 1
    assert len(payload.live_board) == 1
    assert payload.ledger == []
    assert payload.tracker.cohort == "undrafted"
    assert payload.tracker.rows[0].stat_columns == {"ts_pct": 0.58}
    assert payload.freshness.as_of_et_label == "as of 4:00pm ET"
