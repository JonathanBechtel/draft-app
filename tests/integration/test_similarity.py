"""Integration tests for player similarity API."""

import pytest

from app.models.fields import (
    CohortType,
    MetricSource,
    SimilarityDimension,
)
from app.schemas.metrics import MetricSnapshot, PlayerSimilarity
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position


async def _create_player(
    db_session, slug: str, name: str | None = None, draft_year: int | None = None
) -> PlayerMaster:
    player = PlayerMaster(
        display_name=name or slug.replace("-", " ").title(),
        slug=slug,
        draft_year=draft_year,
    )
    db_session.add(player)
    await db_session.flush()
    return player


async def _create_snapshot(
    db_session,
    source: MetricSource,
    version: int,
    cohort: CohortType = CohortType.global_scope,
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


async def _create_position(db_session, code: str, name: str) -> Position:
    position = Position(code=code, name=name)
    db_session.add(position)
    await db_session.flush()
    return position


async def _create_player_status(
    db_session,
    player_id: int,
    position_id: int | None = None,
    is_active_nba: bool = False,
) -> PlayerStatus:
    status = PlayerStatus(
        player_id=player_id,
        position_id=position_id,
        is_active_nba=is_active_nba,
    )
    db_session.add(status)
    await db_session.flush()
    return status


@pytest.mark.asyncio
async def test_similar_players_returns_expected_structure(app_client, db_session):
    """Endpoint returns similar players with correct structure."""
    anchor = await _create_player(db_session, "anchor-player", "Anchor Player")
    comp1 = await _create_player(db_session, "comp-one", "Comp One")
    comp2 = await _create_player(db_session, "comp-two", "Comp Two")

    snapshot = await _create_snapshot(
        db_session, MetricSource.combine_anthro, version=1
    )

    db_session.add_all(
        [
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=comp1.id,
                similarity_score=92.5,
                rank_within_anchor=1,
                shared_position=True,
            ),
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=comp2.id,
                similarity_score=85.0,
                rank_within_anchor=2,
                shared_position=False,
            ),
        ]
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro"},
    )

    assert resp.status_code == 200
    payload = resp.json()

    assert payload["anchor_slug"] == anchor.slug
    assert payload["dimension"] == "anthro"
    assert payload["snapshot_id"] == snapshot.id
    assert len(payload["players"]) == 2

    # First player should be highest ranked (rank 1)
    p1 = payload["players"][0]
    assert p1["slug"] == comp1.slug
    assert p1["display_name"] == "Comp One"
    assert p1["similarity_score"] == pytest.approx(92.5)
    assert p1["rank"] == 1
    assert p1["shared_position"] is True

    # Second player
    p2 = payload["players"][1]
    assert p2["slug"] == comp2.slug
    assert p2["similarity_score"] == pytest.approx(85.0)
    assert p2["rank"] == 2


@pytest.mark.asyncio
async def test_similar_players_same_position_filter(app_client, db_session):
    """Filter by same_position returns only players with shared_position=True."""
    anchor = await _create_player(db_session, "anchor-pos", "Anchor Pos")
    same_pos = await _create_player(db_session, "same-pos", "Same Pos")
    diff_pos = await _create_player(db_session, "diff-pos", "Diff Pos")

    snapshot = await _create_snapshot(
        db_session, MetricSource.combine_anthro, version=1
    )

    db_session.add_all(
        [
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=same_pos.id,
                similarity_score=90.0,
                rank_within_anchor=1,
                shared_position=True,
            ),
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=diff_pos.id,
                similarity_score=88.0,
                rank_within_anchor=2,
                shared_position=False,
            ),
        ]
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro", "same_position": "true"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["players"]) == 1
    assert payload["players"][0]["slug"] == same_pos.slug
    assert payload["players"][0]["shared_position"] is True


@pytest.mark.asyncio
async def test_similar_players_same_draft_year_filter(app_client, db_session):
    """Filter by same_draft_year returns only players with matching draft year."""
    anchor = await _create_player(
        db_session, "anchor-2025", "Anchor 2025", draft_year=2025
    )
    same_year = await _create_player(
        db_session, "same-year", "Same Year", draft_year=2025
    )
    diff_year = await _create_player(
        db_session, "diff-year", "Diff Year", draft_year=2024
    )

    snapshot = await _create_snapshot(
        db_session, MetricSource.combine_anthro, version=1
    )

    db_session.add_all(
        [
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=same_year.id,
                similarity_score=91.0,
                rank_within_anchor=1,
            ),
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=diff_year.id,
                similarity_score=89.0,
                rank_within_anchor=2,
            ),
        ]
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro", "same_draft_year": "true"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["players"]) == 1
    assert payload["players"][0]["slug"] == same_year.slug


