"""Integration tests for player_merge_service.

MANDATORY gate tests for ticket #304.  These tests exercise the full FK set
against a real Postgres test schema (using the shared conftest fixtures) and
assert:

1. Full-FK test: survivor owns all reassignable rows post-merge; discard row
   is gone; no orphaned FKs; alias created; survivor slug unchanged.
2. Conflict resolution: discard's conflicting rows are deleted, not reassigned.
3. Singleton resolution: survivor's row wins; discard's row deleted.
4. player_similarity: self-links deleted; both columns reassigned.
5. preview_merge fidelity: counts match what merge_players then performs.
6. Atomicity / rollback: mid-merge failure → full rollback, DB left clean.
7. keep_id == discard_id rejection.
8. find_duplicate_candidates excludes the player itself.
9. count_inbound_references returns correct counts.

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.boards import Board, BoardEntry, BoardKind, BoardStatus
from app.schemas.player_enrichment_jobs import PlayerEnrichmentJob
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting
from app.schemas.consensus import BigBoardConsensus, ConsensusSnapshot, ConsensusTrigger
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.player_content_mentions import (
    ContentType,
    MentionSource,
    PlayerContentMention,
)
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_lifecycle import PlayerLifecycle
from app.schemas.player_status import PlayerStatus
from app.schemas.draft_results import DraftResult
from app.schemas.player_affiliation import AffiliationType, PlayerAffiliation
from app.schemas.players_master import PlayerMaster
from app.schemas.podcast_episodes import PodcastEpisode, PodcastEpisodeTag
from app.schemas.podcast_shows import PodcastShow
from app.schemas.seasons import Season
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeaguePlayerResolutionReview,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueDeskGrade,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskStoryline,
    SummerLeagueDeskTriggerType,
)
from app.services.player_merge_service import (
    count_inbound_references,
    find_duplicate_candidates,
    merge_players,
    preview_merge,
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _player(
    db: AsyncSession, display_name: str, is_stub: bool = True
) -> PlayerMaster:
    """Insert and flush a minimal PlayerMaster."""
    p = PlayerMaster(display_name=display_name, is_stub=is_stub)
    db.add(p)
    await db.flush()
    return p


async def _season(db: AsyncSession, code: str = "2025-26") -> Season:
    """Insert and flush a Season row."""
    s = Season(code=code, start_year=2025, end_year=2026)
    db.add(s)
    await db.flush()
    return s


async def _news_source(db: AsyncSession) -> NewsSource:
    """Insert and flush a NewsSource row with a unique feed URL."""
    token = secrets.token_hex(4)
    src = NewsSource(
        name=f"Test Source {token}",
        display_name=f"Test Source {token}",
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/feed-{token}.rss",
    )
    db.add(src)
    await db.flush()
    return src


async def _podcast_show(db: AsyncSession) -> PodcastShow:
    """Insert and flush a PodcastShow row with a unique feed URL."""
    token = secrets.token_hex(4)
    show = PodcastShow(
        name=f"Test Podcast {token}",
        display_name=f"Test Podcast {token}",
        feed_url=f"https://example.com/podcast-{token}.rss",
    )
    db.add(show)
    await db.flush()
    return show


async def _board(db: AsyncSession, source: NewsSource) -> Board:
    """Insert and flush a Board row."""
    from datetime import datetime

    b = Board(
        news_source_id=source.id,  # type: ignore[arg-type]
        draft_year=2026,
        published_at=datetime.utcnow(),
        size=10,
        status=BoardStatus.PENDING,
        kind=BoardKind.BIG_BOARD,
    )
    db.add(b)
    await db.flush()
    return b


# ---------------------------------------------------------------------------
# Full-FK seeder
# ---------------------------------------------------------------------------


async def _seed_full_fk(
    db: AsyncSession,
    keep: PlayerMaster,
    discard: PlayerMaster,
) -> dict[str, object]:
    """Seed one row per FK table on the discard player.

    Returns a mapping of table label → entity id for later assertions.
    """
    assert discard.id is not None
    assert keep.id is not None

    meta: dict[str, object] = {}

    season = await _season(db)
    meta["season_id"] = season.id

    # --- player_aliases ---
    alias = PlayerAlias(player_id=discard.id, full_name="D. Alias Name")
    db.add(alias)
    await db.flush()
    meta["alias_id"] = alias.id

    # --- player_lifecycle ---
    lc = PlayerLifecycle(player_id=discard.id)
    db.add(lc)
    await db.flush()
    meta["lifecycle_id"] = lc.id

    # --- player_status ---
    st = PlayerStatus(player_id=discard.id)
    db.add(st)
    await db.flush()
    meta["status_id"] = st.id

    # --- player_bio_snapshots ---
    snap = PlayerBioSnapshot(player_id=discard.id, source="bbr")
    db.add(snap)
    await db.flush()
    meta["bio_snap_id"] = snap.id

    # --- player_external_ids ---
    ext = PlayerExternalId(
        player_id=discard.id, system="bbr", external_id="testplayer01"
    )
    db.add(ext)
    await db.flush()
    meta["ext_id"] = ext.id

    # --- player_college_stats ---
    cs = PlayerCollegeStats(player_id=discard.id, season="2024-25")
    db.add(cs)
    await db.flush()
    meta["college_stats_id"] = cs.id

    # --- combine_anthro ---
    anthro = CombineAnthro(player_id=discard.id, season_id=season.id)
    db.add(anthro)
    await db.flush()
    meta["anthro_id"] = anthro.id

    # --- combine_agility ---
    agility = CombineAgility(player_id=discard.id, season_id=season.id)
    db.add(agility)
    await db.flush()
    meta["agility_id"] = agility.id

    # --- combine_shooting_results ---
    shooting = CombineShooting(player_id=discard.id, season_id=season.id)
    db.add(shooting)
    await db.flush()
    meta["shooting_id"] = shooting.id

    # --- news_items ---
    news_source = await _news_source(db)
    meta["news_source_id"] = news_source.id
    from datetime import datetime

    news = NewsItem(
        source_id=news_source.id,  # type: ignore[arg-type]
        external_id="test-article-001",
        title="Test Article",
        url="https://example.com/article",
        published_at=datetime.utcnow(),
        player_id=discard.id,
        tag=NewsItemTag.SCOUTING_REPORT,
    )
    db.add(news)
    await db.flush()
    meta["news_id"] = news.id

    # --- podcast_episodes ---
    podcast_show = await _podcast_show(db)
    meta["podcast_show_id"] = podcast_show.id
    episode = PodcastEpisode(
        show_id=podcast_show.id,  # type: ignore[arg-type]
        external_id="ep-001",
        title="Test Episode",
        audio_url="https://example.com/ep001.mp3",
        published_at=datetime.utcnow(),
        player_id=discard.id,
        tag=PodcastEpisodeTag.DRAFT_ANALYSIS,
    )
    db.add(episode)
    await db.flush()
    meta["episode_id"] = episode.id

    # --- player_content_mentions ---
    mention = PlayerContentMention(
        player_id=discard.id,
        content_type=ContentType.NEWS,
        content_id=news.id,  # type: ignore[arg-type]
        source=MentionSource.AI,
    )
    db.add(mention)
    await db.flush()
    meta["mention_id"] = mention.id

    # --- board_entries (nullable player_id) ---
    board_src = await _news_source(db)
    b = await _board(db, board_src)
    meta["board_id"] = b.id
    entry = BoardEntry(
        board_id=b.id,  # type: ignore[arg-type]
        player_id=discard.id,
        position=1,
        raw_name=discard.display_name or "Test Discard",
    )
    db.add(entry)
    await db.flush()
    meta["entry_id"] = entry.id

    # --- big_board_consensus ---
    consensus_snap = ConsensusSnapshot(
        draft_year=2026,
        num_boards=1,
        board_ids=[b.id],
        trigger=ConsensusTrigger.MANUAL,
    )
    db.add(consensus_snap)
    await db.flush()
    meta["consensus_snap_id"] = consensus_snap.id

    bbc = BigBoardConsensus(
        snapshot_id=consensus_snap.id,  # type: ignore[arg-type]
        draft_year=2026,
        player_id=discard.id,
        consensus_rank=1,
        avg_rank=1.0,
        median_rank=1.0,
        high_rank=1,
        low_rank=1,
        std_dev=0.0,
        num_sources=1,
    )
    db.add(bbc)
    await db.flush()
    meta["bbc_id"] = bbc.id

    return meta


# ---------------------------------------------------------------------------
# Full FK integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_full_fk(db_session: AsyncSession) -> None:
    """merge_players reassigns all FK rows from discard to survivor.

    Asserts:
    - survivor owns all reassignable rows post-merge.
    - discard row is gone from players_master.
    - No orphaned FKs (all child rows point to survivor).
    - Alias is added on survivor.
    - Survivor slug is unchanged.
    """
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player", is_stub=False)
        discard = await _player(db_session, "Discard Player", is_stub=True)

    keep_slug_before = keep.slug

    async with db_session.begin_nested():
        await _seed_full_fk(db_session, keep, discard)

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    assert report.keep_id == keep.id
    assert report.discard_id == discard.id
    assert report.alias_added == "Discard Player"

    # Discard row must be gone
    gone = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == discard.id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    assert gone is None, "Discard player row must be deleted"

    # Survivor still exists
    survivor = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == keep.id)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert survivor.slug == keep_slug_before, "Survivor slug must be unchanged"

    # Alias added on survivor
    alias_rows = (
        (
            await db_session.execute(
                select(PlayerAlias).where(PlayerAlias.player_id == keep.id)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    alias_names = {a.full_name for a in alias_rows}
    assert "Discard Player" in alias_names, (
        "Alias for discard name must exist on survivor"
    )

    # No orphaned FKs — check a sample of child tables
    for table, col in [
        ("player_lifecycle", "player_id"),
        ("player_status", "player_id"),
        ("player_bio_snapshots", "player_id"),
        ("player_college_stats", "player_id"),
        ("player_external_ids", "player_id"),
        ("combine_anthro", "player_id"),
        ("combine_agility", "player_id"),
        ("combine_shooting_results", "player_id"),
        ("board_entries", "player_id"),
        ("big_board_consensus", "player_id"),
        ("player_content_mentions", "player_id"),
        ("news_items", "player_id"),
        ("podcast_episodes", "player_id"),
    ]:
        orphan_count = (
            await db_session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} = :discard_id"),
                {"discard_id": discard.id},
            )
        ).scalar()
        assert orphan_count == 0, (
            f"Orphaned FK in {table}.{col} for discard_id={discard.id}"
        )


# ---------------------------------------------------------------------------
# Singleton conflict test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_singleton_conflict(db_session: AsyncSession) -> None:
    """When survivor already has a lifecycle/status row, discard's is deleted."""
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")

    async with db_session.begin_nested():
        # Both players have a lifecycle row — discard's should be deleted.
        db_session.add(PlayerLifecycle(player_id=keep.id))
        db_session.add(PlayerLifecycle(player_id=discard.id))
        await db_session.flush()

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # Discard's lifecycle row should have been deleted
    lc_for_discard = (
        await db_session.execute(
            text("SELECT count(*) FROM player_lifecycle WHERE player_id = :pid"),
            {"pid": discard.id},
        )
    ).scalar()
    assert lc_for_discard == 0

    # Survivor still has exactly one lifecycle row
    lc_for_keep = (
        await db_session.execute(
            text("SELECT count(*) FROM player_lifecycle WHERE player_id = :pid"),
            {"pid": keep.id},
        )
    ).scalar()
    assert lc_for_keep == 1

    # Report reflects deleted_conflict
    if "player_lifecycle.player_id" in report.per_table:
        assert report.per_table["player_lifecycle.player_id"]["deleted_conflict"] >= 1


