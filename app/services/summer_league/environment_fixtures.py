"""Deterministic Competition Context demo fixture (contract §10).

A single, repeatable seed used by integration tests, the visual harness, and the
standalone demo script so browser/visual verification never depends on
"whichever records happen to exist" in a developer database. It writes only the
derived, versioned environment-profile projection tables (plus the minimal
competition rows the profile/membership foreign keys require) — never raw game
facts.

The dataset deliberately contains, per the frozen implementation contract §10:

* two competitions in one year (2024 Las Vegas + California Classic) with
  unequal denominators and a shared repeat player (disclosed via membership);
* two years for one venue series (Las Vegas 2024 + 2025) plus a stale 2023
  Las Vegas prior with a metric gap;
* complete-box/complete-shot, box-only (shot unavailable), partial, and
  unavailable coverage states;
* scheduled/not-final games counted separately from final games;
* resolved and unresolved appeared players and known/unknown field attributes;
* a stale prior profile served as the last good version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select as _select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueCompetition
from app.schemas.summer_league_environment import (
    SCOPE_KIND_COMPETITION,
    SCOPE_KIND_SEASON,
    SummerLeagueEnvironmentFieldComposition,
    SummerLeagueEnvironmentProfile,
    SummerLeagueEnvironmentSeasonMembership,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league_environment_registry import (
    CALCULATION_VERSION,
    REGISTRY_VERSION,
)
from app.services.summer_league_environment_service import (
    competition_scope_key,
    season_scope_key,
)


@dataclass
class CompetitionContextSeed:
    """Handles to the seeded rows for assertions/navigation."""

    competition_ids: dict[str, int] = field(default_factory=dict)
    profile_ids: dict[str, int] = field(default_factory=dict)
    years: list[int] = field(default_factory=list)


# A representative complete environment/landscape metric block (raw canonical
# values: ratios are 0-1 fractions, scaled ×100 for display).
_COMPLETE_METRICS: dict[str, Optional[float]] = {
    "points_per_team_game": 92.4,
    "estimated_possessions": 88.1,
    "pace_per_48": 99.5,
    "offensive_rating": 104.9,
    "three_attempt_share": 0.401,
    "three_fg_pct": 0.336,
    "free_throw_rate": 0.238,
    "offensive_rebound_rate": 0.281,
    "turnover_rate": 0.164,
    "assisted_fg_rate": 0.552,
    "rim_attempt_share": 0.352,
    "rim_fg_pct": 0.598,
    "average_score_margin": 11.8,
    "close_game_share": 0.333,
    "overtime_share": 0.083,
    "team_ortg_iqr": 18.4,
    "team_points_iqr": 12.5,
    "top_decile_minutes_share": 0.214,
    "top_decile_points_share": 0.271,
    "median_age": 22.1,
}


def _metrics(
    *, box: bool = True, shot: bool = True, score: bool = True, ot: bool = True
) -> dict[str, Optional[float]]:
    """Copy of the complete metric block, nulling metrics an input can't cover."""
    values = dict(_COMPLETE_METRICS)
    if not box:
        for key in (
            "points_per_team_game",
            "estimated_possessions",
            "pace_per_48",
            "offensive_rating",
            "three_attempt_share",
            "three_fg_pct",
            "free_throw_rate",
            "offensive_rebound_rate",
            "turnover_rate",
            "assisted_fg_rate",
            "team_ortg_iqr",
            "team_points_iqr",
            "top_decile_minutes_share",
            "top_decile_points_share",
        ):
            values[key] = None
    if not shot:
        values["rim_attempt_share"] = None
        values["rim_fg_pct"] = None
    if not score:
        values["average_score_margin"] = None
        values["close_game_share"] = None
    if not ot:
        values["overtime_share"] = None
    return values


