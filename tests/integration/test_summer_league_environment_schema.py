"""Integration tests for the Competition Context profile schema (#606).

Persists profiles into a disposable database and asserts the frozen data
contract: stable scope keys, one-current-per-scope enforcement, version
retention with current switching, season-membership uniqueness, per-metric
coverage/field-composition/provenance children, and the indexed current-profile
lookup for both scope kinds.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_environment import (
    SCOPE_KIND_COMPETITION,
    SCOPE_KIND_SEASON,
    SummerLeagueEnvironmentFieldComposition,
    SummerLeagueEnvironmentMetricCoverage,
    SummerLeagueEnvironmentProfile,
    SummerLeagueEnvironmentProvenance,
    SummerLeagueEnvironmentSeasonMembership,
)
from app.services.summer_league_environment_registry import (
    CALCULATION_VERSION,
    REGISTRY_VERSION,
)
from app.services.summer_league_environment_service import (
    EnvironmentScope,
    get_environment_profile,
)


async def _make_competition(
    db: AsyncSession, *, year: int, venue_slug: str
) -> SummerLeagueEdition:
    """Seed one competition for FK targets."""
    comp = SummerLeagueEdition(
        year=year,
        league_id="15",
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


def _season_profile(
    year: int, *, version: int, is_current: bool
) -> SummerLeagueEnvironmentProfile:
    """Build an all-competitions season profile row."""
    return SummerLeagueEnvironmentProfile(
        scope_key=f"season:{year}",
        scope_kind=SCOPE_KIND_SEASON,
        year=year,
        competition_id=None,
        display_name=f"{year} Summer League (all competitions)",
        version=version,
        is_current=is_current,
        registry_version=REGISTRY_VERSION,
        calculation_version=CALCULATION_VERSION,
        included_competitions=3,
        final_games=88,
        pace_per_48=98.4,
        offensive_rating=104.2,
        three_attempt_share=0.401,
        appeared_players=280,
        appeared_unresolved=173,
        calculated_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_scope_key_current_uniqueness(db_session: AsyncSession) -> None:
    """A partial unique index forbids two current profiles for one scope."""
    db_session.add(_season_profile(2025, version=1, is_current=True))
    await db_session.commit()

    db_session.add(_season_profile(2025, version=2, is_current=True))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_scope_version_uniqueness(db_session: AsyncSession) -> None:
    """(scope_key, version) is unique regardless of current flag."""
    db_session.add(_season_profile(2024, version=1, is_current=True))
    await db_session.commit()

    db_session.add(_season_profile(2024, version=1, is_current=False))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_version_retention_and_current_switch(db_session: AsyncSession) -> None:
    """Old versions are retained; flipping current lets a new version be current."""
    v1 = _season_profile(2023, version=1, is_current=True)
    db_session.add(v1)
    await db_session.commit()

    # Demote v1, publish v2 as current in the same transaction.
    v1.is_current = False
    db_session.add(v1)
    db_session.add(_season_profile(2023, version=2, is_current=True))
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(SummerLeagueEnvironmentProfile)
                .where(SummerLeagueEnvironmentProfile.scope_key == "season:2023")
                .order_by(SummerLeagueEnvironmentProfile.version)
            )
        )
        .scalars()
        .all()
    )
    assert [r.version for r in rows] == [1, 2]  # v1 retained
    assert [r.is_current for r in rows] == [False, True]

    current = await get_environment_profile(
        db_session, EnvironmentScope.for_season(2023)
    )
    assert current is not None
    assert current.version == 2


@pytest.mark.asyncio
async def test_get_environment_profile_by_scope(db_session: AsyncSession) -> None:
    """Current-profile lookup resolves both season and competition scopes."""
    comp = await _make_competition(db_session, year=2025, venue_slug="las_vegas")
    db_session.add(_season_profile(2025, version=1, is_current=True))
    db_session.add(
        SummerLeagueEnvironmentProfile(
            scope_key=f"competition:{comp.id}",
            scope_kind=SCOPE_KIND_COMPETITION,
            year=2025,
            competition_id=comp.id,
            venue_slug="las_vegas",
            display_name="2025 Las Vegas",
            version=1,
            is_current=True,
            registry_version=REGISTRY_VERSION,
            calculation_version=CALCULATION_VERSION,
            final_games=76,
            pace_per_48=99.1,
        )
    )
    await db_session.commit()

    season = await get_environment_profile(
        db_session, EnvironmentScope.for_season(2025)
    )
    assert season is not None
    assert season.scope_kind == SCOPE_KIND_SEASON
    assert season.competition_id is None

    assert comp.id is not None
    competition = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp.id, 2025)
    )
    assert competition is not None
    assert competition.scope_kind == SCOPE_KIND_COMPETITION
    assert competition.competition_id == comp.id

    # A scope with no published profile returns None (no fabrication).
    assert (
        await get_environment_profile(db_session, EnvironmentScope.for_season(1999))
        is None
    )


@pytest.mark.asyncio
async def test_season_membership_uniqueness(db_session: AsyncSession) -> None:
    """A competition cannot be counted twice in one season profile."""
    comp = await _make_competition(db_session, year=2022, venue_slug="las_vegas")
    profile = _season_profile(2022, version=1, is_current=True)
    db_session.add(profile)
    await db_session.flush()
    assert profile.id is not None
    assert comp.id is not None

    db_session.add(
        SummerLeagueEnvironmentSeasonMembership(
            profile_id=profile.id, competition_id=comp.id, year=2022, final_games=75
        )
    )
    await db_session.commit()

    db_session.add(
        SummerLeagueEnvironmentSeasonMembership(
            profile_id=profile.id, competition_id=comp.id, year=2022, final_games=75
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_profile_with_children_persists(db_session: AsyncSession) -> None:
    """A representative profile persists with coverage/composition/provenance children."""
    comp = await _make_competition(db_session, year=2021, venue_slug="las_vegas")
    profile = _season_profile(2021, version=1, is_current=True)
    db_session.add(profile)
    await db_session.flush()
    assert profile.id is not None
    assert comp.id is not None

    db_session.add(
        SummerLeagueEnvironmentSeasonMembership(
            profile_id=profile.id, competition_id=comp.id, year=2021, final_games=75
        )
    )
    db_session.add(
        SummerLeagueEnvironmentMetricCoverage(
            profile_id=profile.id,
            metric_key="pace_per_48",
            coverage="complete",
            covered_games=85,
            eligible_games=85,
            reason="every eligible final game box-complete",
        )
    )
    db_session.add(
        SummerLeagueEnvironmentMetricCoverage(
            profile_id=profile.id,
            metric_key="overtime_share",
            coverage="unavailable",
            covered_games=0,
            eligible_games=85,
            reason="status_text OT state populated only for 2026+",
        )
    )
    db_session.add(
        SummerLeagueEnvironmentFieldComposition(
            profile_id=profile.id,
            attribute_key="draft",
            known=137,
            unknown=143,
            total=280,
            distribution={"first_round": 40, "second_round": 30, "undrafted": 67},
        )
    )
    db_session.add(
        SummerLeagueEnvironmentProvenance(
            profile_id=profile.id,
            source_kind="box",
            watermark_at=datetime.utcnow(),
            row_count=170,
            parse_status="complete",
        )
    )
    await db_session.commit()

    coverage = (
        (
            await db_session.execute(
                select(SummerLeagueEnvironmentMetricCoverage).where(
                    SummerLeagueEnvironmentMetricCoverage.profile_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {c.metric_key: c.coverage for c in coverage} == {
        "pace_per_48": "complete",
        "overtime_share": "unavailable",
    }

    comp_rows = (
        (
            await db_session.execute(
                select(SummerLeagueEnvironmentFieldComposition).where(
                    SummerLeagueEnvironmentFieldComposition.profile_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert comp_rows[0].unknown == 143
    assert comp_rows[0].distribution == {
        "first_round": 40,
        "second_round": 30,
        "undrafted": 67,
    }

    prov = (
        (
            await db_session.execute(
                select(SummerLeagueEnvironmentProvenance).where(
                    SummerLeagueEnvironmentProvenance.profile_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert prov[0].source_kind == "box"
    assert prov[0].row_count == 170


@pytest.mark.asyncio
async def test_metric_coverage_uniqueness(db_session: AsyncSession) -> None:
    """One coverage row per (profile, metric)."""
    profile = _season_profile(2020, version=1, is_current=True)
    db_session.add(profile)
    await db_session.flush()
    assert profile.id is not None

    db_session.add(
        SummerLeagueEnvironmentMetricCoverage(
            profile_id=profile.id,
            metric_key="pace_per_48",
            coverage="partial",
            covered_games=1,
            eligible_games=80,
        )
    )
    await db_session.commit()

    db_session.add(
        SummerLeagueEnvironmentMetricCoverage(
            profile_id=profile.id,
            metric_key="pace_per_48",
            coverage="complete",
            covered_games=80,
            eligible_games=80,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
