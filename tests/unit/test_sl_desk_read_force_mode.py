"""Unit tests for #544's Desk force-mode date override (`desk_read._effective_now`).

`_effective_now` is pure (settings + datetime, no session) -- it composes the
request/tick instant with `settings.sl_desk_force_date` (the Event Desk
framework doc's "config force-on/off & date override" lever), so it's
exercised directly here with `monkeypatch.setattr(settings, ...)` rather than
through a DB-backed integration test. See
``tests/integration/test_sl_desk_home.py`` for the end-to-end force-mode /
ownership-gate / freshness coverage that does need the database.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.summer_league.desk_read import _effective_now


def test_effective_now_passthrough_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no `sl_desk_force_date` set, an explicit `now` passes through unchanged."""
    monkeypatch.setattr(settings, "sl_desk_force_date", None)
    now = datetime(2026, 7, 10, 23, 5)
    assert _effective_now(now) == now


def test_effective_now_defaults_to_current_instant_when_now_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no override and no explicit `now`, it falls back to the current UTC instant."""
    monkeypatch.setattr(settings, "sl_desk_force_date", None)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    resolved = _effective_now(None)
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    assert before <= resolved <= after


def test_effective_now_date_override_replaces_date_keeps_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sl_desk_force_date` swaps the calendar date but preserves the time-of-day.

    This is the exact lever an operator uses to pin the Desk to a specific
    day (e.g. demoing a Wind-down Ledger) without waiting for the real
    calendar -- and it applies even when the caller passed an explicit `now`,
    since the override is meant to win over whatever the request clock says.
    """
    from datetime import date

    monkeypatch.setattr(settings, "sl_desk_force_date", date(2026, 7, 15))
    now = datetime(2020, 1, 1, 23, 5)  # an otherwise off-window instant.
    resolved = _effective_now(now)
    assert resolved == datetime(2026, 7, 15, 23, 5)


def test_effective_now_rejects_force_date_for_production_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled production write cannot silently replay a historical date."""
    from datetime import date

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "sl_desk_force_date", date(2026, 7, 15))

    with pytest.raises(RuntimeError, match="scheduled production Desk write"):
        _effective_now(datetime(2026, 7, 22, 12, 0), scheduled_write=True)


def test_effective_now_allows_force_date_for_staging_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staging remains an explicit QA/demo context for force-date writes."""
    from datetime import date

    monkeypatch.setattr(settings, "env", "stage")
    monkeypatch.setattr(settings, "sl_desk_force_date", date(2026, 7, 15))

    assert _effective_now(
        datetime(2026, 7, 22, 12, 0), scheduled_write=True
    ) == datetime(2026, 7, 15, 12, 0)