@pytest.mark.asyncio
async def test_merge_singleton_no_conflict(db_session: AsyncSession) -> None:
    """When survivor has no lifecycle row, discard's is reassigned."""
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")

    async with db_session.begin_nested():
        # Only discard has a lifecycle row.
        db_session.add(PlayerLifecycle(player_id=discard.id))
        await db_session.flush()

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # Discard's lifecycle row should now be owned by keep
    lc_for_keep = (
        await db_session.execute(
            text("SELECT count(*) FROM player_lifecycle WHERE player_id = :pid"),
            {"pid": keep.id},
        )
    ).scalar()
    assert lc_for_keep == 1

    assert (
        report.per_table.get("player_lifecycle.player_id", {}).get("reassigned", 0) >= 1
    )


# ---------------------------------------------------------------------------
# Unique conflict test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_unique_conflict_college_stats(db_session: AsyncSession) -> None:
    """Discard's college_stats row is deleted if survivor already has same season."""
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")

    async with db_session.begin_nested():
        # Both have stats for the same season — unique conflict.
        db_session.add(PlayerCollegeStats(player_id=keep.id, season="2024-25"))
        db_session.add(PlayerCollegeStats(player_id=discard.id, season="2024-25"))
        # Discard also has a different season — should be reassigned.
        db_session.add(PlayerCollegeStats(player_id=discard.id, season="2023-24"))
        await db_session.flush()

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # No orphans for discard
    orphans = (
        await db_session.execute(
            text("SELECT count(*) FROM player_college_stats WHERE player_id = :pid"),
            {"pid": discard.id},
        )
    ).scalar()
    assert orphans == 0

    # Survivor has two seasons
    kept_count = (
        await db_session.execute(
            text("SELECT count(*) FROM player_college_stats WHERE player_id = :pid"),
            {"pid": keep.id},
        )
    ).scalar()
    assert kept_count == 2

    tbl = report.per_table.get("player_college_stats.player_id", {})
    assert tbl.get("deleted_conflict", 0) == 1
    assert tbl.get("reassigned", 0) == 1


