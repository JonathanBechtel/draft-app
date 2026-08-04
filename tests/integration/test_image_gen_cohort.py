"""Integration tests for SL-cohort image-generation targeting (T8).

Verifies that `scripts/generate_player_images.py` can restrict its player
selection to the Summer League rostered cohort (T0 selector) and, combined
with the existing `--missing-only` filter, builds a Gemini batch job that
covers only cohort players missing a stylized image. The Gemini client is
stubbed throughout so no real API call is ever made.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType
from app.schemas.image_snapshots import (
    IMAGE_PIPELINE_CALCULATION_VERSION,
    PlayerImageAsset,
    PlayerImageSnapshot,
)
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.image_generation import image_generation_service
from scripts.generate_player_images import check_existing_image, get_players
from tests.integration.conftest import make_player


async def _seed_competition(
    db: AsyncSession, *, year: int, league_id: str, venue_slug: str
) -> tuple[SummerLeagueEdition, SummerLeagueTeamEntry]:
    """Seed one Summer League competition with a single team entry."""
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="cohort-team-1",
        raw_team_name="Test Team",
        raw_team_abbreviation="TST",
        team_slug="cohort-tst-1",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return comp, team


async def _participate(
    db: AsyncSession,
    *,
    comp_id: int,
    team_entry_id: int,
    name: str,
    person_id: str,
    canonical_player_id: int,
) -> SummerLeagueParticipation:
    """Seed one resolved participation row tying a source player to a canonical player."""
    sp = SummerLeagueSourceRecord(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        normalized_name=name.lower(),
        canonical_player_id=canonical_player_id,
    )
    db.add(sp)
    await db.flush()
    assert sp.id is not None
    part = SummerLeagueParticipation(
        competition_id=comp_id,
        team_entry_id=team_entry_id,
        source_player_id=sp.id,
        player_id=canonical_player_id,
        stint_no=1,
    )
    db.add(part)
    await db.flush()
    return part


class _DummyCreatedBatch:
    """Stand-in for the Gemini SDK's created-batch response object."""

    def __init__(self, name: str) -> None:
        self.name = name


class _DummyBatches:
    """Stand-in for `genai.Client().batches` that never calls a real API."""

    def __init__(self, created_name: str) -> None:
        self._created = _DummyCreatedBatch(created_name)

    def create(self, *, model: str, src, config):  # noqa: ANN001, ARG002
        return self._created


class _DummyClient:
    """Stand-in for `genai.Client` — no real Gemini API call is ever made."""

    def __init__(self, created_name: str = "batches/fake-sl-cohort") -> None:
        self.batches = _DummyBatches(created_name)


@pytest.mark.asyncio
async def test_summer_league_batch_targets_only_missing_cohort_players(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch build should cover only cohort players missing a stylized image.

    Seeds a Summer League cohort (via `summer_league_cohort`, T0) with one
    resolved player who already has a successful "default"-style image asset
    and one who doesn't, plus a third player who is not in the cohort at all.
    Asserts `get_players(summer_league=True)` returns only the two cohort
    players, that the `--missing-only` filter (`check_existing_image`) narrows
    that down to the player without an image, and that
    `image_generation_service.submit_batch_job` — with the Gemini client
    stubbed so no real API call occurs — builds a batch covering only that
    player.
    """
    player_has_image = make_player("Has", "Image", school="Duke")
    player_missing = make_player("Missing", "Image", school="Kansas")
    player_not_cohort = make_player("Not", "Cohort", school="UCLA")
    db_session.add_all([player_has_image, player_missing, player_not_cohort])
    await db_session.flush()
    assert player_has_image.id is not None
    assert player_missing.id is not None
    assert player_not_cohort.id is not None

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="15", venue_slug="las_vegas"
    )
    assert comp.id is not None
    assert team.id is not None

    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Has Image",
        person_id="sl-img-1",
        canonical_player_id=player_has_image.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Missing Image",
        person_id="sl-img-2",
        canonical_player_id=player_missing.id,
    )
    await db_session.commit()

    # Give player_has_image an existing successful "default"-style image asset.
    existing_snapshot = PlayerImageSnapshot(
        run_key="existing_run",
        version=1,
        is_current=True,
        style="default",
        cohort=CohortType.global_scope,
        image_size="1K",
        system_prompt="test system prompt",
        registry_version="test",
        calculation_version=IMAGE_PIPELINE_CALCULATION_VERSION,
    )
    db_session.add(existing_snapshot)
    await db_session.commit()
    assert existing_snapshot.id is not None

    db_session.add(
        PlayerImageAsset(
            player_id=player_has_image.id,
            snapshot_id=existing_snapshot.id,
            s3_key="players/has-image-default.png",
            public_url="https://example.test/has-image-default.png",
            user_prompt="test prompt",
            generated_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await db_session.commit()

    # 1. Cohort selection restricts the candidate set to cohort players only.
    cohort_players = await get_players(
        db_session, summer_league=True, summer_league_year=2025
    )
    cohort_ids = {p.id for p in cohort_players}
    assert cohort_ids == {player_has_image.id, player_missing.id}
    assert player_not_cohort.id not in cohort_ids

    # 2. --missing-only filter narrows the cohort to players without a stylized image.
    missing_only_players = []
    for player in cohort_players:
        assert player.id is not None
        has_image = await check_existing_image(db_session, player.id, "default")
        if not has_image:
            missing_only_players.append(player)
    assert [p.id for p in missing_only_players] == [player_missing.id]

    # 3. The batch is built (not spent): stub the Gemini client entirely.
    monkeypatch.setattr(image_generation_service, "_client", _DummyClient())

    build_snapshot = PlayerImageSnapshot(
        run_key="sl_cohort_test_run",
        version=1,
        is_current=False,
        style="default",
        cohort=CohortType.global_scope,
        image_size="1K",
        system_prompt="test system prompt",
        registry_version="test",
        calculation_version=IMAGE_PIPELINE_CALCULATION_VERSION,
    )
    db_session.add(build_snapshot)
    await db_session.commit()
    assert build_snapshot.id is not None

    job_record = await image_generation_service.submit_batch_job(
        db=db_session,
        players=missing_only_players,
        snapshot=build_snapshot,
        style="default",
        image_size="1K",
        fetch_likeness=False,
    )
    await db_session.commit()

    assert job_record.gemini_job_name == "batches/fake-sl-cohort"
    built_player_ids = {
        item["player_id"] for item in json.loads(job_record.player_ids_json)
    }
    assert built_player_ids == {player_missing.id}
    assert player_has_image.id not in built_player_ids


@pytest.mark.asyncio
async def test_summer_league_cohort_selection_empty_scope_returns_no_players(
    db_session: AsyncSession,
) -> None:
    """A Summer League scope with no participations yields an empty player list."""
    players = await get_players(db_session, summer_league=True, summer_league_year=1999)

    assert players == []