def _profile(
    *,
    scope_kind: str,
    year: int,
    competition_id: Optional[int],
    venue_slug: Optional[str],
    display_name: str,
    final_games: int,
    scheduled_games: int,
    box_complete_games: int,
    shot_covered_games: int,
    pbp_covered_games: int,
    appeared_players: int,
    appeared_unresolved: int,
    calculated_at: datetime,
    included_competitions: int = 1,
    version: int = 1,
    bump: float = 0.0,
    starts_on: Optional[date] = None,
    ends_on: Optional[date] = None,
    repeat_participants: Optional[int] = None,
) -> SummerLeagueEnvironmentProfile:
    """Build one current profile row with coverage-consistent metric values.

    ``bump`` nudges a few headline metrics per profile so year-over-year trends
    slope visibly instead of rendering as a flat line.
    """
    scope_key = (
        season_scope_key(year)
        if scope_kind == SCOPE_KIND_SEASON
        else competition_scope_key(competition_id)  # type: ignore[arg-type]
    )
    values = _metrics(
        box=box_complete_games >= final_games and final_games > 0,
        shot=shot_covered_games >= final_games and final_games > 0,
        score=final_games > 0,
        ot=final_games > 0,
    )
    if bump and values["pace_per_48"] is not None:
        for key, factor in (
            ("pace_per_48", 1.0),
            ("offensive_rating", 1.4),
            ("points_per_team_game", 1.0),
            ("estimated_possessions", 0.8),
        ):
            base = values[key]
            if base is not None:
                values[key] = round(base + bump * factor, 1)
    drafted_count = int(appeared_players * 0.55)
    not_yet_drafted_count = int(appeared_players * 0.05)
    undrafted_count = appeared_players - drafted_count - not_yet_drafted_count
    return SummerLeagueEnvironmentProfile(
        scope_key=scope_key,
        scope_kind=scope_kind,
        year=year,
        competition_id=competition_id,
        venue_slug=venue_slug,
        display_name=display_name,
        starts_on=starts_on,
        ends_on=ends_on,
        version=version,
        is_current=True,
        registry_version=REGISTRY_VERSION,
        calculation_version=CALCULATION_VERSION,
        included_competitions=included_competitions,
        final_games=final_games,
        scheduled_games=scheduled_games,
        distinct_teams=8 if final_games else 0,
        box_complete_games=box_complete_games,
        shot_covered_games=shot_covered_games,
        pbp_covered_games=pbp_covered_games,
        games_with_score=final_games,
        games_with_known_ot=final_games,
        appeared_players=appeared_players,
        appeared_unresolved=appeared_unresolved,
        participation_count=appeared_players + appeared_unresolved,
        player_games=final_games * 20,
        rookie_count=int(appeared_players * 0.45),
        returner_count=appeared_players - int(appeared_players * 0.45),
        drafted_count=drafted_count,
        undrafted_count=undrafted_count,
        not_yet_drafted_count=not_yet_drafted_count,
        first_round_count=int(appeared_players * 0.3),
        second_round_count=int(appeared_players * 0.3),
        lottery_count=int(appeared_players * 0.15),
        teams_represented=8 if final_games else 0,
        median_age=values["median_age"],
        repeat_participants=repeat_participants,
        calculated_at=calculated_at,
        source_watermark=calculated_at,
        **{k: v for k, v in values.items() if k != "median_age"},
    )


def _field_comp(
    attribute_key: str,
    known: int,
    unknown: int,
    distribution: Optional[dict[str, int]] = None,
    reason: Optional[str] = None,
) -> SummerLeagueEnvironmentFieldComposition:
    return SummerLeagueEnvironmentFieldComposition(
        profile_id=0,  # set by caller after flush
        attribute_key=attribute_key,
        known=known,
        unknown=unknown,
        total=known + unknown,
        distribution=distribution,
        reason=reason,
    )


# Fallback-usage disclosure reasons, matching the live aggregation's wording
# (app.services.summer_league_environment_service._field_composition) so the
# demo fixture reads identically to a real rebuild.
_AGE_REFERENCE_REASON = (
    "known = age computed at the exact competition/appearance date; unknown "
    "= July 1 fallback used because the event date was unavailable."
)
_POSITION_SOURCE_REASON = (
    "known = event-time roster/starter position; unknown = canonical "
    "player_status position used as a labeled fallback."
)
_ORIGIN_REASON = (
    "Pre-event college/international affiliation provenance is not yet "
    "sufficient to certify this distribution in v1; disclosed as fully "
    "unavailable rather than inferred from current biography."
)


