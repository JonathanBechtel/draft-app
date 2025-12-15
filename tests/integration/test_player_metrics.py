"""Integration tests for player metrics API endpoint."""

import pytest
from sqlalchemy import select

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    MetricStatistic,
)
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_shooting import CombineShooting
from app.schemas.players_master import PlayerMaster
from app.schemas.player_status import PlayerStatus
from app.schemas.positions import Position
from app.schemas.seasons import Season


async def _create_position(db_session, code: str, parents: list[str]) -> Position:
    position = Position(code=code, description=code, parents=parents)
    db_session.add(position)
    await db_session.flush()
    return position


async def _create_player(db_session, slug: str, position: Position) -> PlayerMaster:
    player = PlayerMaster(display_name=slug.replace("-", " ").title(), slug=slug)
    db_session.add(player)
    await db_session.flush()

    status = PlayerStatus(player_id=player.id, position_id=position.id)
    db_session.add(status)
    await db_session.flush()
    return player


async def _create_snapshot(
    db_session,
    cohort: CohortType,
    source: MetricSource,
    season_id: int | None,
    position_scope_parent: str | None,
    version: int,
    is_current: bool = True,
) -> MetricSnapshot:
    snapshot = MetricSnapshot(
        run_key=f"{cohort.value}_v{version}",
        cohort=cohort,
        season_id=season_id,
        position_scope_parent=position_scope_parent,
        position_scope_fine=None,
        source=source,
        population_size=10,
        version=version,
        is_current=is_current,
    )
    db_session.add(snapshot)
    await db_session.flush()
    return snapshot


async def _attach_metric_value(
    db_session,
    snapshot: MetricSnapshot,
    definition: MetricDefinition,
    player_id: int,
    raw_value: float,
    percentile: float,
    rank: int | None = None,
):
    value = PlayerMetricValue(
        snapshot_id=snapshot.id,
        metric_definition_id=definition.id,
        player_id=player_id,
        raw_value=raw_value,
        percentile=percentile,
        rank=rank,
    )
    db_session.add(value)
    await db_session.flush()
    return value