# ---------------------------------------------------------------------------
# player_similarity self-link + both columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_similarity_self_links_deleted(db_session: AsyncSession) -> None:
    """player_similarity rows that would become self-links are deleted."""
    from app.schemas.metrics import MetricSnapshot, PlayerSimilarity
    from app.models.fields import CohortType, MetricSource, SimilarityDimension

    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")
        third = await _player(db_session, "Third Player")

    async with db_session.begin_nested():
        snap = MetricSnapshot(
            run_key="test_run",
            cohort=CohortType.current_draft,
            source=MetricSource.combine_anthro,
            population_size=3,
            version=1,
        )
        db_session.add(snap)
        await db_session.flush()

        # Self-link candidate: discard → keep (would become keep → keep)
        db_session.add(
            PlayerSimilarity(
                snapshot_id=snap.id,
                dimension=SimilarityDimension.composite,
                anchor_player_id=discard.id,
                comparison_player_id=keep.id,
                similarity_score=90.0,
            )
        )
        # Self-link candidate: keep → discard (would become keep → keep)
        db_session.add(
            PlayerSimilarity(
                snapshot_id=snap.id,
                dimension=SimilarityDimension.composite,
                anchor_player_id=keep.id,
                comparison_player_id=discard.id,
                similarity_score=90.0,
            )
        )
        # Normal row on discard → third (should be reassigned to keep)
        db_session.add(
            PlayerSimilarity(
                snapshot_id=snap.id,
                dimension=SimilarityDimension.composite,
                anchor_player_id=discard.id,
                comparison_player_id=third.id,
                similarity_score=75.0,
            )
        )
        await db_session.flush()

    async with db_session.begin_nested():
        await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # No orphans for discard
    orphan_anchor = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM player_similarity WHERE anchor_player_id = :pid"
            ),
            {"pid": discard.id},
        )
    ).scalar()
    orphan_comp = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM player_similarity WHERE comparison_player_id = :pid"
            ),
            {"pid": discard.id},
        )
    ).scalar()
    assert orphan_anchor == 0
    assert orphan_comp == 0

    # The keep→third row should exist now (reassigned from discard→third)
    keep_to_third = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM player_similarity "
                "WHERE anchor_player_id = :keep AND comparison_player_id = :third"
            ),
            {"keep": keep.id, "third": third.id},
        )
    ).scalar()
    assert keep_to_third == 1


