"""Integration tests for Summer League player resolution and log backfill."""

from __future__ import annotations

from datetime import date, datetime
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.player_resolution import (
    NBA_STATS_SYSTEM,
    STUB_BIO_SOURCE,
    apply_source_player_resolution_plan,
    build_resolution_report,
    prepare_source_player_resolution,
    prepare_summer_league_player_resolutions,
    resolve_source_player,
    resolve_summer_league_players,
)


@dataclass(frozen=True, slots=True)
class FakeCandidate:
    """Candidate object matching the fields used by the resolution service."""

    player_id: int
    display_name: str | None
    school: str | None
    score: float


async def _seed_game_context(
    db_session: AsyncSession,
    *,
    year: int = 2024,
    league_id: str = "15",
) -> tuple[SummerLeagueCompetition, SummerLeagueTeamEntry, SummerLeagueGame]:
    competition = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas Summer League",
        starts_on=date(year, 7, 12),
        data_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"1610612{year % 1000:03d}",
        raw_team_name="Test Team",
        raw_team_abbreviation="TST",
        team_slug=f"test-team-{year}-{league_id}",
    )
    db_session.add(team)
    await db_session.flush()
    assert team.id is not None

    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"15{year}00001{league_id}",
        game_date=date(year, 7, 12),
        home_team_entry_id=team.id,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(game)
    await db_session.flush()
    assert game.id is not None
    return competition, team, game


async def _source_with_log(
    db_session: AsyncSession,
    *,
    raw_name: str,
    person_id: str,
    competition: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    game: SummerLeagueGame,
    canonical_player_id: int | None = None,
    status: SummerLeagueResolutionStatus = SummerLeagueResolutionStatus.UNRESOLVED,
) -> SummerLeagueSourcePlayer:
    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=person_id,
        raw_player_name=raw_name,
        normalized_name=_normalized_name_key(raw_name),
        first_seen_year=competition.year,
        last_seen_year=competition.year,
        canonical_player_id=canonical_player_id,
        resolution_status=status,
    )
    db_session.add(source_player)
    await db_session.flush()
    assert source_player.id is not None

    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,  # type: ignore[arg-type]
            game_id=game.id,  # type: ignore[arg-type]
            team_entry_id=team.id,  # type: ignore[arg-type]
            source_player_id=source_player.id,
            player_id=None,
            nba_stats_person_id=person_id,
            raw_player_name=raw_name,
            minutes_seconds=1200,
            pts=12,
            source_endpoint="boxscoretraditionalv2",
        )
    )
    await db_session.flush()
    return source_player


