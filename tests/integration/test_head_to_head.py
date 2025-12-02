"""Integration tests for head-to-head comparison API."""

import pytest

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    SimilarityDimension,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.metrics import MetricSnapshot, PlayerSimilarity
from app.schemas.players_master import PlayerMaster
from app.schemas.seasons import Season


async def _create_player(
    db_session, slug: str, name: str | None = None
) -> PlayerMaster:
    player = PlayerMaster(
        display_name=name or slug.replace("-", " ").title(), slug=slug
    )
    db_session.add(player)
    await db_session.flush()
    return player


async def _create_season(db_session, code: str, start_year: int) -> Season:
    season = Season(code=code, start_year=start_year, end_year=start_year + 1)
    db_session.add(season)
    await db_session.flush()
    return season


async def _create_snapshot(
    db_session,
    source: MetricSource,
    version: int,
    cohort: CohortType = CohortType.current_draft,
    is_current: bool = True,
) -> MetricSnapshot:
    snapshot = MetricSnapshot(
        run_key=f"{source.value}_v{version}",
        cohort=cohort,
        season_id=None,
        position_scope_parent=None,
        position_scope_fine=None,
        source=source,
        population_size=10,
        version=version,
        is_current=is_current,
    )
    db_session.add(snapshot)
    await db_session.flush()
    return snapshot


@pytest.mark.asyncio
async def test_head_to_head_returns_shared_metrics_only(app_client, db_session):
    """Endpoint returns only shared metrics using raw combine data."""
    player_a = await _create_player(db_session, "player-a", "Player A")
    player_b = await _create_player(db_session, "player-b", "Player B")
    season = await _create_season(db_session, "2024-25", 2024)

    db_session.add_all(
        [
            CombineAnthro(
                player_id=player_a.id,
                season_id=season.id,
                wingspan_in=84.0,
                standing_reach_in=108.0,
            ),
            CombineAnthro(
                player_id=player_b.id,
                season_id=season.id,
                wingspan_in=85.0,
            ),
        ]
    )
    await db_session.commit()

    resp = await app_client.get(
        "/api/players/head-to-head",
        params={
            "player_a": player_a.slug,
            "player_b": player_b.slug,
            "category": MetricCategory.anthropometrics.value,
        },
    )

    assert resp.status_code == 200
    payload = resp.json()

    assert payload["player_a"]["slug"] == player_a.slug
    assert payload["player_b"]["slug"] == player_b.slug
    assert payload["category"] == MetricCategory.anthropometrics.value

    # Only the shared metric should be returned
    assert len(payload["metrics"]) == 1
    metric = payload["metrics"][0]
    assert metric["metric"] == "Wingspan"
    assert metric["display_value_a"].startswith("7'")
    assert metric["display_value_b"].startswith("7'")
    # Unit is empty because format_metric_value converts to feet/inches display
    assert metric["unit"] == ""
    assert metric["raw_value_a"] == 84.0
    assert metric["raw_value_b"] == 85.0


@pytest.mark.asyncio
async def test_head_to_head_includes_similarity_and_direction(app_client, db_session):
    """Endpoint returns lower-is-better flags and similarity when available."""
    player_a = await _create_player(db_session, "player-c", "Player C")
    player_b = await _create_player(db_session, "player-d", "Player D")
    season = await _create_season(db_session, "2023-24", 2023)

    db_session.add_all(
        [
            CombineAgility(
                player_id=player_a.id,
                season_id=season.id,
                lane_agility_time_s=10.4,
                bench_press_reps=12,
            ),
            CombineAgility(
                player_id=player_b.id,
                season_id=season.id,
                lane_agility_time_s=10.9,
                bench_press_reps=8,
            ),
        ]
    )
    snapshot = await _create_snapshot(
        db_session, MetricSource.combine_agility, version=1, is_current=True
    )

    db_session.add(
        PlayerSimilarity(
            snapshot_id=snapshot.id,
            dimension=SimilarityDimension.combine,
            anchor_player_id=player_a.id,
            comparison_player_id=player_b.id,
            similarity_score=87.5,
            distance=None,
            overlap_pct=0.6,
            rank_within_anchor=1,
        )
    )

    await db_session.commit()

    resp = await app_client.get(
        "/api/players/head-to-head",
        params={
            "player_a": player_a.slug,
            "player_b": player_b.slug,
            "category": MetricCategory.combine_performance.value,
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["similarity"]["score"] == pytest.approx(87.5)

    metrics = payload["metrics"]
    lane_metric = next(
        (m for m in metrics if m["metric_key"] == "lane_agility_time_s"), None
    )
    assert lane_metric
    assert lane_metric["lower_is_better"] is True
    assert lane_metric["raw_value_a"] == 10.4
    assert lane_metric["raw_value_b"] == 10.9
    assert lane_metric["unit"].strip() == "sec"