# ---------------------------------------------------------------------------
# preview_merge fidelity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_merge_matches_execute(db_session: AsyncSession) -> None:
    """preview_merge counts match what merge_players actually performs."""
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")

    async with db_session.begin_nested():
        # Seed: lifecycle (singleton), two college stats seasons (one conflict)
        db_session.add(PlayerLifecycle(player_id=keep.id))
        db_session.add(PlayerLifecycle(player_id=discard.id))
        db_session.add(PlayerCollegeStats(player_id=keep.id, season="2024-25"))
        db_session.add(PlayerCollegeStats(player_id=discard.id, season="2024-25"))
        db_session.add(PlayerCollegeStats(player_id=discard.id, season="2023-24"))
        await db_session.flush()

    # Preview (no writes)
    preview = await preview_merge(
        db_session,
        keep_id=keep.id,  # type: ignore[arg-type]
        discard_id=discard.id,  # type: ignore[arg-type]
    )

    # Execute the actual merge
    async with db_session.begin_nested():
        executed = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # Per-table counts from preview should match executed
    for key in preview.per_table:
        if key in executed.per_table:
            assert (
                preview.per_table[key]["reassigned"]
                == executed.per_table[key]["reassigned"]
            ), f"{key}: preview reassigned != executed reassigned"
            assert (
                preview.per_table[key]["deleted_conflict"]
                == executed.per_table[key]["deleted_conflict"]
            ), f"{key}: preview deleted_conflict != executed deleted_conflict"

    # Both should report the alias
    assert preview.alias_added == executed.alias_added


# ---------------------------------------------------------------------------
# Atomicity / rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_rollback_on_failure(db_session: AsyncSession) -> None:
    """A failure mid-merge rolls back all changes.

    We verify this by:
    1. Seeding keep + discard with lifecycle rows (one row each).
    2. Starting a savepoint, running merge_players, then raising an exception.
    3. Rolling back the savepoint.
    4. Asserting both players still exist and discard still has its lifecycle row.
    """
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")

    async with db_session.begin_nested():
        db_session.add(PlayerLifecycle(player_id=discard.id))
        db_session.add(PlayerAlias(player_id=discard.id, full_name="Alt Discard Name"))
        await db_session.flush()

    # Record pre-merge row counts
    pre_discard_lc = (
        await db_session.execute(
            text("SELECT count(*) FROM player_lifecycle WHERE player_id = :pid"),
            {"pid": discard.id},
        )
    ).scalar()
    assert pre_discard_lc == 1

    # Attempt merge inside a savepoint, then roll it back
    try:
        async with db_session.begin_nested():
            await merge_players(
                db_session,
                keep_id=keep.id,  # type: ignore[arg-type]
                discard_id=discard.id,  # type: ignore[arg-type]
            )
            # Simulate a mid-merge failure
            raise RuntimeError("Simulated failure")
    except RuntimeError:
        pass  # Expected — savepoint was rolled back

    # Discard player must still exist
    still_there = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == discard.id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    assert still_there is not None, "Discard player must still exist after rollback"

    # Discard's lifecycle row must still be on the discard (not on keep)
    lc_on_discard = (
        await db_session.execute(
            text("SELECT count(*) FROM player_lifecycle WHERE player_id = :pid"),
            {"pid": discard.id},
        )
    ).scalar()
    assert lc_on_discard == 1, "Discard's lifecycle row must be restored after rollback"

    # No lifecycle row on keep (it had none before)
    lc_on_keep = (
        await db_session.execute(
            text("SELECT count(*) FROM player_lifecycle WHERE player_id = :pid"),
            {"pid": keep.id},
        )
    ).scalar()
    assert lc_on_keep == 0, "Keep should have no lifecycle row after rollback"


