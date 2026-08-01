"""Integration tests for Summer League Desk commentary persistence (#519).

Seeds a real T1 baseline + T2 grade row (via #502/#503's own builders) and a
T4 slate row, then exercises `desk_commentary.persist_grade_facts` /
`persist_slate_facts` end to end: asserts the rendered strings + Fact
provenance actually land on the persisted `facts` JSONB column, and that
`is_hero` gates whether the hero-tagline surface is included.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueCompetition, SummerLeagueGame
from app.schemas.summer_league_desk import (
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
)
from app.services.summer_league.cohort_baselines import build_baselines
from app.services.summer_league.desk_commentary import (
    persist_grade_facts,
    persist_slate_facts,
)
from app.services.summer_league.desk_facts import (
    Fact,
    FactKind,
    FactProvenance,
    FactSubject,
)
from app.services.summer_league.desk_grades import grade_player_event

pytestmark = pytest.mark.asyncio


async def _seed_competition(db: AsyncSession, *, year: int) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id="13",
        venue_slug="las_vegas",
        display_name=f"{year} las_vegas",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_player(
    db: AsyncSession, *, name: str, draft_round: int | None, draft_pick: int | None
) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name,
        last_name="Test",
        display_name=f"{name} Test",
        draft_year=2020,
        draft_round=draft_round,
        draft_pick=draft_pick,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _seed_season(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    player: PlayerMaster,
    year: int,
    gmsc: float,
    minutes: float,
    gp: int,
) -> None:
    from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason

    assert competition.id is not None
    assert player.id is not None
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=year,
            venue_slug=competition.venue_slug,
            is_current=True,
            gp=gp,
            minutes=minutes,
            gmsc=gmsc,
        )
    )
    await db.flush()


async def _seed_lottery_cohort_history(
    db: AsyncSession, *, values: list[float], start_year: int
) -> None:
    for i, gmsc in enumerate(values):
        year = start_year + i
        comp = await _seed_competition(db, year=year)
        player = await _seed_player(db, name=f"Hist{i}", draft_round=1, draft_pick=1)
        await _seed_season(
            db,
            competition=comp,
            player=player,
            year=year,
            gmsc=gmsc,
            minutes=100.0,
            gp=5,
        )


async def test_persist_grade_facts_writes_rendered_prose_chip_and_provenance(
    db_session: AsyncSession,
) -> None:
    """A confident (ungated), notable grade's Facts persist with rendered
    prose + chip + provenance onto the existing T2 row."""
    history = [float(v) for v in range(10, 101, 10)]  # 10 confident historical points
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2014)
    version = await build_baselines(
        db_session, season_range="2014-2023", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(
        db_session, name="Subject", draft_round=1, draft_pick=1
    )
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=95.0,
        minutes=150.0,
        gp=5,
    )
    assert subject.id is not None
    assert subject_comp.id is not None

    grade = await grade_player_event(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
    )
    assert grade.gated is False

    fact = Fact(
        kind=FactKind.PERCENTILE,
        subject=FactSubject(
            player_id=subject.id,
            player_label="Subject Test",
            competition_id=subject_comp.id,
        ),
        metric="gmsc",
        cohort=grade.cohort_key,
        values={
            "value": grade.subject_value,
            "pctl": grade.pctl,
            "n_cohort": grade.n_cohort,
            "gated": grade.gated,
        },
        notability=1.0,
        provenance=FactProvenance(
            detector_id="percentile",
            baseline_version=version,
            cohort_key=grade.cohort_key,
        ),
    )

    payload = await persist_grade_facts(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
        facts=[fact],
    )
    await db_session.flush()

    persisted = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == subject.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == subject_comp.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version == version,  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert persisted.facts == payload
    assert len(persisted.facts) == 1
    entry = persisted.facts[0]
    assert entry["kind"] == "percentile"
    assert entry["provenance"]["detector_id"] == "percentile"
    assert entry["provenance"]["baseline_version"] == version
    assert entry["provenance"]["cohort_key"] == grade.cohort_key
    assert "top-4 cohort" in entry["prose"]  # cohort_key translated, never raw
    assert "slot:" not in entry["prose"]
    assert set(entry["selected_for"]) == {"tick_note", "ledger_echo"}
    assert entry["chip"]


async def test_persist_grade_facts_gated_grade_has_chip_but_no_prose(
    db_session: AsyncSession,
) -> None:
    """A gated grade's Fact still gets a chip; the notability floor keeps it
    out of prose selection (spec: gated grades must not drive confident
    prose, but the chip renders regardless)."""
    history = [float(v) for v in range(10, 101, 10)]
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2014)
    version = await build_baselines(
        db_session, season_range="2014-2023", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(db_session, name="Thin", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=55.0,
        minutes=15.0,
        gp=1,
    )
    assert subject.id is not None
    assert subject_comp.id is not None

    grade = await grade_player_event(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
    )
    assert grade.gated is True

    fact = Fact(
        kind=FactKind.PERCENTILE,
        subject=FactSubject(
            player_id=subject.id,
            player_label="Thin Test",
            competition_id=subject_comp.id,
        ),
        metric="gmsc",
        cohort=grade.cohort_key,
        values={
            "value": grade.subject_value,
            "pctl": grade.pctl,
            "n_cohort": grade.n_cohort,
            "gated": grade.gated,
        },
        # detect_percentile would dampen this well below the notability
        # floor for a gated outcome; set explicitly here to isolate the
        # persistence-layer behavior under test from Stage 1's own damping.
        notability=0.1,
        provenance=FactProvenance(detector_id="percentile", baseline_version=version),
    )

    payload = await persist_grade_facts(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
        facts=[fact],
    )
    entry = payload[0]
    assert entry["selected_for"] == []
    assert entry["prose"] is None
    assert entry["chip"].startswith("early · ")


async def test_persist_grade_facts_raises_for_missing_t2_row(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="summer_league_desk_player_grades"):
        await persist_grade_facts(
            db_session,
            player_id=999999,
            competition_id=999999,
            baseline_version="nope",
            facts=[],
        )


async def test_persist_slate_facts_hero_row_includes_hero_tagline_surface(
    db_session: AsyncSession,
) -> None:
    """`is_hero=True` adds the hero_tagline surface; a non-hero row doesn't."""
    comp = await _seed_competition(db_session, year=2026)
    assert comp.id is not None
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id="test-hero-game",
        game_date=date(2026, 7, 10),
    )
    db_session.add(game)
    await db_session.flush()
    assert game.id is not None

    slate_row = SummerLeagueDeskSlate(
        game_date=date(2026, 7, 10),
        competition_id=comp.id,
        game_id=game.id,
        total_weight=90.0,
        rank=1,
        is_hero=True,
    )
    db_session.add(slate_row)
    await db_session.flush()

    fact = Fact(
        kind=FactKind.DEBUT_VS_BAR,
        subject=FactSubject(
            player_id=1, player_label="Hero Prospect", competition_id=comp.id
        ),
        metric="gmsc",
        cohort="debut:1-4",
        values={"value": 22.0, "bar": 11.2, "delta": 10.8},
        notability=1.0,
        provenance=FactProvenance(detector_id="debut_vs_bar", baseline_version="v1"),
    )

    payload = await persist_slate_facts(
        db_session, game_id=game.id, facts=[fact], is_hero=True
    )

    persisted = (
        await db_session.execute(
            select(SummerLeagueDeskSlate).where(
                SummerLeagueDeskSlate.game_id == game.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert persisted.facts == payload
    entry = persisted.facts[0]
    assert set(entry["selected_for"]) == {"tick_note", "hero_tagline"}
    assert entry["prose"] is not None
    assert "debut" in entry["prose"].lower()
    assert "top-4 cohort" in entry["prose"]  # cohort_key translated, never raw
    assert "debut:" not in entry["prose"]
    assert "mcdonald" not in entry["prose"].lower()


async def test_persist_slate_facts_non_hero_row_excludes_hero_tagline_surface(
    db_session: AsyncSession,
) -> None:
    comp = await _seed_competition(db_session, year=2026)
    assert comp.id is not None
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id="test-nonhero-game",
        game_date=date(2026, 7, 10),
    )
    db_session.add(game)
    await db_session.flush()
    assert game.id is not None

    slate_row = SummerLeagueDeskSlate(
        game_date=date(2026, 7, 10),
        competition_id=comp.id,
        game_id=game.id,
        total_weight=40.0,
        rank=3,
        is_hero=False,
    )
    db_session.add(slate_row)
    await db_session.flush()

    fact = Fact(
        kind=FactKind.DEBUT_VS_BAR,
        subject=FactSubject(
            player_id=2, player_label="Non-hero Prospect", competition_id=comp.id
        ),
        metric="gmsc",
        cohort="debut:1-4",
        values={"value": 22.0, "bar": 11.2, "delta": 10.8},
        notability=1.0,
        provenance=FactProvenance(detector_id="debut_vs_bar", baseline_version="v1"),
    )

    payload = await persist_slate_facts(
        db_session, game_id=game.id, facts=[fact], is_hero=False
    )
    assert payload[0]["selected_for"] == ["tick_note"]


async def test_persist_slate_facts_raises_for_missing_t4_row(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="summer_league_desk_slate"):
        await persist_slate_facts(db_session, game_id=999999, facts=[])


async def test_persist_grade_facts_rerun_overwrites_facts_in_place(
    db_session: AsyncSession,
) -> None:
    """Re-persisting replaces the `facts` payload rather than accumulating
    duplicate entries (mirrors the T2/T3/T4 rebuildable-projection pattern
    used elsewhere in this pipeline)."""
    history = [float(v) for v in range(10, 101, 10)]
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2014)
    version = await build_baselines(
        db_session, season_range="2014-2023", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(db_session, name="Rerun", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=95.0,
        minutes=150.0,
        gp=5,
    )
    assert subject.id is not None
    assert subject_comp.id is not None

    grade = await grade_player_event(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
    )
    subject_ref = FactSubject(
        player_id=subject.id, player_label="Rerun Test", competition_id=subject_comp.id
    )
    fact = Fact(
        kind=FactKind.PERCENTILE,
        subject=subject_ref,
        metric="gmsc",
        cohort=grade.cohort_key,
        values={
            "value": grade.subject_value,
            "pctl": grade.pctl,
            "n_cohort": grade.n_cohort,
            "gated": grade.gated,
        },
        notability=1.0,
        provenance=FactProvenance(detector_id="percentile"),
    )

    await persist_grade_facts(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
        facts=[fact, fact],
    )
    second = await persist_grade_facts(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
        facts=[fact],
    )

    persisted = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == subject.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == subject_comp.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version == version,  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert len(persisted.facts) == 1
    assert persisted.facts == second
