"""Unit tests for combine stats service static config and formatting."""

from app.services.combine_stats_service import (
    METRIC_COLUMN_MAP,
    MetricColumnDef,
    get_all_metrics,
    get_metric_info,
    get_metrics_grouped,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.utils.combine_formatters import (
    format_agility_value,
    format_anthro_value,
    format_height_inches,
    format_inches,
    format_percentage,
    format_shooting_result,
    format_weight,
)


# === METRIC_COLUMN_MAP tests ===


def test_get_all_metrics_returns_all_entries() -> None:
    """All METRIC_COLUMN_MAP entries are returned by get_all_metrics."""
    metrics = get_all_metrics()
    assert len(metrics) == len(METRIC_COLUMN_MAP)
    keys = {m.key for m in metrics}
    assert keys == set(METRIC_COLUMN_MAP.keys())
    for m in metrics:
        assert m.display_name
        assert m.category in ("measurements", "athletic_testing")
        assert m.sort_direction in ("asc", "desc")


def test_get_metric_info_valid_key() -> None:
    """get_metric_info returns correct MetricInfo for a known key."""
    info = get_metric_info("wingspan_in")
    assert info is not None
    assert info.key == "wingspan_in"
    assert info.display_name == "Wingspan"
    assert info.unit == "in"
    assert info.category == "measurements"
    assert info.sort_direction == "desc"


def test_get_metric_info_invalid_key() -> None:
    """get_metric_info returns None for an unknown key."""
    assert get_metric_info("fake_metric") is None
    assert get_metric_info("") is None


def test_get_metrics_grouped_has_all_categories() -> None:
    """get_metrics_grouped returns dict with expected category keys."""
    groups = get_metrics_grouped()
    assert "measurements" in groups
    assert "athletic_testing" in groups
    total = sum(len(v) for v in groups.values())
    assert total == len(METRIC_COLUMN_MAP)


def test_metric_column_map_sort_directions() -> None:
    """Times and body_fat should sort asc; everything else desc."""
    asc_keys = {
        "lane_agility_time_s",
        "shuttle_run_s",
        "three_quarter_sprint_s",
        "body_fat_pct",
    }
    for key, defn in METRIC_COLUMN_MAP.items():
        if key in asc_keys:
            assert defn.sort_direction == "asc", f"{key} should be asc"
        else:
            assert defn.sort_direction == "desc", f"{key} should be desc"


def test_metric_column_map_tables_valid() -> None:
    """Every entry references a real table class with a real column."""
    valid_tables = {CombineAnthro, CombineAgility}
    for key, defn in METRIC_COLUMN_MAP.items():
        assert defn.table in valid_tables, f"{key} has unknown table"
        assert hasattr(
            defn.table, defn.column
        ), f"{key}: {defn.table.__name__} has no column '{defn.column}'"


# === Formatting tests ===


def test_format_height_inches_whole() -> None:
    """72 inches formats as 6'0\"."""
    assert format_height_inches(72.0) == "6'0\""


def test_format_height_inches_fractional() -> None:
    """94 inches formats as 7'10\"."""
    assert format_height_inches(94.0) == "7'10\""


def test_format_height_inches_quarter() -> None:
    """88.25 formats as 7'4.25\"."""
    assert format_height_inches(88.25) == "7'4.25\""


def test_format_height_inches_none() -> None:
    """None returns None."""
    assert format_height_inches(None) is None


def test_format_weight() -> None:
    """205.0 formats as '205 lbs'."""
    assert format_weight(205.0) == "205 lbs"


def test_format_weight_none() -> None:
    """None returns None."""
    assert format_weight(None) is None


def test_format_percentage() -> None:
    """5.3 formats as '5.3%'."""
    assert format_percentage(5.3) == "5.3%"


def test_format_percentage_none() -> None:
    """None returns None."""
    assert format_percentage(None) is None


def test_format_inches_whole() -> None:
    """10 formats as '10 in'."""
    assert format_inches(10.0) == "10 in"


def test_format_inches_fractional() -> None:
    """9.75 formats as '9.75 in'."""
    assert format_inches(9.75) == "9.75 in"


def test_format_inches_none() -> None:
    """None returns None."""
    assert format_inches(None) is None


def test_format_agility_value_time() -> None:
    """Sprint time formats with 2 decimal places and 's'."""
    assert format_agility_value("three_quarter_sprint_s", 3.04) == "3.04s"


def test_format_agility_value_vertical() -> None:
    """Vertical formats as inches."""
    assert format_agility_value("max_vertical_in", 44.5) == "44.5 in"


def test_format_agility_value_bench() -> None:
    """Bench press formats as reps."""
    assert format_agility_value("bench_press_reps", 24) == "24 reps"


def test_format_agility_value_none() -> None:
    """None returns None."""
    assert format_agility_value("lane_agility_time_s", None) is None


def test_format_anthro_value_wingspan() -> None:
    """Wingspan delegates to height formatter."""
    result = format_anthro_value("wingspan_in", 94.0)
    assert result == "7'10\""


def test_format_anthro_value_weight() -> None:
    """Weight formats with lbs."""
    result = format_anthro_value("weight_lb", 250.0)
    assert result == "250 lbs"


def test_format_anthro_value_body_fat() -> None:
    """Body fat formats as percentage."""
    result = format_anthro_value("body_fat_pct", 5.3)
    assert result == "5.3%"


def test_format_anthro_value_hand() -> None:
    """Hand measurements format as inches."""
    result = format_anthro_value("hand_width_in", 10.25)
    assert result == "10.25 in"


def test_format_anthro_value_none() -> None:
    """None returns None."""
    assert format_anthro_value("wingspan_in", None) is None


def test_format_shooting_result() -> None:
    """12/15 formats as '12/15 (80.0%)'."""
    assert format_shooting_result(12, 15) == "12/15 (80.0%)"


def test_format_shooting_result_zero_fga() -> None:
    """0 FGA returns just the ratio."""
    assert format_shooting_result(0, 0) == "0/0"


def test_format_shooting_result_none() -> None:
    """None inputs return None."""
    assert format_shooting_result(None, 15) is None
    assert format_shooting_result(12, None) is None