# ---------------------------------------------------------------------------
# keep_id == discard_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_same_id_raises(db_session: AsyncSession) -> None:
    """merge_players raises ValueError when keep_id == discard_id."""
    async with db_session.begin_nested():
        player = await _player(db_session, "Some Player")

    with pytest.raises(ValueError, match="must be different"):
        await merge_players(
            db_session,
            keep_id=player.id,  # type: ignore[arg-type]
            discard_id=player.id,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_preview_same_id_raises(db_session: AsyncSession) -> None:
    """preview_merge raises ValueError when keep_id == discard_id."""
    async with db_session.begin_nested():
        player = await _player(db_session, "Some Player")

    with pytest.raises(ValueError, match="must be different"):
        await preview_merge(
            db_session,
            keep_id=player.id,  # type: ignore[arg-type]
            discard_id=player.id,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_merge_missing_discard_raises(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """merge_players raises ValueError when discard player does not exist.

    Uses a fresh session (via session_factory) to avoid connection-state
    contamination from prior tests that used savepoints.
    """
    async with session_factory() as db:
        await db.execute(text(f'SET search_path TO "{test_schema}"'))
        await db.commit()

        async with db.begin():
            keep = await _player(db, "Keep Player For Missing Discard Test")

        with pytest.raises(ValueError, match="not found"):
            await merge_players(
                db,
                keep_id=keep.id,  # type: ignore[arg-type]
                discard_id=999999,
            )


@pytest.mark.asyncio
async def test_merge_missing_keep_raises(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """merge_players raises ValueError when keep player does not exist.

    Uses a fresh session (via session_factory) to avoid connection-state
    contamination from prior tests that used savepoints.
    """
    async with session_factory() as db:
        await db.execute(text(f'SET search_path TO "{test_schema}"'))
        await db.commit()

        async with db.begin():
            discard = await _player(db, "Discard Player For Missing Keep Test")

        with pytest.raises(ValueError, match="not found"):
            await merge_players(
                db,
                keep_id=999999,
                discard_id=discard.id,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# count_inbound_references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_inbound_references_empty(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """count_inbound_references returns empty dict for a player with no child rows.

    Uses a fresh session to avoid stale connections from prior long-running tests.
    """
    async with session_factory() as db:
        await db.execute(text(f'SET search_path TO "{test_schema}"'))
        await db.commit()

        async with db.begin():
            player = await _player(db, "Lonely Player Ref Count")

        refs = await count_inbound_references(db, player.id)  # type: ignore[arg-type]
        assert refs == {}


@pytest.mark.asyncio
async def test_count_inbound_references_with_data(db_session: AsyncSession) -> None:
    """count_inbound_references counts rows in each child table."""
    async with db_session.begin_nested():
        player = await _player(db_session, "Player With Data")

    async with db_session.begin_nested():
        db_session.add(PlayerLifecycle(player_id=player.id))
        db_session.add(PlayerAlias(player_id=player.id, full_name="Alternate Name"))
        await db_session.flush()

    refs = await count_inbound_references(db_session, player.id)  # type: ignore[arg-type]
    assert refs.get("player_lifecycle.player_id", 0) == 1
    assert refs.get("player_aliases.player_id", 0) == 1


# ---------------------------------------------------------------------------
# find_duplicate_candidates excludes self
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_duplicate_candidates_excludes_self(
    db_session: AsyncSession,
) -> None:
    """find_duplicate_candidates never returns the player itself."""
    async with db_session.begin_nested():
        player = await _player(db_session, "John Smith")
        # Add a similar player to have something to match against.
        _other = await _player(db_session, "John Smith Jr.")

    candidates = await find_duplicate_candidates(db_session, player.id)  # type: ignore[arg-type]
    player_ids = {c.player_id for c in candidates}
    assert player.id not in player_ids, (
        "find_duplicate_candidates must not include the query player itself"
    )


@pytest.mark.asyncio
async def test_find_duplicate_candidates_missing_player_raises(
    db_session: AsyncSession,
) -> None:
    """find_duplicate_candidates raises ValueError for an absent player."""
    with pytest.raises(ValueError, match="not found"):
        await find_duplicate_candidates(db_session, 999999)


# ---------------------------------------------------------------------------
# Alias idempotency (ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_alias_idempotent(db_session: AsyncSession) -> None:
    """If discard's display_name already exists as an alias on survivor, no error."""
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player")
        discard = await _player(db_session, "Discard Player")

    async with db_session.begin_nested():
        # Pre-create the alias that merge_players would insert.
        db_session.add(
            PlayerAlias(
                player_id=keep.id, full_name="Discard Player", context="pre_existing"
            )
        )
        await db_session.flush()

    async with db_session.begin_nested():
        # Should not raise even though the alias already exists.
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    assert report.alias_added == "Discard Player"

    # Exactly one alias row with that name (no duplicate)
    count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM player_aliases "
                "WHERE player_id = :pid AND full_name = :name"
            ),
            {"pid": keep.id, "name": "Discard Player"},
        )
    ).scalar()
    assert count == 1


# ---------------------------------------------------------------------------
# Regression: player_enrichment_jobs FK (non-cascade) — added in PR #311
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_enrichment_job_reassigned(db_session: AsyncSession) -> None:
    """merge_players reassigns PlayerEnrichmentJob rows from discard to survivor.

    Before the fix, player_enrichment_jobs had no entry in _CHILD_TABLES, so
    the final DELETE players_master raised a ForeignKeyViolation because the
    job's non-cascade FK still pointed at the discard row.

    Asserts:
    - merge_players completes without error.
    - The job row is now owned by the survivor (player_id == keep.id).
    - No orphan job row remains for the discard player.
    """
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player Enrichment")
        discard = await _player(db_session, "Discard Player Enrichment")

    async with db_session.begin_nested():
        job = PlayerEnrichmentJob(
            player_id=discard.id,  # type: ignore[arg-type]
            state="queued",
            source="admin_single",
        )
        db_session.add(job)
        await db_session.flush()
        job_id = job.id

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    assert report.keep_id == keep.id
    assert report.discard_id == discard.id

    # No orphan for the discard player
    orphan_count = (
        await db_session.execute(
            text("SELECT count(*) FROM player_enrichment_jobs WHERE player_id = :pid"),
            {"pid": discard.id},
        )
    ).scalar()
    assert orphan_count == 0, (
        "No enrichment job should reference the deleted discard player"
    )

    # The job should now point to the survivor
    survivor_count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM player_enrichment_jobs "
                "WHERE player_id = :pid AND id = :job_id"
            ),
            {"pid": keep.id, "job_id": job_id},
        )
    ).scalar()
    assert survivor_count == 1, "Enrichment job must be reassigned to the survivor"


