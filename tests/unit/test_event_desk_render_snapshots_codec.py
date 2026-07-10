"""Codec tests for `app.services.event_desk.render_snapshots` (launch-readiness item 9).

Pure round-trip coverage of `serialize_desk_view`/`deserialize_desk_view` over
realistic Preview/Live/Recap `DeskView` fixtures -- no database. Each test asserts
the decoded `DeskView` is field-for-field equal to the original, including enum
values stored as strings, datetimes, nullable fields, and list ordering.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.event_desk.payload import (
    DeskFreshness,
    DeskHero,
    DeskLedgerRow,
    DeskLiveBoardRow,
    DeskPayload,
    DeskSlateRow,
    DeskTrackerRow,
    DeskTrackerSection,
)
from app.services.event_desk.render_snapshots import (
    CURRENT_SCHEMA_VERSION,
    UnsupportedRenderSnapshotSchemaVersion,
    deserialize_desk_payload,
    deserialize_desk_view,
    serialize_desk_payload,
    serialize_desk_view,
)
from app.services.summer_league.desk_read import DeskView


def _freshness() -> DeskFreshness:
    return DeskFreshness(
        last_tick_at=datetime(2026, 7, 10, 19, 0),
        next_tick_eta=datetime(2026, 7, 10, 20, 0),
        as_of_et_label="as of 3:00pm ET",
    )


def _preview_view() -> DeskView:
    """A Morning-Card-shaped DeskView fixture (pre-tip: slate present, no live board)."""
    hero = DeskHero(
        kind="marquee",
        game_id=9,
        subject_player_id=101,
        subject_player_id_2=102,
        headline="Two lottery picks share the floor tonight.",
        tagline="7:00pm ET tip",
        facts=[{"kind": "prominence", "rank": 3}],
    )
    payload = DeskPayload(
        daily_state="preview",
        is_home_owner=True,
        hero=hero,
        slate=[
            DeskSlateRow(
                game_id=10,
                matchup_label="CHA vs ORL",
                status="scheduled",
                tip_datetime=datetime(2026, 7, 10, 23, 0),
                weight=60.0,
                read=None,
            ),
            DeskSlateRow(
                game_id=11,
                matchup_label="NYK vs MIA",
                status="scheduled",
                tip_datetime=None,
                weight=12.5,
                read="Bottom of the slate.",
            ),
        ],
        live_board=[],
        ledger=[],
        tracker=DeskTrackerSection(
            cohort="lottery",
            stat_view="box",
            rows=[
                DeskTrackerRow(
                    player_id=101,
                    display_name="Prospect One",
                    identity_label="#5 · NOP · G",
                    gp=3,
                    minutes=72.0,
                    gmsc=18.2,
                    grade="warm",
                    stat_columns={"pts": 20.0, "reb": None},
                ),
            ],
            truncated=False,
        ),
        freshness=_freshness(),
    )
    return DeskView(
        payload=payload,
        players={
            101: {
                "display_name": "Prospect One",
                "slug": "prospect-one",
                "photo_url": None,
                "draft_tag": "Pick 5",
            },
            102: {
                "display_name": "Prospect Two",
                "slug": "prospect-two",
                "photo_url": "https://example.com/p2.png",
                "draft_tag": "Undrafted",
            },
        },
        matchups={
            9: {
                "home": {"abbrev": "NOP", "logo_url": "https://example.com/nop.png"},
                "away": {"abbrev": "SAS", "logo_url": None},
            },
        },
        tracker_teams={101: {"abbrev": "NOP", "logo_url": None}},
    )


def _live_view() -> DeskView:
    """A Live-Desk-shaped DeskView fixture (in-progress: live board populated)."""
    hero = DeskHero(
        kind="live_duel",
        game_id=8,
        subject_player_id=101,
        subject_player_id_2=None,
        headline="Leads all rookies through three quarters.",
        tagline=None,
        facts=[],
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
                tip_datetime=datetime(2026, 7, 10, 23, 0),
                weight=60.0,
                read=None,
            ),
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
            ),
            DeskLiveBoardRow(
                game_id=9,
                matchup_label="CHA vs ORL",
                status="scheduled",
                home_score=None,
                away_score=None,
                top_performer_player_id=None,
                top_performer_gmsc=None,
                read=None,
            ),
        ],
        ledger=[],
        tracker=DeskTrackerSection(cohort="round1", stat_view="per36", rows=[]),
        freshness=_freshness(),
    )
    return DeskView(
        payload=payload,
        players={101: {"display_name": "Prospect One", "slug": "prospect-one"}},
        matchups={
            8: {
                "home": {"abbrev": "NYK", "logo_url": None},
                "away": {"abbrev": "MIA", "logo_url": None},
            },
        },
        tracker_teams={},
    )


def _recap_view() -> DeskView:
    """A Ledger-shaped DeskView fixture (post-final: ledger populated, no slate/live)."""
    hero = DeskHero(
        kind="performance_of_night",
        game_id=8,
        subject_player_id=101,
        subject_player_id_2=None,
        headline="Best debut by a #1 pick since 2019.",
        tagline=None,
        facts=[{"kind": "debut"}],
    )
    payload = DeskPayload(
        daily_state="recap",
        is_home_owner=True,
        hero=hero,
        slate=[],
        live_board=[],
        ledger=[
            DeskLedgerRow(
                game_id=8,
                player_id=101,
                gmsc=24.5,
                pctl=96.0,
                grade="hot",
                read="Best debut by a #1 pick since 2019.",
            ),
            DeskLedgerRow(
                game_id=8,
                player_id=102,
                gmsc=10.1,
                pctl=40.0,
                grade="cold",
                read=None,
            ),
        ],
        tracker=DeskTrackerSection(
            cohort="undrafted",
            stat_view="advanced",
            rows=[],
            truncated=True,
        ),
        freshness=_freshness(),
    )
    return DeskView(
        payload=payload,
        players={
            101: {"display_name": "Prospect One", "slug": "prospect-one"},
            102: {"display_name": "Prospect Two", "slug": "prospect-two"},
        },
        matchups={},
        tracker_teams={},
    )


@pytest.mark.parametrize(
    "make_view", [_preview_view, _live_view, _recap_view], ids=["preview", "live", "recap"]
)
def test_desk_view_round_trips_losslessly(make_view: object) -> None:
    """Preview/Live/Recap DeskView fixtures survive encode -> decode unchanged."""
    view = make_view()  # type: ignore[operator]
    payload_json, view_context_json = serialize_desk_view(view)
    assert payload_json is not None

    decoded = deserialize_desk_view(
        payload_json=payload_json,
        view_context_json=view_context_json,
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    assert decoded == view


def test_serialize_desk_view_preserves_slate_row_ordering() -> None:
    """List order (slate rows) is not silently reshuffled through JSON encoding."""
    view = _preview_view()
    payload_json, _ = serialize_desk_view(view)
    assert payload_json is not None
    game_ids = [row["game_id"] for row in payload_json["slate"]]
    assert game_ids == [10, 11]


def test_serialize_desk_view_preserves_nullable_fields() -> None:
    """None-valued fields (tip_datetime, tagline, gmsc/grade) round-trip as None, not dropped."""
    view = _preview_view()
    payload_json, _ = serialize_desk_view(view)
    assert payload_json is not None
    assert payload_json["slate"][1]["tip_datetime"] is None

    decoded = deserialize_desk_view(
        payload_json=payload_json,
        view_context_json=serialize_desk_view(view)[1],
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    assert decoded.payload is not None
    assert decoded.payload.slate[1].tip_datetime is None
    assert decoded.payload.tracker.rows[0].stat_columns["reb"] is None


def test_serialize_desk_view_preserves_datetime_values() -> None:
    """Datetime fields round-trip to the exact same instant, not a truncated/shifted one."""
    view = _live_view()
    payload_json, _ = serialize_desk_view(view)
    assert payload_json is not None
    decoded_payload = deserialize_desk_payload(payload_json)
    assert decoded_payload.freshness.last_tick_at == datetime(2026, 7, 10, 19, 0)
    assert decoded_payload.slate[0].tip_datetime == datetime(2026, 7, 10, 23, 0)


def test_serialize_desk_view_encodes_int_keyed_lookups_as_json_safe() -> None:
    """Player/matchup/tracker-team dicts (int keys) become JSON-safe string keys."""
    view = _preview_view()
    _, view_context_json = serialize_desk_view(view)
    assert "101" in view_context_json["players"]
    assert 101 not in view_context_json["players"]


def test_deserialize_desk_view_rejects_off_window_none_payload() -> None:
    """A view with no payload (off-window) encodes payload_json as None and decodes back."""
    view = DeskView(payload=None, players={}, matchups={}, tracker_teams={})
    payload_json, view_context_json = serialize_desk_view(view)
    assert payload_json is None

    decoded = deserialize_desk_view(
        payload_json=payload_json,
        view_context_json=view_context_json,
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    assert decoded.payload is None


def test_deserialize_desk_view_rejects_unknown_schema_version() -> None:
    """An unrecognized schema_version raises a typed error instead of misdecoding."""
    view = _preview_view()
    payload_json, view_context_json = serialize_desk_view(view)

    with pytest.raises(UnsupportedRenderSnapshotSchemaVersion) as exc_info:
        deserialize_desk_view(
            payload_json=payload_json,
            view_context_json=view_context_json,
            schema_version=999,
        )
    assert exc_info.value.schema_version == 999


def test_serialize_desk_payload_matches_deserialize_desk_payload_round_trip() -> None:
    """The narrower payload-only codec pair also round-trips (used when no view-context)."""
    payload = _recap_view().payload
    assert payload is not None
    encoded = serialize_desk_payload(payload)
    decoded = deserialize_desk_payload(encoded)
    assert decoded == payload