async def seed_competition_context_demo(
    session: AsyncSession,
) -> CompetitionContextSeed:
    """Seed the deterministic Competition Context demo dataset.

    Idempotent for a fresh/isolated database: it assumes the environment tables
    are empty (integration fixtures drop/create per session; the demo script
    targets a throwaway database). Commits nothing — the caller owns the
    transaction boundary.
    """
    refs = CompetitionContextSeed()
    now = datetime(2026, 7, 18, 12, 0, 0)
    stale = now - timedelta(days=10)

    # --- Competitions (FK targets for competition profiles + membership) ---
    comps = {
        "lv2023": SummerLeagueCompetition(
            year=2023,
            league_id="15",
            venue_slug="las_vegas",
            display_name="2023 Las Vegas",
            bump=-2.0,
            starts_on=date(2023, 7, 7),
            ends_on=date(2023, 7, 17),
        ),
        "lv2024": SummerLeagueCompetition(
            year=2024,
            league_id="15",
            venue_slug="las_vegas",
            display_name="2024 Las Vegas",
            starts_on=date(2024, 7, 12),
            ends_on=date(2024, 7, 22),
        ),
        "cc2024": SummerLeagueCompetition(
            year=2024,
            league_id="13",
            venue_slug="california_classic",
            display_name="2024 California Classic",
            bump=-1.5,
            starts_on=date(2024, 7, 6),
            ends_on=date(2024, 7, 9),
        ),
        "slc2024": SummerLeagueCompetition(
            year=2024,
            league_id="16",
            venue_slug="salt_lake_city",
            display_name="2024 Salt Lake City",
            starts_on=date(2024, 7, 8),
            ends_on=date(2024, 7, 12),
        ),
        "lv2025": SummerLeagueCompetition(
            year=2025,
            league_id="15",
            venue_slug="las_vegas",
            display_name="2025 Las Vegas",
            bump=3.4,
            starts_on=date(2025, 7, 10),
            ends_on=date(2025, 7, 20),
        ),
    }
    # Get-or-create by (year, league_id) so the demo script can re-seed a
    # throwaway database that already holds a prior run's competition rows
    # (integration tests always start from an empty schema, so this creates).
    for key, comp in comps.items():
        existing = (
            await session.execute(
                _select(SummerLeagueCompetition).where(
                    SummerLeagueCompetition.year == comp.year,  # type: ignore[arg-type]
                    SummerLeagueCompetition.league_id == comp.league_id,  # type: ignore[arg-type]
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            refs.competition_ids[key] = existing.id  # type: ignore[assignment]
        else:
            session.add(comp)
            await session.flush()
            refs.competition_ids[key] = comp.id  # type: ignore[assignment]

    # --- Profiles ---
    profiles: dict[str, SummerLeagueEnvironmentProfile] = {}

    # Season 2024: complete, pools three competitions (lv + cc + slc).
    profiles["season2024"] = _profile(
        scope_kind=SCOPE_KIND_SEASON,
        year=2024,
        competition_id=None,
        venue_slug=None,
        display_name="2024 Summer League (all competitions)",
        bump=0.0,
        final_games=26,
        scheduled_games=2,
        box_complete_games=26,
        shot_covered_games=26,
        pbp_covered_games=26,
        appeared_players=180,
        appeared_unresolved=6,
        calculated_at=now,
        included_competitions=3,
        starts_on=date(2024, 7, 6),  # earliest of lv/cc/slc 2024
        ends_on=date(2024, 7, 22),  # latest of lv/cc/slc 2024
        repeat_participants=8,
    )
    # Season 2025: complete.
    profiles["season2025"] = _profile(
        scope_kind=SCOPE_KIND_SEASON,
        year=2025,
        competition_id=None,
        venue_slug=None,
        display_name="2025 Summer League (all competitions)",
        bump=2.6,
        final_games=24,
        scheduled_games=0,
        box_complete_games=24,
        shot_covered_games=24,
        pbp_covered_games=20,
        appeared_players=170,
        appeared_unresolved=3,
        calculated_at=now,
        included_competitions=2,
        starts_on=date(2025, 7, 10),
        ends_on=date(2025, 7, 20),
        repeat_participants=3,
    )
    # Season 2023: box-partial.
    profiles["season2023"] = _profile(
        scope_kind=SCOPE_KIND_SEASON,
        year=2023,
        competition_id=None,
        venue_slug=None,
        display_name="2023 Summer League (all competitions)",
        bump=-3.1,
        final_games=22,
        scheduled_games=0,
        box_complete_games=10,  # partial box coverage
        shot_covered_games=0,
        pbp_covered_games=0,
        appeared_players=150,
        appeared_unresolved=12,
        calculated_at=now,
        included_competitions=2,
        starts_on=date(2023, 7, 7),
        ends_on=date(2023, 7, 17),
        repeat_participants=5,
    )

    # Competition lv2024: complete box + shot.
    profiles["lv2024"] = _profile(
        scope_kind=SCOPE_KIND_COMPETITION,
        year=2024,
        competition_id=refs.competition_ids["lv2024"],
        venue_slug="las_vegas",
        display_name="2024 Las Vegas",
        bump=1.2,
        final_games=18,
        scheduled_games=1,
        box_complete_games=18,
        shot_covered_games=18,
        pbp_covered_games=18,
        appeared_players=120,
        appeared_unresolved=2,
        calculated_at=now,
        starts_on=comps["lv2024"].starts_on,
        ends_on=comps["lv2024"].ends_on,
    )
    # Competition cc2024: box-only (shot unavailable → rim metrics NULL).
    profiles["cc2024"] = _profile(
        scope_kind=SCOPE_KIND_COMPETITION,
        year=2024,
        competition_id=refs.competition_ids["cc2024"],
        venue_slug="california_classic",
        display_name="2024 California Classic",
        final_games=6,
        scheduled_games=0,
        box_complete_games=6,
        shot_covered_games=0,  # no shot chart
        pbp_covered_games=0,
        appeared_players=48,
        appeared_unresolved=4,
        calculated_at=now,
        starts_on=comps["cc2024"].starts_on,
        ends_on=comps["cc2024"].ends_on,
    )
    # Competition slc2024: unavailable (0 final games — in-progress/empty).
    profiles["slc2024"] = _profile(
        scope_kind=SCOPE_KIND_COMPETITION,
        year=2024,
        competition_id=refs.competition_ids["slc2024"],
        venue_slug="salt_lake_city",
        display_name="2024 Salt Lake City",
        final_games=0,
        scheduled_games=4,
        box_complete_games=0,
        shot_covered_games=0,
        pbp_covered_games=0,
        appeared_players=0,
        appeared_unresolved=0,
        calculated_at=now,
        starts_on=comps["slc2024"].starts_on,
        ends_on=comps["slc2024"].ends_on,
    )
    # Competition lv2025: complete (venue-series point 2).
    profiles["lv2025"] = _profile(
        scope_kind=SCOPE_KIND_COMPETITION,
        year=2025,
        competition_id=refs.competition_ids["lv2025"],
        venue_slug="las_vegas",
        display_name="2025 Las Vegas",
        final_games=17,
        scheduled_games=0,
        box_complete_games=17,
        shot_covered_games=17,
        pbp_covered_games=12,
        appeared_players=115,
        appeared_unresolved=1,
        calculated_at=now,
        starts_on=comps["lv2025"].starts_on,
        ends_on=comps["lv2025"].ends_on,
    )
    # Competition lv2023: STALE prior + box-partial gap (venue-series point 0).
    profiles["lv2023"] = _profile(
        scope_kind=SCOPE_KIND_COMPETITION,
        year=2023,
        competition_id=refs.competition_ids["lv2023"],
        venue_slug="las_vegas",
        display_name="2023 Las Vegas",
        final_games=16,
        scheduled_games=0,
        box_complete_games=7,  # partial → pace gap
        shot_covered_games=0,
        pbp_covered_games=0,
        appeared_players=110,
        appeared_unresolved=8,
        calculated_at=stale,  # older than STALE_AFTER_HOURS
        starts_on=comps["lv2023"].starts_on,
        ends_on=comps["lv2023"].ends_on,
    )

    for profile in profiles.values():
        session.add(profile)
    await session.flush()
    refs.profile_ids = {k: p.id for k, p in profiles.items()}  # type: ignore[misc]
    refs.years = [2023, 2024, 2025]

    # --- Season 2024 membership (names every competition, incl. 0-final SLC) ---
    for comp_key, final in (("lv2024", 18), ("cc2024", 6), ("slc2024", 0)):
        session.add(
            SummerLeagueEnvironmentSeasonMembership(
                profile_id=refs.profile_ids["season2024"],
                competition_id=refs.competition_ids[comp_key],
                year=2024,
                venue_slug=comps[comp_key].venue_slug,
                final_games=final,
            )
        )
    for comp_key, final in (("lv2025", 17),):
        session.add(
            SummerLeagueEnvironmentSeasonMembership(
                profile_id=refs.profile_ids["season2025"],
                competition_id=refs.competition_ids[comp_key],
                year=2025,
                venue_slug=comps[comp_key].venue_slug,
                final_games=final,
            )
        )

    # --- Field composition for the detail profiles (known/unknown/total) ---
    def add_field_comp(
        profile_key: str, players: int, unknown_pos: int, year: int
    ) -> None:
        pid = refs.profile_ids[profile_key]
        age_known = int(players * 0.85)
        position_known = players - unknown_pos
        age_ref_known = int(age_known * 0.7)
        position_event_time = int(position_known * 0.24)  # ~event-time coverage
        rows = [
            _field_comp(
                "draft",
                known=int(players * 0.9),
                unknown=players - int(players * 0.9),
                distribution={
                    "lottery": int(players * 0.15),
                    "first_round": int(players * 0.15),
                    "second_round": int(players * 0.3),
                    "undrafted": int(players * 0.25),
                    "not_yet_drafted": int(players * 0.05),
                },
            ),
            _field_comp(
                "draft_class",
                known=int(players * 0.6),
                unknown=players - int(players * 0.6),
                distribution={
                    str(year - 2): int(players * 0.15),
                    str(year - 1): int(players * 0.2),
                    str(year): int(players * 0.15),
                    str(year + 1): int(players * 0.1),
                },
            ),
            _field_comp("age", known=age_known, unknown=players - age_known),
            _field_comp(
                "age_reference",
                known=age_ref_known,
                unknown=age_known - age_ref_known,
                reason=_AGE_REFERENCE_REASON,
            ),
            _field_comp(
                "position",
                known=position_known,
                unknown=unknown_pos,
                distribution={
                    "Guards": players // 3,
                    "Wings": players // 3,
                    "Bigs": players // 3,
                },
            ),
            _field_comp(
                "position_source",
                known=position_event_time,
                unknown=position_known - position_event_time,
                reason=_POSITION_SOURCE_REASON,
            ),
            _field_comp(
                "appearance",
                known=players,
                unknown=0,
                distribution={
                    "1": int(players * 0.45),
                    "2": int(players * 0.3),
                    "3": int(players * 0.15),
                    "4+": players
                    - int(players * 0.45)
                    - int(players * 0.3)
                    - int(players * 0.15),
                },
            ),
            _field_comp(
                "origin",
                known=int(players * 0.4),
                unknown=players - int(players * 0.4),
                reason=_ORIGIN_REASON,
            ),
        ]
        for row in rows:
            row.profile_id = pid
            session.add(row)

    add_field_comp("season2024", 180, unknown_pos=40, year=2024)
    add_field_comp("lv2024", 120, unknown_pos=25, year=2024)
    add_field_comp("cc2024", 48, unknown_pos=12, year=2024)
    add_field_comp("lv2025", 115, unknown_pos=20, year=2025)

    # --- Leaders strip fixture (contract: leaders "presentation over
    # existing Players Explorer results") -- a handful of real
    # SummerLeaguePlayerSeason rows tied to the lv2024 competition so the
    # Competition Context leaders boards (and the season2024 pool, which
    # includes every 2024 competition) have real, distinct PTS/REB/AST
    # leaders to render and assert against. cc2024/slc2024 deliberately carry
    # no player-season rows: cc2024's detail is box-only anyway (a natural
    # "no leaders yet" honest-unavailable case).
    await _seed_leaders_players(session, competition_id=refs.competition_ids["lv2024"])

    await session.flush()
    return refs


async def _seed_leaders_players(session: AsyncSession, *, competition_id: int) -> None:
    """Seed a small, deterministic PTS/REB/AST leaderboard for one competition.

    Five players with distinct per-game leaders in each category (idempotent:
    skipped if this competition already has player-season rows, so re-seeding
    a throwaway database stays deterministic).
    """
    existing = (
        await session.execute(
            _select(SummerLeaguePlayerSeason.id)  # type: ignore[call-overload]
            .where(
                SummerLeaguePlayerSeason.competition_id == competition_id,
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        return

    # (display_name, total pts, total reb, total ast) over gp=3 games —
    # per-game leaders: Ace Scorer (PTS, 20.0), Bo Board (REB, 10.0),
    # Cy Dish (AST, 5.0), each clearing the Players Explorer default
    # eligibility floor (2+ games, 60+ minutes).
    players = (
        ("Ace Scorer", 60, 15, 9),
        ("Bo Board", 45, 30, 6),
        ("Cy Dish", 30, 9, 15),
        ("Dee Wing", 51, 12, 3),
        ("Eli Bench", 24, 6, 3),
    )
    for display_name, pts, reb, ast in players:
        player = PlayerMaster(display_name=display_name)
        session.add(player)
        await session.flush()
        session.add(
            SummerLeaguePlayerSeason(
                competition_id=competition_id,
                player_id=player.id,
                year=2024,
                venue_slug="las_vegas",
                gp=3,
                minutes=90.0,
                pts=pts,
                reb=reb,
                ast=ast,
            )
        )