async def _log_player_id(
    db_session: AsyncSession,
    *,
    person_id: str,
) -> int | None:
    result = await db_session.execute(
        select(SummerLeaguePlayerGameLog.player_id).where(
            SummerLeaguePlayerGameLog.nba_stats_person_id == person_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one()


async def _external_id_count(
    db_session: AsyncSession,
    *,
    person_id: str,
) -> int:
    count = await db_session.scalar(
        select(func.count())
        .select_from(PlayerExternalId)
        .where(
            PlayerExternalId.system == NBA_STATS_SYSTEM,  # type: ignore[arg-type]
            PlayerExternalId.external_id == person_id,  # type: ignore[arg-type]
        )
    )
    return int(count or 0)


@pytest.mark.asyncio
async def test_external_id_resolution_wins_and_backfills_logs(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NBA Stats PERSON_ID links resolve before exact name matches."""
    competition, team, game = await _seed_game_context(db_session)
    external_player = PlayerMaster(display_name="External Linked Player")
    exact_name_player = PlayerMaster(display_name="Shared Source Name")
    db_session.add_all([external_player, exact_name_player])
    await db_session.flush()
    assert external_player.id is not None
    db_session.add(
        PlayerExternalId(
            player_id=external_player.id,
            system=NBA_STATS_SYSTEM,
            external_id="1641001",
        )
    )
    source_player = await _source_with_log(
        db_session,
        raw_name="Shared Source Name",
        person_id="1641001",
        competition=competition,
        team=team,
        game=game,
    )

    async def fail_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        raise AssertionError("candidate search should not run")

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fail_search,
    )

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id == external_player.id
    assert result.status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert result.logs_backfilled == 1
    assert source_player.canonical_player_id == external_player.id
    assert await _log_player_id(db_session, person_id="1641001") == external_player.id
    assert await _external_id_count(db_session, person_id="1641001") == 1


@pytest.mark.asyncio
async def test_resolution_backfills_shot_events(
    db_session: AsyncSession,
) -> None:
    """Resolving a source player links its existing shot events.

    Shot events are normalized before resolution, so their ``player_id`` starts
    NULL; per-player shot charts stay empty until resolution backfills them
    (mirroring the game-log backfill).
    """
    competition, team, game = await _seed_game_context(db_session)
    player = PlayerMaster(display_name="Shot Chart Player")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    db_session.add(
        PlayerExternalId(
            player_id=player.id, system=NBA_STATS_SYSTEM, external_id="1642222"
        )
    )
    source_player = await _source_with_log(
        db_session,
        raw_name="Shot Chart Player",
        person_id="1642222",
        competition=competition,
        team=team,
        game=game,
    )
    # Two unlinked shot events (player_id NULL) for this source player.
    for i in range(2):
        db_session.add(
            SummerLeagueShotEvent(
                game_id=game.id,  # type: ignore[arg-type]
                competition_id=competition.id,  # type: ignore[arg-type]
                team_entry_id=team.id,  # type: ignore[arg-type]
                source_player_id=source_player.id,
                player_id=None,
                nba_stats_person_id="1642222",
                nba_stats_game_id=game.nba_stats_game_id,
                nba_stats_game_event_id=i + 1,
                loc_x=10 + i,
                loc_y=20 + i,
                made=(i == 0),
            )
        )
    await db_session.flush()

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id == player.id
    assert result.shots_backfilled == 2
    linked = await db_session.scalar(
        select(func.count())
        .select_from(SummerLeagueShotEvent)
        .where(SummerLeagueShotEvent.player_id == player.id)  # type: ignore[arg-type]
    )
    assert linked == 2


@pytest.mark.asyncio
async def test_existing_source_link_is_reused_and_gets_external_id(
    db_session: AsyncSession,
) -> None:
    """Existing canonical source links are treated as confirmed resolutions."""
    competition, team, game = await _seed_game_context(db_session)
    player = PlayerMaster(display_name="Manual Link")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    source_player = await _source_with_log(
        db_session,
        raw_name="Different Source Name",
        person_id="1641002",
        competition=competition,
        team=team,
        game=game,
        canonical_player_id=player.id,
    )
    manual_resolved_at = datetime(2024, 7, 1, 12, 0, 0)
    source_player.resolution_status = SummerLeagueResolutionStatus.MANUAL
    source_player.resolved_at = manual_resolved_at
    source_player.resolved_by = "admin@example.test"

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id == player.id
    assert result.method == "EXISTING_SOURCE"
    assert source_player.resolution_status == SummerLeagueResolutionStatus.MANUAL
    assert source_player.resolved_at == manual_resolved_at
    assert source_player.resolved_by == "admin@example.test"
    assert await _log_player_id(db_session, person_id="1641002") == player.id
    assert await _external_id_count(db_session, person_id="1641002") == 1


@pytest.mark.asyncio
async def test_unique_exact_normalized_display_name_resolves(
    db_session: AsyncSession,
) -> None:
    """Exact display-name resolution folds diacritics and suffixes."""
    competition, team, game = await _seed_game_context(db_session)
    player = PlayerMaster(display_name="José García Jr.")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    source_player = await _source_with_log(
        db_session,
        raw_name="Jose Garcia",
        person_id="1641003",
        competition=competition,
        team=team,
        game=game,
    )

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id == player.id
    assert result.status == SummerLeagueResolutionStatus.EXACT
    assert await _log_player_id(db_session, person_id="1641003") == player.id
    assert await _external_id_count(db_session, person_id="1641003") == 1


@pytest.mark.asyncio
async def test_unique_alias_match_resolves(
    db_session: AsyncSession,
) -> None:
    """Alias resolution uses unique normalized player_aliases.full_name matches."""
    competition, team, game = await _seed_game_context(db_session)
    player = PlayerMaster(display_name="Canonical Prospect")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    db_session.add(PlayerAlias(player_id=player.id, full_name="Source Alias Jr."))
    source_player = await _source_with_log(
        db_session,
        raw_name="Source Alias",
        person_id="1641004",
        competition=competition,
        team=team,
        game=game,
    )

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id == player.id
    assert result.status == SummerLeagueResolutionStatus.ALIAS
    assert await _log_player_id(db_session, person_id="1641004") == player.id
    assert await _external_id_count(db_session, person_id="1641004") == 1


@pytest.mark.asyncio
async def test_ambiguous_exact_match_collects_candidates_without_resolution(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous name matches stay unresolved and store review candidates."""
    competition, team, game = await _seed_game_context(db_session)
    player_one = PlayerMaster(
        slug="duplicate-prospect-one",
        display_name="Duplicate Prospect",
    )
    player_two = PlayerMaster(
        slug="duplicate-prospect-two",
        display_name="Duplicate Prospect",
    )
    db_session.add_all([player_one, player_two])
    await db_session.flush()
    assert player_one.id is not None
    assert player_two.id is not None
    source_player = await _source_with_log(
        db_session,
        raw_name="Duplicate Prospect",
        person_id="1641005",
        competition=competition,
        team=team,
        game=game,
    )

    async def fake_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        return [
            FakeCandidate(
                player_id=player_one.id,  # type: ignore[arg-type]
                display_name=player_one.display_name,
                school=None,
                score=0.72,
            ),
            FakeCandidate(
                player_id=player_two.id,  # type: ignore[arg-type]
                display_name=player_two.display_name,
                school=None,
                score=0.71,
            ),
        ]

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_search,
    )

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id is None
    assert result.status == SummerLeagueResolutionStatus.VECTOR_CANDIDATE
    assert len(result.candidates) == 2
    assert source_player.canonical_player_id is None
    assert source_player.resolution_candidates is not None
    assert await _log_player_id(db_session, person_id="1641005") is None
    assert await _external_id_count(db_session, person_id="1641005") == 0


@pytest.mark.asyncio
async def test_unresolved_without_candidates_does_not_create_stub_by_default(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-match source players remain unresolved unless stub mode is enabled."""
    competition, team, game = await _seed_game_context(db_session)
    source_player = await _source_with_log(
        db_session,
        raw_name="Unmatched Prospect",
        person_id="1641006",
        competition=competition,
        team=team,
        game=game,
    )

    async def fake_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        return []

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_search,
    )

    result = await resolve_source_player(db_session, source_player)

    assert result.player_id is None
    assert result.status == SummerLeagueResolutionStatus.UNRESOLVED
    assert source_player.canonical_player_id is None
    assert await _log_player_id(db_session, person_id="1641006") is None
    assert await _external_id_count(db_session, person_id="1641006") == 0


@pytest.mark.asyncio
async def test_stub_creation_requires_stub_mode_and_no_serious_candidate(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub mode creates a canonical stub only when candidates are weak."""
    competition, team, game = await _seed_game_context(db_session)
    weak_candidate = PlayerMaster(display_name="Weak Existing Candidate")
    db_session.add(weak_candidate)
    await db_session.flush()
    assert weak_candidate.id is not None
    source_player = await _source_with_log(
        db_session,
        raw_name="New Stub Prospect",
        person_id="1641007",
        competition=competition,
        team=team,
        game=game,
    )

    async def fake_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        return [
            FakeCandidate(
                player_id=weak_candidate.id,  # type: ignore[arg-type]
                display_name=weak_candidate.display_name,
                school=None,
                score=0.12,
            )
        ]

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_search,
    )

    result = await resolve_source_player(
        db_session,
        source_player,
        create_stub=True,
    )

    assert result.stub_created is True
    assert result.status == SummerLeagueResolutionStatus.STUB
    assert result.player_id is not None
    stub = await db_session.get(PlayerMaster, result.player_id)
    assert stub is not None
    assert stub.display_name == "New Stub Prospect"
    assert stub.is_stub is True
    assert stub.bio_source == STUB_BIO_SOURCE
    assert await _log_player_id(db_session, person_id="1641007") == result.player_id
    assert await _external_id_count(db_session, person_id="1641007") == 1


@pytest.mark.asyncio
async def test_batch_resolution_filters_by_year_and_league(
    db_session: AsyncSession,
) -> None:
    """Batch resolution scopes source players through existing game logs."""
    target_competition, target_team, target_game = await _seed_game_context(
        db_session,
        year=2024,
        league_id="15",
    )
    other_competition, other_team, other_game = await _seed_game_context(
        db_session,
        year=2025,
        league_id="13",
    )
    target_player = PlayerMaster(display_name="Target Batch Player")
    other_player = PlayerMaster(display_name="Other Batch Player")
    db_session.add_all([target_player, other_player])
    await db_session.flush()
    await _source_with_log(
        db_session,
        raw_name="Target Batch Player",
        person_id="1641008",
        competition=target_competition,
        team=target_team,
        game=target_game,
    )
    await _source_with_log(
        db_session,
        raw_name="Other Batch Player",
        person_id="1641009",
        competition=other_competition,
        team=other_team,
        game=other_game,
    )

    report = await resolve_summer_league_players(
        db_session,
        year=2024,
        league_id="15",
    )

    assert report.total_source_players == 1
    assert report.exact_resolutions == 1
    assert await _log_player_id(db_session, person_id="1641008") == target_player.id
    assert await _log_player_id(db_session, person_id="1641009") is None


# ---------------------------------------------------------------------------
# Prepare/apply split (ticket #632): `resolve_source_player` and
# `resolve_summer_league_players` are now thin compositions of a read-only,
# provider-calling preparation step and a write-only apply step, so a caller
# (the ingest cron) can run the preparation with no writer-lock transaction
# held and batch the writes separately. These tests prove the split is
# behavior-preserving: preparation alone writes nothing, and prepare+apply
# together reach the exact same resolved state as the one-shot functions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_source_player_resolution_performs_no_writes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation alone (including the candidate-search call) writes nothing."""
    competition, team, game = await _seed_game_context(db_session)
    source_player = await _source_with_log(
        db_session,
        raw_name="Prepare Only Prospect",
        person_id="1641010",
        competition=competition,
        team=team,
        game=game,
    )

    async def fake_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        return []

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_search,
    )

    plan = await prepare_source_player_resolution(db_session, source_player)

    assert plan.kind == "UNRESOLVED"
    assert source_player.canonical_player_id is None
    assert source_player.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED
    assert await _log_player_id(db_session, person_id="1641010") is None
    assert await _external_id_count(db_session, person_id="1641010") == 0


@pytest.mark.asyncio
async def test_prepare_then_apply_matches_one_shot_resolve(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Split prepare+apply reaches the same resolved state as one-shot resolve.

    Runs the VECTOR_CANDIDATE-then-stub path (the branch that calls candidate
    search) through the split functions and cross-checks the result against
    what `resolve_source_player` (prepare+apply composed) would already do for
    an equivalent source player -- the two must agree bit-for-bit.
    """
    competition, team, game = await _seed_game_context(db_session)
    weak_candidate = PlayerMaster(display_name="Weak Split Candidate")
    db_session.add(weak_candidate)
    await db_session.flush()
    assert weak_candidate.id is not None

    # Distinct raw names -- otherwise the first branch's stub creation would
    # itself become an exact-name match for the second branch's lookup,
    # masking the candidate-search path this test means to exercise.
    split_player = await _source_with_log(
        db_session,
        raw_name="Split Stub Prospect One",
        person_id="1641011",
        competition=competition,
        team=team,
        game=game,
    )
    one_shot_player = await _source_with_log(
        db_session,
        raw_name="Split Stub Prospect Two",
        person_id="1641012",
        competition=competition,
        team=team,
        game=game,
    )

    async def fake_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        return [
            FakeCandidate(
                player_id=weak_candidate.id,  # type: ignore[arg-type]
                display_name=weak_candidate.display_name,
                school=None,
                score=0.12,
            )
        ]

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_search,
    )

    plan = await prepare_source_player_resolution(db_session, split_player)
    # Preparation performed no writes yet.
    assert split_player.canonical_player_id is None
    split_result = await apply_source_player_resolution_plan(
        db_session, split_player, plan, create_stub=True
    )

    one_shot_result = await resolve_source_player(
        db_session, one_shot_player, create_stub=True
    )

    assert split_result.status == one_shot_result.status == SummerLeagueResolutionStatus.STUB
    assert split_result.stub_created is True
    assert split_result.player_id != one_shot_result.player_id  # distinct new stubs
    assert (
        await _log_player_id(db_session, person_id="1641011") == split_result.player_id
    )
    assert (
        await _log_player_id(db_session, person_id="1641012")
        == one_shot_result.player_id
    )
    assert await _external_id_count(db_session, person_id="1641011") == 1
    assert await _external_id_count(db_session, person_id="1641012") == 1


@pytest.mark.asyncio
async def test_prepare_batch_then_apply_matches_resolve_summer_league_players(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch prepare-then-apply reaches the same report as the one-shot batch call.

    Mirrors `test_batch_resolution_filters_by_year_and_league` but drives the
    ingest cron's actual call shape: `prepare_summer_league_player_resolutions`
    (no writes) followed by `apply_source_player_resolution_plan` per pair.
    """
    competition, team, game = await _seed_game_context(db_session, year=2024, league_id="15")
    exact_player = PlayerMaster(display_name="Prepare Batch Player")
    db_session.add(exact_player)
    await db_session.flush()
    await _source_with_log(
        db_session,
        raw_name="Prepare Batch Player",
        person_id="1641013",
        competition=competition,
        team=team,
        game=game,
    )

    pairs = await prepare_summer_league_player_resolutions(
        db_session, year=2024, league_id="15"
    )
    assert len(pairs) == 1
    source_player, plan = pairs[0]
    assert plan.kind == "EXACT"
    # Preparation performed no writes yet.
    assert source_player.canonical_player_id is None

    results = [
        await apply_source_player_resolution_plan(db_session, sp, p, create_stub=True)
        for sp, p in pairs
    ]
    report = build_resolution_report(year=2024, league_id="15", results=results)

    assert report.total_source_players == 1
    assert report.exact_resolutions == 1
    assert await _log_player_id(db_session, person_id="1641013") == exact_player.id