@pytest.mark.asyncio
async def test_metrics_prefers_parent_scope_when_position_adjusted(
    app_client, db_session
):
    """Position-adjusted request returns parent-scoped snapshot when available."""
    position = await _create_position(db_session, code="PG", parents=["guard"])
    player = await _create_player(db_session, slug="metrics-player", position=position)

    season = Season(code="2024-25", start_year=2024, end_year=2025)
    db_session.add(season)
    await db_session.flush()

    metric_def = MetricDefinition(
        metric_key="wingspan_in",
        display_name="Wingspan",
        short_label="WS",
        source=MetricSource.combine_anthro,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.anthropometrics,
        unit="inches",
    )
    db_session.add(metric_def)
    await db_session.flush()

    guard_snapshot = await _create_snapshot(
        db_session,
        cohort=CohortType.current_draft,
        source=MetricSource.combine_anthro,
        season_id=season.id,
        position_scope_parent="guard",
        version=1,
    )
    baseline_snapshot = await _create_snapshot(
        db_session,
        cohort=CohortType.current_draft,
        source=MetricSource.combine_anthro,
        season_id=season.id,
        position_scope_parent=None,
        version=2,
    )
    await _attach_metric_value(
        db_session,
        guard_snapshot,
        metric_def,
        player.id,
        raw_value=86.5,
        percentile=90,
        rank=5,
    )
    await _attach_metric_value(
        db_session,
        baseline_snapshot,
        metric_def,
        player.id,
        raw_value=86.5,
        percentile=50,
        rank=20,
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{player.slug}/metrics",
        params={
            "cohort": CohortType.current_draft.value,
            "category": MetricCategory.anthropometrics.value,
            "position_adjusted": True,
            "season_id": season.id,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["snapshot_id"] == guard_snapshot.id
    assert payload["metrics"][0]["percentile"] == 90
    assert payload["metrics"][0]["rank"] == 5
    assert payload["population_size"] == guard_snapshot.population_size


@pytest.mark.asyncio
async def test_metrics_falls_back_to_baseline_when_parent_missing(
    app_client, db_session
):
    """Position-adjusted request falls back to baseline when parent snapshot absent."""
    position = await _create_position(db_session, code="SF", parents=["wing"])
    player = await _create_player(db_session, slug="fallback-player", position=position)

    season = Season(code="2024-25", start_year=2024, end_year=2025)
    db_session.add(season)
    await db_session.flush()

    metric_def = MetricDefinition(
        metric_key="standing_reach_in",
        display_name="Standing Reach",
        short_label="SR",
        source=MetricSource.combine_anthro,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.anthropometrics,
        unit="inches",
    )
    db_session.add(metric_def)
    await db_session.flush()

    baseline_snapshot = await _create_snapshot(
        db_session,
        cohort=CohortType.current_draft,
        source=MetricSource.combine_anthro,
        season_id=season.id,
        position_scope_parent=None,
        version=1,
    )
    await _attach_metric_value(
        db_session,
        baseline_snapshot,
        metric_def,
        player.id,
        raw_value=110,
        percentile=77,
        rank=12,
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{player.slug}/metrics",
        params={
            "cohort": CohortType.current_draft.value,
            "category": MetricCategory.anthropometrics.value,
            "position_adjusted": True,
            "season_id": season.id,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["snapshot_id"] == baseline_snapshot.id
    assert payload["metrics"][0]["percentile"] == 77
    assert payload["metrics"][0]["rank"] == 12
    assert payload["population_size"] == baseline_snapshot.population_size


@pytest.mark.asyncio
async def test_metrics_returns_metric_population_size_for_nba_cohort(
    app_client, db_session
):
    """Metric rows include the baseline population size for the cohort."""
    position = await _create_position(db_session, code="G", parents=["guard"])
    prospect = await _create_player(
        db_session, slug="population-prospect", position=position
    )
    active_a = await _create_player(db_session, slug="active-a", position=position)
    active_b = await _create_player(db_session, slug="active-b", position=position)
    inactive = await _create_player(db_session, slug="inactive", position=position)

    season = Season(code="2024-25", start_year=2024, end_year=2025)
    db_session.add(season)
    await db_session.flush()

    # Mark active/inactive status for NBA cohorts.
    result = await db_session.execute(
        select(PlayerStatus).where(
            PlayerStatus.player_id.in_(  # type: ignore[arg-type]
                [active_a.id, active_b.id, inactive.id]
            )
        )
    )
    statuses = {status.player_id: status for status in result.scalars().all()}
    statuses[active_a.id].is_active_nba = True
    statuses[active_b.id].is_active_nba = True
    statuses[inactive.id].is_active_nba = False

    metric_def = MetricDefinition(
        metric_key="lane_agility_time_s",
        display_name="Lane Agility",
        short_label="Lane",
        source=MetricSource.combine_agility,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.combine_performance,
        unit="seconds",
    )
    db_session.add(metric_def)
    await db_session.flush()

    snapshot = await _create_snapshot(
        db_session,
        cohort=CohortType.current_nba,
        source=MetricSource.combine_agility,
        season_id=None,
        position_scope_parent=None,
        version=1,
    )
    snapshot.population_size = 999

    # Only active players have baseline data in the underlying table.
    db_session.add_all(
        [
            CombineAgility(
                player_id=active_a.id,
                season_id=season.id,
                lane_agility_time_s=11.1,
            ),
            CombineAgility(
                player_id=active_b.id,
                season_id=season.id,
                lane_agility_time_s=11.3,
            ),
            CombineAgility(
                player_id=inactive.id,
                season_id=season.id,
                lane_agility_time_s=12.0,
            ),
            CombineAgility(
                player_id=prospect.id,
                season_id=season.id,
                lane_agility_time_s=11.8,
            ),
        ]
    )
    await db_session.flush()

    await _attach_metric_value(
        db_session,
        snapshot,
        metric_def,
        prospect.id,
        raw_value=11.8,
        percentile=40,
        rank=2,
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{prospect.slug}/metrics",
        params={
            "cohort": CohortType.current_nba.value,
            "category": MetricCategory.combine_performance.value,
            "position_adjusted": False,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["population_size"] == 999
    assert payload["metrics"][0]["population_size"] == 2


@pytest.mark.asyncio
async def test_metrics_handles_all_time_nba_cohort(app_client, db_session):
    """all_time_nba cohort returns metrics when available."""
    position = await _create_position(db_session, code="C", parents=["big"])
    player = await _create_player(
        db_session, slug="all-time-nba-player", position=position
    )

    metric_def = MetricDefinition(
        metric_key="weight_lb",
        display_name="Weight",
        short_label="WT",
        source=MetricSource.combine_anthro,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.anthropometrics,
        unit="pounds",
    )
    db_session.add(metric_def)
    await db_session.flush()

    snapshot = await _create_snapshot(
        db_session,
        cohort=CohortType.all_time_nba,
        source=MetricSource.combine_anthro,
        season_id=None,
        position_scope_parent=None,
        version=1,
    )
    await _attach_metric_value(
        db_session,
        snapshot,
        metric_def,
        player.id,
        raw_value=250,
        percentile=65,
        rank=30,
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{player.slug}/metrics",
        params={
            "cohort": CohortType.all_time_nba.value,
            "category": MetricCategory.anthropometrics.value,
            "position_adjusted": False,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["snapshot_id"] == snapshot.id
    assert payload["metrics"][0]["percentile"] == 65
    assert payload["metrics"][0]["rank"] == 30
    assert payload["population_size"] == snapshot.population_size


@pytest.mark.asyncio
async def test_metrics_uses_source_position_for_shooting_position_adjusted(
    app_client, db_session
):
    """Position-adjusted shooting uses the combine table position scope."""
    wing_position = await _create_position(db_session, code="SF", parents=["wing"])
    forward_position = await _create_position(
        db_session, code="PF", parents=["forward"]
    )
    player = await _create_player(
        db_session, slug="shooting-position-mismatch", position=wing_position
    )

    season = Season(code="2025-26", start_year=2025, end_year=2026)
    db_session.add(season)
    await db_session.flush()

    # Combine row says the player is a forward, even if PlayerStatus says wing.
    db_session.add(
        CombineShooting(
            player_id=player.id,
            season_id=season.id,  # type: ignore[arg-type]
            position_id=forward_position.id,
            raw_position="PF",
            spot_up_fgm=10,
            spot_up_fga=20,
        )
    )

    metric_def = MetricDefinition(
        metric_key="spot_up_fg_pct",
        display_name="Spot-Up FG%",
        short_label="SU",
        source=MetricSource.combine_shooting,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.shooting,
        unit="percent",
    )
    db_session.add(metric_def)
    await db_session.flush()

    wing_snapshot = MetricSnapshot(
        run_key="shooting_mismatch|pos=wing",
        cohort=CohortType.all_time_nba,
        season_id=None,
        position_scope_parent="wing",
        position_scope_fine=None,
        source=MetricSource.combine_shooting,
        population_size=10,
        version=1,
        is_current=True,
    )
    forward_snapshot = MetricSnapshot(
        run_key="shooting_mismatch|pos=forward",
        cohort=CohortType.all_time_nba,
        season_id=None,
        position_scope_parent="forward",
        position_scope_fine=None,
        source=MetricSource.combine_shooting,
        population_size=10,
        version=1,
        is_current=True,
    )
    db_session.add_all([wing_snapshot, forward_snapshot])
    await db_session.flush()

    await _attach_metric_value(
        db_session,
        forward_snapshot,
        metric_def,
        player.id,
        raw_value=50.0,
        percentile=88,
        rank=2,
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{player.slug}/metrics",
        params={
            "cohort": CohortType.all_time_nba.value,
            "category": MetricCategory.shooting.value,
            "position_adjusted": True,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["snapshot_id"] == forward_snapshot.id
    assert payload["metrics"][0]["percentile"] == 88


@pytest.mark.asyncio
async def test_metrics_formats_values_for_units(app_client, db_session):
    """Values are formatted with friendly units (%, in/ft, lbs, sec)."""
    position = await _create_position(db_session, code="PF", parents=["forward"])
    player = await _create_player(db_session, slug="format-player", position=position)

    season = Season(code="2025-26", start_year=2025, end_year=2026)
    db_session.add(season)
    await db_session.flush()

    height_def = MetricDefinition(
        metric_key="height_w_shoes_in",
        display_name="Height (With Shoes)",
        short_label="HTWS",
        source=MetricSource.combine_anthro,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.anthropometrics,
        unit="inches",
    )
    bodyfat_def = MetricDefinition(
        metric_key="body_fat_pct",
        display_name="Body Fat",
        short_label="BF",
        source=MetricSource.combine_anthro,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.anthropometrics,
        unit="percent",
    )
    weight_def = MetricDefinition(
        metric_key="weight_lb",
        display_name="Weight",
        short_label="WT",
        source=MetricSource.combine_anthro,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.anthropometrics,
        unit="pounds",
    )
    agility_def = MetricDefinition(
        metric_key="lane_agility_time_s",
        display_name="Lane Agility",
        short_label="LA",
        source=MetricSource.combine_agility,
        statistic=MetricStatistic.percentile,
        category=MetricCategory.combine_performance,
        unit="seconds",
    )
    db_session.add_all([height_def, bodyfat_def, weight_def, agility_def])
    await db_session.flush()

    snapshot = MetricSnapshot(
        run_key="format_test",
        cohort=CohortType.current_draft,
        season_id=season.id,
        position_scope_parent=None,
        position_scope_fine=None,
        source=MetricSource.combine_anthro,
        population_size=10,
        version=1,
        is_current=True,
    )
    agility_snapshot = MetricSnapshot(
        run_key="format_test_agility",
        cohort=CohortType.current_draft,
        season_id=season.id,
        position_scope_parent=None,
        position_scope_fine=None,
        source=MetricSource.combine_agility,
        population_size=10,
        version=1,
        is_current=True,
    )
    db_session.add_all([snapshot, agility_snapshot])
    await db_session.flush()

    await _attach_metric_value(
        db_session,
        snapshot,
        height_def,
        player.id,
        raw_value=81.5,
        percentile=90,
        rank=3,
    )
    await _attach_metric_value(
        db_session,
        snapshot,
        bodyfat_def,
        player.id,
        raw_value=5.4,
        percentile=80,
        rank=8,
    )
    await _attach_metric_value(
        db_session,
        snapshot,
        weight_def,
        player.id,
        raw_value=216.8,
        percentile=70,
        rank=12,
    )
    await _attach_metric_value(
        db_session,
        agility_snapshot,
        agility_def,
        player.id,
        raw_value=11.0,
        percentile=60,
        rank=20,
    )
    await db_session.commit()

    # Anthropometrics cohort (height/body fat/weight)
    resp = await app_client.get(
        f"/api/players/{player.slug}/metrics",
        params={
            "cohort": CohortType.current_draft.value,
            "category": MetricCategory.anthropometrics.value,
            "position_adjusted": False,
            "season_id": season.id,
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    values = {
        item["metric"]: (item["value"], item["unit"]) for item in payload["metrics"]
    }
    assert values["Height (With Shoes)"] == ("6'9.5\"", "")
    assert values["Body Fat"] == ("5.4", "%")
    assert values["Weight"] == ("216.8", " lbs")
    ranks = {item["metric"]: item["rank"] for item in payload["metrics"]}
    assert ranks["Height (With Shoes)"] == 3
    assert payload["population_size"] == snapshot.population_size

    # Combine performance cohort (lane agility)
    resp2 = await app_client.get(
        f"/api/players/{player.slug}/metrics",
        params={
            "cohort": CohortType.current_draft.value,
            "category": MetricCategory.combine_performance.value,
            "position_adjusted": False,
            "season_id": season.id,
        },
    )
    assert resp2.status_code == 200
    values2 = {
        item["metric"]: (item["value"], item["unit"])
        for item in resp2.json()["metrics"]
    }
    assert values2["Lane Agility"] == ("11", " sec")
    assert resp2.json()["population_size"] == agility_snapshot.population_size


@pytest.mark.asyncio
async def test_metrics_returns_404_for_unknown_player(app_client):
    """Unknown slug returns 404."""
    resp = await app_client.get(
        "/api/players/does-not-exist/metrics",
        params={
            "cohort": CohortType.current_draft.value,
            "category": MetricCategory.anthropometrics.value,
        },
    )
    assert resp.status_code == 404