@pytest.mark.asyncio
async def test_similar_players_nba_only_filter(app_client, db_session):
    """Filter by nba_only returns only players with is_active_nba=True."""
    anchor = await _create_player(db_session, "anchor-nba", "Anchor NBA")
    nba_player = await _create_player(db_session, "nba-active", "NBA Active")
    prospect = await _create_player(db_session, "prospect", "Prospect")

    # Create player status records
    await _create_player_status(db_session, nba_player.id, is_active_nba=True)
    await _create_player_status(db_session, prospect.id, is_active_nba=False)

    snapshot = await _create_snapshot(
        db_session, MetricSource.combine_anthro, version=1
    )

    db_session.add_all(
        [
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=nba_player.id,
                similarity_score=88.0,
                rank_within_anchor=1,
            ),
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=prospect.id,
                similarity_score=90.0,
                rank_within_anchor=2,
            ),
        ]
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro", "nba_only": "true"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["players"]) == 1
    assert payload["players"][0]["slug"] == nba_player.slug


@pytest.mark.asyncio
async def test_similar_players_not_found(app_client, db_session):
    """Returns 404 for non-existent player slug."""
    resp = await app_client.get(
        "/api/players/nonexistent-player/similar",
        params={"dimension": "anthro"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Player not found"


@pytest.mark.asyncio
async def test_similar_players_limit_parameter(app_client, db_session):
    """Limit parameter restricts number of results."""
    anchor = await _create_player(db_session, "anchor-limit", "Anchor Limit")
    comps = []
    for i in range(5):
        comps.append(await _create_player(db_session, f"comp-{i}", f"Comp {i}"))

    snapshot = await _create_snapshot(
        db_session, MetricSource.combine_anthro, version=1
    )

    for i, comp in enumerate(comps):
        db_session.add(
            PlayerSimilarity(
                snapshot_id=snapshot.id,
                dimension=SimilarityDimension.anthro,
                anchor_player_id=anchor.id,
                comparison_player_id=comp.id,
                similarity_score=95.0 - i,
                rank_within_anchor=i + 1,
            )
        )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro", "limit": "3"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["players"]) == 3


@pytest.mark.asyncio
async def test_similar_players_empty_results(app_client, db_session):
    """Returns empty players list when no similarity data exists."""
    anchor = await _create_player(db_session, "anchor-empty", "Anchor Empty")

    # Create snapshot but no similarity data
    await _create_snapshot(db_session, MetricSource.combine_anthro, version=1)
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["anchor_slug"] == anchor.slug
    assert payload["players"] == []


@pytest.mark.asyncio
async def test_similar_players_prefers_global_scope_snapshot(app_client, db_session):
    """Endpoint prefers global_scope cohort snapshots over others."""
    anchor = await _create_player(db_session, "anchor-global", "Anchor Global")
    comp_global = await _create_player(db_session, "comp-global", "Comp Global")
    comp_draft = await _create_player(db_session, "comp-draft", "Comp Draft")

    # Create both global and current_draft snapshots
    global_snapshot = await _create_snapshot(
        db_session,
        MetricSource.combine_anthro,
        version=1,
        cohort=CohortType.global_scope,
    )
    draft_snapshot = await _create_snapshot(
        db_session,
        MetricSource.combine_anthro,
        version=2,
        cohort=CohortType.current_draft,
    )

    # Global snapshot has comp_global
    db_session.add(
        PlayerSimilarity(
            snapshot_id=global_snapshot.id,
            dimension=SimilarityDimension.anthro,
            anchor_player_id=anchor.id,
            comparison_player_id=comp_global.id,
            similarity_score=95.0,
            rank_within_anchor=1,
        )
    )
    # Draft snapshot has comp_draft
    db_session.add(
        PlayerSimilarity(
            snapshot_id=draft_snapshot.id,
            dimension=SimilarityDimension.anthro,
            anchor_player_id=anchor.id,
            comparison_player_id=comp_draft.id,
            similarity_score=90.0,
            rank_within_anchor=1,
        )
    )
    await db_session.commit()

    resp = await app_client.get(
        f"/api/players/{anchor.slug}/similar",
        params={"dimension": "anthro"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    # Should use global snapshot
    assert payload["snapshot_id"] == global_snapshot.id
    assert len(payload["players"]) == 1
    assert payload["players"][0]["slug"] == comp_global.slug