# ---------------------------------------------------------------------------
# Regression: board_entries same-board conflict — added in PR #311
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_board_entries_same_board_conflict(
    db_session: AsyncSession,
) -> None:
    """Discard's board entry is deleted when survivor is on the same board.

    Before the fix, board_entries had no conflict_columns spec, so reassigning
    the discard's entry would violate uq_board_entries_board_player when both
    players already sit on the same board.

    Asserts:
    - merge_players completes without error.
    - The survivor's entry remains on the shared board.
    - The discard's conflicting entry is deleted (no duplicate board/player pair).
    - An entry on a different board (only the discard had it) is reassigned to
      the survivor.
    """
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Player Board")
        discard = await _player(db_session, "Discard Player Board")

    async with db_session.begin_nested():
        # Two boards: both players share board_a; only discard is on board_b.
        src_a = await _news_source(db_session)
        board_a = await _board(db_session, src_a)

        src_b = await _news_source(db_session)
        board_b = await _board(db_session, src_b)

        # Survivor on board_a at position 1
        keep_entry_a = BoardEntry(
            board_id=board_a.id,  # type: ignore[arg-type]
            player_id=keep.id,
            position=1,
            raw_name=keep.display_name or "Keep Player Board",
        )
        # Discard on board_a at position 2 — will conflict during merge
        discard_entry_a = BoardEntry(
            board_id=board_a.id,  # type: ignore[arg-type]
            player_id=discard.id,
            position=2,
            raw_name=discard.display_name or "Discard Player Board",
        )
        # Discard on board_b — no conflict; should be reassigned to survivor
        discard_entry_b = BoardEntry(
            board_id=board_b.id,  # type: ignore[arg-type]
            player_id=discard.id,
            position=3,
            raw_name=discard.display_name or "Discard Player Board",
        )
        db_session.add(keep_entry_a)
        db_session.add(discard_entry_a)
        db_session.add(discard_entry_b)
        await db_session.flush()
        keep_entry_a_id = keep_entry_a.id
        discard_entry_b_id = discard_entry_b.id

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    assert report.keep_id == keep.id

    # No entry for the discard player on any board
    orphan_count = (
        await db_session.execute(
            text("SELECT count(*) FROM board_entries WHERE player_id = :pid"),
            {"pid": discard.id},
        )
    ).scalar()
    assert orphan_count == 0, (
        "No board entries should reference the deleted discard player"
    )

    # Survivor's original entry on board_a must be intact (not overwritten)
    keep_a_exists = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM board_entries "
                "WHERE player_id = :pid AND board_id = :bid AND id = :eid"
            ),
            {"pid": keep.id, "bid": board_a.id, "eid": keep_entry_a_id},
        )
    ).scalar()
    assert keep_a_exists == 1, (
        "Survivor's original board_a entry must survive the merge"
    )

    # No duplicate (board_a, keep.id) entries
    dup_count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM board_entries "
                "WHERE player_id = :pid AND board_id = :bid"
            ),
            {"pid": keep.id, "bid": board_a.id},
        )
    ).scalar()
    assert dup_count == 1, "Exactly one entry for survivor on board_a after merge"

    # The board_b entry was reassigned from discard to survivor
    reassigned_b = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM board_entries "
                "WHERE player_id = :pid AND board_id = :bid AND id = :eid"
            ),
            {"pid": keep.id, "bid": board_b.id, "eid": discard_entry_b_id},
        )
    ).scalar()
    assert reassigned_b == 1, "Discard's board_b entry must be reassigned to survivor"

    # Report should note the deleted conflict and the reassigned row
    board_stats = report.per_table.get("board_entries.player_id", {})
    assert board_stats.get("deleted_conflict", 0) == 1, (
        "Report must show 1 deleted_conflict for board_entries"
    )
    assert board_stats.get("reassigned", 0) == 1, (
        "Report must show 1 reassigned for board_entries (the board_b entry)"
    )


# ---------------------------------------------------------------------------
# Summer League + draft/affiliation FKs — ticket #675 (backlog 4.4)
# ---------------------------------------------------------------------------


async def _sl_scaffold(
    db: AsyncSession,
) -> tuple[SummerLeagueCompetition, SummerLeagueTeamEntry, SummerLeagueGame]:
    """Seed one competition, one team entry and one game for SL child rows."""
    from datetime import date

    token = secrets.token_hex(4)
    comp = SummerLeagueCompetition(
        year=2026,
        league_id="15",
        venue_slug=f"merge-test-{token}",
        display_name=f"Merge Test SL {token}",
        starts_on=date(2026, 7, 1),
        ends_on=date(2026, 7, 20),
    )
    db.add(comp)
    await db.flush()

    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"team-{token}",
        raw_team_name=f"Merge Team {token}",
        raw_team_abbreviation="MT",
        team_slug=f"merge-team-{token}",
    )
    db.add(team)
    await db.flush()

    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"game-{token}",
        game_date=date(2026, 7, 10),
        tip_datetime=None,
        home_team_entry_id=team.id,
        away_team_entry_id=team.id,
        status=SummerLeagueGameStatus.FINAL,
    )
    db.add(game)
    await db.flush()
    return comp, team, game


async def _sl_source_player(
    db: AsyncSession, player: PlayerMaster
) -> SummerLeagueSourcePlayer:
    """Seed a source player resolved to the given canonical player."""
    token = secrets.token_hex(4)
    src = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"person-{token}",
        raw_player_name=player.display_name or "SL Player",
        normalized_name=(player.display_name or "sl player").lower(),
        canonical_player_id=player.id,
    )
    db.add(src)
    await db.flush()
    return src


