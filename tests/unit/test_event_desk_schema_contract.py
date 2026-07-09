"""Unit checks for the generic Event Desk registry/state schema contract."""

from __future__ import annotations

from app.schemas.event_desk import (
    Event,
    EventCalendarSource,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
    EventType,
)


def test_event_lifecycle_phase_enum_matches_framework_doc() -> None:
    """Outer state machine phases stay aligned with the Event Desk framework doc."""
    assert [phase.value for phase in EventLifecyclePhase] == [
        "dormant",
        "announced",
        "warmup",
        "active",
        "winddown",
        "archived",
    ]


def test_event_daily_state_enum_matches_framework_doc() -> None:
    """Inner state machine states stay aligned with the Event Desk framework doc."""
    assert [state.value for state in EventDailyState] == ["preview", "live", "recap"]


def test_event_calendar_source_enum_matches_framework_doc() -> None:
    """Calendar source stays aligned with the framework doc's two supported sources."""
    assert [source.value for source in EventCalendarSource] == ["schedule", "config"]


def test_event_type_enum_includes_pro_summer() -> None:
    """SL's concrete config (event_type: "pro_summer") is a valid EventType member."""
    assert EventType.PRO_SUMMER.value == "pro_summer"
    assert "pro_summer" in [member.value for member in EventType]


def test_events_table_contract_names_constraints_and_indexes() -> None:
    """events registry table exposes the expected table name, constraint, and index."""
    table = Event.__table__  # type: ignore[attr-defined]

    assert table.name == "events"
    assert {
        constraint.name for constraint in table.constraints if constraint.name is not None
    } >= {"uq_events_key"}
    assert {index.name for index in table.indexes} >= {"ix_events_is_active_priority"}
    assert table.c.event_type.type.name == "event_type_enum"
    assert table.c.calendar_source.type.name == "event_calendar_source_enum"


def test_event_desk_state_table_contract_names_constraints_and_indexes() -> None:
    """event_desk_state table exposes the expected table name, constraint, and index."""
    table = EventDeskState.__table__  # type: ignore[attr-defined]

    assert table.name == "event_desk_state"
    assert {
        constraint.name for constraint in table.constraints if constraint.name is not None
    } >= {"uq_event_desk_state_event"}
    assert {index.name for index in table.indexes} >= {"ix_event_desk_state_home_owner"}
    assert table.c.lifecycle_phase.type.name == "event_lifecycle_phase_enum"
    assert table.c.daily_state.type.name == "event_daily_state_enum"
    assert table.c.daily_state.nullable is True


def test_event_desk_state_references_events_by_foreign_key() -> None:
    """event_desk_state.event_id is a foreign key into events.id (single-row-per-event)."""
    table = EventDeskState.__table__  # type: ignore[attr-defined]
    fk_targets = {
        f"{fk.column.table.name}.{fk.column.name}" for fk in table.c.event_id.foreign_keys
    }
    assert fk_targets == {"events.id"}
