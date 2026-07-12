"""Unit tests for Event Desk time helpers.

Covers ``format_et_clock`` -- the ``et_time`` Jinja filter used to render
``summer_league_games.tip_datetime`` (naive UTC) as an Eastern wall-clock
label. Regression guard for the slate-card bug where a naive-UTC value was
formatted directly and mislabeled ``ET`` (a 23:00 UTC tip showed as
``11:00 PM ET`` instead of ``7:00 PM ET``).
"""

from datetime import datetime

from app.services.event_desk.timeutils import format_et_clock


def test_summer_evening_tip_renders_edt() -> None:
    """A 23:00 UTC July tip is 7:00 PM EDT (UTC-4), not 11:00 PM."""
    assert format_et_clock(datetime(2026, 7, 10, 23, 0)) == "7:00 PM ET"


def test_winter_tip_renders_est_dst_aware() -> None:
    """The same 23:00 UTC instant in January is 6:00 PM EST (UTC-5)."""
    assert format_et_clock(datetime(2026, 1, 10, 23, 0)) == "6:00 PM ET"


def test_after_midnight_utc_rolls_back_to_previous_evening() -> None:
    """00:30 UTC is the prior evening in Eastern (8:30 PM EDT)."""
    assert format_et_clock(datetime(2026, 7, 10, 0, 30)) == "8:30 PM ET"


def test_noon_and_midnight_hour_labels() -> None:
    """The 12-hour clock renders noon/midnight as 12, not 0."""
    assert format_et_clock(datetime(2026, 7, 10, 16, 0)) == "12:00 PM ET"  # noon ET
    assert format_et_clock(datetime(2026, 7, 10, 4, 0)) == "12:00 AM ET"  # midnight ET


def test_none_renders_empty_string() -> None:
    """A missing tip time renders as empty, not a crash or 'None'."""
    assert format_et_clock(None) == ""


def test_aware_input_is_respected() -> None:
    """An already-aware UTC datetime converts the same as a naive one."""
    from datetime import timezone

    aware = datetime(2026, 7, 10, 23, 0, tzinfo=timezone.utc)
    assert format_et_clock(aware) == "7:00 PM ET"