@pytest.mark.asyncio
async def test_merge_summer_league_union(db_session: AsyncSession) -> None:
    """Merging two players who both hold Summer League data succeeds (#675).

    Before the fix, none of the summer_league_* tables (nor draft_results /
    player_affiliations) were registered with the merge service, so the final
    DELETE players_master raised a ForeignKeyViolation.

    Seeds both players with participation, game logs and shot events, plus
    discard-side PBP references on all three person columns, a draft result,
    an affiliation, a resolved source player, a resolution review, and
    storylines on both subject columns. Asserts the merge completes, the
    survivor holds the union, and no column still references the discard.
    """
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep SL Player")
        discard = await _player(db_session, "Discard SL Player")

    async with db_session.begin_nested():
        comp, team, game = await _sl_scaffold(db_session)
        keep_src = await _sl_source_player(db_session, keep)
        discard_src = await _sl_source_player(db_session, discard)

        for player, src in ((keep, keep_src), (discard, discard_src)):
            db_session.add(
                SummerLeagueParticipation(
                    competition_id=comp.id,
                    team_entry_id=team.id,
                    source_player_id=src.id,
                    player_id=player.id,
                )
            )
            db_session.add(
                SummerLeaguePlayerGameLog(
                    competition_id=comp.id,
                    game_id=game.id,
                    team_entry_id=team.id,
                    source_player_id=src.id,
                    player_id=player.id,
                    nba_stats_person_id=src.nba_stats_person_id,
                    raw_player_name=src.raw_player_name,
                    pts=10,
                )
            )
            db_session.add(
                SummerLeagueShotEvent(
                    game_id=game.id,
                    competition_id=comp.id,
                    team_entry_id=team.id,
                    source_player_id=src.id,
                    player_id=player.id,
                    nba_stats_person_id=src.nba_stats_person_id,
                    nba_stats_game_id=game.nba_stats_game_id,
                    nba_stats_game_event_id=1 if player is keep else 2,
                    made=True,
                )
            )

        # Discard appears on each PBP person column (scorer / assist / block).
        for event_num, person_col in (
            (1, "person1_id"),
            (2, "person2_id"),
            (3, "person3_id"),
        ):
            db_session.add(
                SummerLeaguePlayByPlayEvent(
                    game_id=game.id,
                    competition_id=comp.id,
                    nba_stats_game_id=game.nba_stats_game_id,
                    event_num=event_num,
                    **{person_col: discard.id},
                )
            )

        db_session.add(
            DraftResult(
                draft_year=2026,
                overall_pick=59,
                round=2,
                round_pick=29,
                player_id=discard.id,
                raw_player_name=discard.display_name or "Discard SL Player",
            )
        )
        db_session.add(
            PlayerAffiliation(
                player_id=discard.id,
                affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
                source="test_seed",
            )
        )
        db_session.add(
            SummerLeaguePlayerResolutionReview(
                source_player_id=discard_src.id,
                raw_player_name=discard_src.raw_player_name,
                nba_stats_person_id=discard_src.nba_stats_person_id,
                selected_player_id=discard.id,
            )
        )
        db_session.add(
            SummerLeagueDeskStoryline(
                game_date=game.game_date,
                competition_id=comp.id,
                game_id=game.id,
                trigger_type=SummerLeagueDeskTriggerType.STREAK,
                subject_player_id=discard.id,
                base_weight=1.0,
                magnitude=1.0,
                weight=1.0,
            )
        )
        db_session.add(
            SummerLeagueDeskStoryline(
                game_date=game.game_date,
                competition_id=comp.id,
                game_id=game.id,
                trigger_type=SummerLeagueDeskTriggerType.DUEL,
                subject_player_id=keep.id,
                subject_player_id_2=discard.id,
                base_weight=1.0,
                magnitude=1.0,
                weight=1.0,
            )
        )
        await db_session.flush()

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # Discard row must be gone (this DELETE is what used to FK-fail).
    gone = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == discard.id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    assert gone is None, "Discard player row must be deleted"

    # No column may still reference the discard.
    for table, col in [
        ("draft_results", "player_id"),
        ("player_affiliations", "player_id"),
        ("summer_league_participation", "player_id"),
        ("summer_league_player_game_logs", "player_id"),
        ("summer_league_shot_events", "player_id"),
        ("summer_league_source_players", "canonical_player_id"),
        ("summer_league_player_resolution_reviews", "selected_player_id"),
        ("summer_league_play_by_play_events", "person1_id"),
        ("summer_league_play_by_play_events", "person2_id"),
        ("summer_league_play_by_play_events", "person3_id"),
        ("summer_league_desk_storylines", "subject_player_id"),
        ("summer_league_desk_storylines", "subject_player_id_2"),
    ]:
        orphan_count = (
            await db_session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} = :discard_id"),
                {"discard_id": discard.id},
            )
        ).scalar()
        assert orphan_count == 0, (
            f"Orphaned FK in {table}.{col} for discard_id={discard.id}"
        )

    # Survivor holds the union: both game logs, both shot events, both
    # participation rows, and every discard-side reference now points at keep.
    for table, col, expected in [
        ("summer_league_participation", "player_id", 2),
        ("summer_league_player_game_logs", "player_id", 2),
        ("summer_league_shot_events", "player_id", 2),
        ("summer_league_source_players", "canonical_player_id", 2),
        ("summer_league_play_by_play_events", "person1_id", 1),
        ("summer_league_play_by_play_events", "person2_id", 1),
        ("summer_league_play_by_play_events", "person3_id", 1),
        ("draft_results", "player_id", 1),
        ("player_affiliations", "player_id", 1),
        ("summer_league_player_resolution_reviews", "selected_player_id", 1),
        ("summer_league_desk_storylines", "subject_player_id", 2),
        ("summer_league_desk_storylines", "subject_player_id_2", 1),
    ]:
        keep_count = (
            await db_session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} = :keep_id"),
                {"keep_id": keep.id},
            )
        ).scalar()
        assert keep_count == expected, (
            f"Survivor should hold {expected} rows in {table}.{col}, got {keep_count}"
        )

    # Report reflects the reassignments.
    assert (
        report.per_table["summer_league_player_game_logs.player_id"]["reassigned"] == 1
    )
    assert (
        report.per_table["summer_league_play_by_play_events.person2_id"]["reassigned"]
        == 1
    )


