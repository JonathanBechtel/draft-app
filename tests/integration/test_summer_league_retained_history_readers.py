"""Reader coverage against retained Summer League metric history.

Metric publication is append-only (P2, "longitudinal-first with retained
history"): a superseded projection version keeps its rows with
``is_current=False``, and the archival backfill deliberately writes published
rows that are *never* current.  Every public reader is therefore responsible for
filtering ``is_current`` itself.

Until this module existed, no reader fixture held more than one version of a
(player, competition), so deleting a reader's ``is_current`` filter kept the
whole suite green while production would have shown duplicate season rows on
player pages and double-counted GP/minutes in Explorer career roll-ups.  Each
test below seeds the same (player, competition) three times -- the current
version, an older superseded version, and an archival close -- and asserts the
reader returns the current version's values exactly once.

The three tests cover the three reader families:

* the player page / metrics service (``get_player_metric_seasons``),
* the Leaders boards (``get_competition_leaders`` / ``get_blended_leaders``),
* the Players Explorer (career and per-competition grains).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    run_explorer_query,
)
from app.services.summer_league_metrics_service import (
    get_blended_leaders,
    get_competition_leaders,
    get_player_metric_seasons,
)
from tests.integration.conftest import make_player

# The current version's values. Every assertion below is written against these
# numbers, so a reader that also sees a retained version fails loudly.
CURRENT_GP = 4
CURRENT_MINUTES = 100.0
CURRENT_PTS = 88
CURRENT_PER = 20.0
CURRENT_AS_OF = datetime(2026, 8, 2, 12)

# The retained versions clear every eligibility floor the readers apply
# (``adv_eligible``, DISPLAY_MIN_MINUTES, the leader gates), so ``is_current``
# is the only thing that can keep them out of a result.
SUPERSEDED_MINUTES = 70.0
ARCHIVAL_MINUTES = 40.0


async def _seed_retained_history(
    db: AsyncSession, *, league_id: str
) -> tuple[PlayerMaster, int]:
    """Seed one (player, competition) with current, superseded, and archival rows.

    Args:
        db: Active integration session.
        league_id: Unique league id so parallel fixtures never collide.

    Returns:
        The seeded player and its competition id.
    """
    competition = SummerLeagueEdition(
        year=2026,
        league_id=league_id,
        venue_slug="las_vegas",
        display_name="Retained History",
    )
    player = make_player("Retained", "History")
    db.add_all([competition, player])
    await db.flush()
    assert competition.id is not None
    assert player.id is not None

    shared = {
        "competition_id": competition.id,
        "player_id": player.id,
        "year": 2026,
        "venue_slug": "las_vegas",
        "adv_eligible": True,
    }
    db.add_all(
        [
            # Archival close: published, historical, never current by design.
            SummerLeaguePlayerSeason(
                version=1,
                is_current=False,
                is_archival=True,
                gp=2,
                minutes=ARCHIVAL_MINUTES,
                pts=30,
                per=9.0,
                effective_day=date(2026, 7, 10),
                as_of=datetime(2026, 7, 20, 12),
                published_at=datetime(2026, 7, 20, 13),
                **shared,
            ),
            # Superseded: the version readers used before the latest rebuild.
            SummerLeaguePlayerSeason(
                version=2,
                is_current=False,
                gp=3,
                minutes=SUPERSEDED_MINUTES,
                pts=55,
                per=15.0,
                effective_day=date(2026, 8, 1),
                as_of=datetime(2026, 8, 1, 12),
                published_at=datetime(2026, 8, 1, 13),
                **shared,
            ),
            # Current: the only version any reader may surface.
            SummerLeaguePlayerSeason(
                version=3,
                is_current=True,
                gp=CURRENT_GP,
                minutes=CURRENT_MINUTES,
                pts=CURRENT_PTS,
                per=CURRENT_PER,
                effective_day=date(2026, 8, 2),
                as_of=CURRENT_AS_OF,
                published_at=datetime(2026, 8, 2, 13),
                **shared,
            ),
        ]
    )
    await db.commit()
    return player, competition.id


async def _seed_retained_only_year(
    db: AsyncSession, *, player: PlayerMaster, year: int, league_id: str
) -> None:
    """Seed an earlier summer that exists only as a superseded (never current) row."""
    competition = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug="las_vegas",
        display_name=f"Retained Only {year}",
    )
    db.add(competition)
    await db.flush()
    assert competition.id is not None
    assert player.id is not None
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=year,
            venue_slug="las_vegas",
            version=1,
            is_current=False,
            adv_eligible=True,
            gp=2,
            minutes=45.0,
            pts=22,
            per=11.0,
            effective_day=date(year, 7, 15),
            as_of=datetime(year, 7, 20, 12),
            published_at=datetime(year, 7, 20, 13),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_player_metric_seasons_reads_only_the_current_version(
    db_session: AsyncSession,
) -> None:
    """The player page shows one season line per competition, at current values."""
    player, _competition_id = await _seed_retained_history(
        db_session, league_id="retained-player-page"
    )
    assert player.id is not None

    profile = await get_player_metric_seasons(db_session, player.id)

    assert profile is not None
    assert len(profile.seasons) == 1
    season = profile.seasons[0]
    assert season.per == CURRENT_PER
    assert season.minutes == CURRENT_MINUTES
    assert season.gp == CURRENT_GP
    # The career roll-up sums across competitions, so a retained version would
    # inflate it even if the season list happened to look right.
    assert profile.career.minutes == CURRENT_MINUTES
    assert profile.career.gp == CURRENT_GP
    assert profile.career.adv_pools == 1


@pytest.mark.asyncio
async def test_leaders_boards_read_only_the_current_version(
    db_session: AsyncSession,
) -> None:
    """One leaderboard row per player; the blend never pools retained versions."""
    await _seed_retained_history(db_session, league_id="retained-leaders")

    competition_leaders = await get_competition_leaders(
        db_session, year=2026, venue_slug="las_vegas"
    )
    assert len(competition_leaders.rows) == 1
    assert competition_leaders.rows[0].gp == CURRENT_GP
    assert competition_leaders.rows[0].values["per"] == CURRENT_PER
    assert competition_leaders.rows[0].values["min"] == CURRENT_MINUTES

    blended = await get_blended_leaders(db_session)
    assert len(blended.rows) == 1
    # The blend groups by player and sums minutes/GP across pools: retained
    # versions would silently triple this player's career sample.
    assert blended.rows[0].gp == CURRENT_GP
    assert blended.rows[0].values["min"] == CURRENT_MINUTES
    assert blended.rows[0].values["per"] == CURRENT_PER


@pytest.mark.asyncio
async def test_explorer_reads_only_the_current_version(
    db_session: AsyncSession,
) -> None:
    """Explorer career totals never double-count, and per-competition stays 1:1."""
    player, _competition_id = await _seed_retained_history(
        db_session, league_id="retained-explorer"
    )
    # A summer that exists *only* as retained history. The appearance-rank map is
    # built by its own query, so if that query stopped filtering ``is_current``
    # this phantom 2024 summer would renumber the player's real appearances.
    await _seed_retained_only_year(
        db_session, player=player, year=2024, league_id="retained-explorer-phantom"
    )

    career = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="career",
            mode="totals",
            min_games=1,
            min_minutes=1,
        ),
    )
    assert career.total == 1
    # Career grain groups by player, so a retained version does not add a row --
    # it inflates the summed box totals instead.
    assert career.rows[0].values["pts"] == CURRENT_PTS
    # Snapshot freshness reads the oldest *current* watermark in scope.
    assert career.as_of == CURRENT_AS_OF

    per_competition = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            mode="totals",
            min_games=1,
            min_minutes=1,
        ),
    )
    assert per_competition.total == 1
    assert per_competition.rows[0].values["pts"] == CURRENT_PTS
    assert per_competition.as_of == CURRENT_AS_OF

    # 2026 is this player's first (and only) real summer.
    first_appearance = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="career",
            mode="totals",
            appearance=1,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert first_appearance.total == 1
    assert first_appearance.rows[0].values["pts"] == CURRENT_PTS