@pytest.mark.asyncio
async def test_merge_desk_grades_conflict(db_session: AsyncSession) -> None:
    """Desk grade rows collide on (competition_id, baseline_version) (#675).

    uq_summer_league_desk_player_grades_player_competition_version includes
    player_id, so when both players are graded in the same competition and
    baseline the discard's row must be dropped rather than reassigned; a
    discard-only grade under a different baseline_version is reassigned.
    """
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Graded Player")
        discard = await _player(db_session, "Discard Graded Player")

    async with db_session.begin_nested():
        comp, _team, _game = await _sl_scaffold(db_session)

        def _grade(
            player_id: int, version: str, pctl: float
        ) -> SummerLeagueDeskPlayerGrade:
            return SummerLeagueDeskPlayerGrade(
                player_id=player_id,
                competition_id=comp.id,
                baseline_version=version,
                cohort_key="all",
                subject_value=10.0,
                pctl=pctl,
                grade=SummerLeagueDeskGrade.WARM,
            )

        # Same competition + baseline for both players — unique conflict.
        db_session.add(_grade(keep.id, "v1", 80.0))
        db_session.add(_grade(discard.id, "v1", 40.0))
        # Discard-only baseline — reassigned.
        db_session.add(_grade(discard.id, "v2", 55.0))
        await db_session.flush()

    async with db_session.begin_nested():
        report = await merge_players(
            db_session,
            keep_id=keep.id,  # type: ignore[arg-type]
            discard_id=discard.id,  # type: ignore[arg-type]
        )

    # No grade rows left for the discard.
    orphans = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM summer_league_desk_player_grades "
                "WHERE player_id = :pid"
            ),
            {"pid": discard.id},
        )
    ).scalar()
    assert orphans == 0

    # Survivor keeps its own v1 grade (pctl 80, not the discard's 40) and
    # gains the discard's v2 grade.
    rows = (
        await db_session.execute(
            text(
                "SELECT baseline_version, pctl "
                "FROM summer_league_desk_player_grades "
                "WHERE player_id = :pid ORDER BY baseline_version"
            ),
            {"pid": keep.id},
        )
    ).all()
    assert [(r[0], r[1]) for r in rows] == [("v1", 80.0), ("v2", 55.0)]

    tbl = report.per_table["summer_league_desk_player_grades.player_id"]
    assert tbl["deleted_conflict"] == 1
    assert tbl["reassigned"] == 1


@pytest.mark.asyncio
async def test_preview_merge_reports_summer_league_tables(
    db_session: AsyncSession,
) -> None:
    """preview_merge reports non-zero counts for the #675 tables, without writes."""
    async with db_session.begin_nested():
        keep = await _player(db_session, "Keep Preview Player")
        discard = await _player(db_session, "Discard Preview Player")

    async with db_session.begin_nested():
        comp, team, game = await _sl_scaffold(db_session)
        discard_src = await _sl_source_player(db_session, discard)
        db_session.add(
            SummerLeagueParticipation(
                competition_id=comp.id,
                team_entry_id=team.id,
                source_player_id=discard_src.id,
                player_id=discard.id,
            )
        )
        db_session.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=team.id,
                source_player_id=discard_src.id,
                player_id=discard.id,
                nba_stats_person_id=discard_src.nba_stats_person_id,
                raw_player_name=discard_src.raw_player_name,
            )
        )
        db_session.add(
            DraftResult(
                draft_year=2026,
                overall_pick=60,
                round=2,
                round_pick=30,
                player_id=discard.id,
                raw_player_name=discard.display_name or "Discard Preview Player",
            )
        )
        await db_session.flush()

    preview = await preview_merge(
        db_session,
        keep_id=keep.id,  # type: ignore[arg-type]
        discard_id=discard.id,  # type: ignore[arg-type]
    )

    for key in (
        "summer_league_participation.player_id",
        "summer_league_player_game_logs.player_id",
        "summer_league_source_players.canonical_player_id",
        "draft_results.player_id",
    ):
        assert preview.per_table.get(key, {}).get("reassigned", 0) == 1, (
            f"preview_merge must report the pending reassignment for {key}"
        )

    # Dry run: the discard player and its rows are untouched.
    still_there = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == discard.id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    assert still_there is not None, "preview_merge must not delete the discard player"
    discard_logs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM summer_league_player_game_logs "
                "WHERE player_id = :pid"
            ),
            {"pid": discard.id},
        )
    ).scalar()
    assert discard_logs == 1, "preview_merge must not reassign rows"
