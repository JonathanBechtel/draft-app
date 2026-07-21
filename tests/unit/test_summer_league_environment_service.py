"""Unit tests for Competition Context aggregation math (#617).

Exercise the pure, DB-free computation: every registry formula from pooled
totals, zero/unequal denominators, 40-minute pace normalization, mapped/unmapped
shot zones, score/overtime distribution (including unknown OT), performance-
landscape concentration/IQR, and event-time field composition (draft bands, age,
position event-time-vs-canonical, first-time/returner, unknown attributes).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.services.summer_league.metrics import Box
from app.services.summer_league_environment_registry import (
    CALCULATION_VERSION,
    REGISTRY_VERSION,
    METRICS_BY_KEY,
)
from app.services.summer_league_environment_service import (
    EnvironmentScope,
    _RAW_RUN_STATUS_VALUE_RANK,
    _REQUIRED_BOX_FIELDS,
    _age_reference_info,
    _box_row_usable,
    _build_candidate,
    _CompetitionInputs,
    _coverage_verdict,
    _field_composition,
    _ordered_buckets,
    _percentile,
    _PlayerAttributes,
    _PooledScope,
    _top_decile_share,
    _validate_candidate,
    _worse_status,
    _year_buckets,
    build_profile_summary_view,
    explorer_competitions_href,
)
from app.schemas.summer_league_environment import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    SCOPE_KIND_COMPETITION,
    SCOPE_KIND_SEASON,
    SummerLeagueEnvironmentProfile,
)


def _box(**stats: float) -> Box:
    """Build a summed team Box with the given counting totals."""
    box = Box()
    for key, value in stats.items():
        setattr(box, key, value)
    return box


_FULL_BOX_ROW: dict[str, float] = {
    "minutes": 200,
    "pts": 100,
    "fgm": 40,
    "fga": 85,
    "fg3m": 10,
    "fg3a": 30,
    "fta": 20,
    "oreb": 10,
    "dreb": 30,
    "tov": 15,
    "ast": 22,
}


def _team_box_row(**overrides: object) -> object:
    """A minimal object exposing every ``_REQUIRED_BOX_FIELDS`` attribute."""
    values = {**_FULL_BOX_ROW, **overrides}
    return type("Row", (), values)()


# --------------------------------------------------------------------------- #
# Box-row certification (contract §3: nullable box fields must never fold to 0)
# --------------------------------------------------------------------------- #
def test_box_row_usable_requires_every_registered_field() -> None:
    """A fully-populated, minute-floor-clearing row is usable."""
    assert _box_row_usable(_team_box_row()) is True


@pytest.mark.parametrize("field_name", _REQUIRED_BOX_FIELDS)
def test_box_row_unusable_when_any_required_field_missing(field_name: str) -> None:
    """A null value in any metric-required field disqualifies the row, even
    though the minutes floor is otherwise cleared -- the exact bug the old
    minutes-only check missed."""
    row = _team_box_row(**{field_name: None})
    assert _box_row_usable(row) is False


def test_box_row_unusable_below_minutes_floor() -> None:
    """A short/garbage line below the regulation-minute floor is never usable
    even with every other field populated."""
    row = _team_box_row(minutes=10)
    assert _box_row_usable(row) is False


def _season_pool(**overrides: object) -> _PooledScope:
    """A season-scope pooled object with explicit pre-pooled totals."""
    pooled = _PooledScope(
        scope=EnvironmentScope.for_season(2025),
        display_name="2025 Summer League",
        venue_slug=None,
        members=[],
    )
    for key, value in overrides.items():
        setattr(pooled, key, value)
    return pooled


# --------------------------------------------------------------------------- #
# Percentile / concentration helpers
# --------------------------------------------------------------------------- #
def test_percentile_linear_interpolation() -> None:
    """Percentile matches numpy's default linear interpolation."""
    values = [10.0, 20.0, 30.0, 40.0]
    assert _percentile(values, 0.5) == pytest.approx(25.0)
    assert _percentile(values, 0.25) == pytest.approx(17.5)
    assert _percentile(values, 0.75) == pytest.approx(32.5)


def test_percentile_empty_raises() -> None:
    """An empty distribution has no percentile."""
    with pytest.raises(ValueError):
        _percentile([], 0.5)


def test_top_decile_share_ceilings_to_one_player() -> None:
    """Top decile of 10 participants is the single busiest (ceil(10%) == 1)."""
    minutes = {i: 10.0 for i in range(1, 10)}
    minutes[10] = 100.0  # one dominant player
    # top 1 of 10 holds 100 / (9*10 + 100) = 100/190.
    assert _top_decile_share(minutes) == pytest.approx(100.0 / 190.0)


def test_top_decile_share_zero_total_is_none() -> None:
    """No positive minutes yields None, never a divide-by-zero."""
    assert _top_decile_share({}) is None
    assert _top_decile_share({1: 0.0}) is None


# --------------------------------------------------------------------------- #
# Environment metric formulas from pooled totals
# --------------------------------------------------------------------------- #
def _rich_pool() -> _PooledScope:
    return _season_pool(
        pooled_box=_box(
            pts=220,
            fga=200,
            fgm=90,
            fg3a=60,
            fg3m=24,
            fta=50,
            ftm=40,
            oreb=40,
            dreb=120,
            ast=63,
            tov=20,
        ),
        team_game_rows=4,
        team_minutes=800.0,
        total_possessions=200.0,
        team_ortgs=[95.0, 105.0, 110.0, 120.0, 130.0],
        margin_abs_sum=40.0,
        close_games=3,
        games_with_score=8,
        games_with_known_ot=8,
        overtime_games=2,
        rim_fga=60,
        rim_fgm=36,
        mapped_fga=150,
        minutes_by_identity={1: 100.0, 2: 50.0, 3: 25.0, 4: 25.0},
        points_by_identity={1: 200.0, 2: 60.0, 3: 40.0, 4: 20.0},
    )


def test_environment_formulas_match_registry() -> None:
    """Pooled numerators/denominators reproduce each registry formula exactly."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    values = _environment_metric_values(_rich_pool())
    assert values["points_per_team_game"] == pytest.approx(55.0)  # 220/4
    assert values["estimated_possessions"] == pytest.approx(50.0)  # 200/4
    # pace = 48 * 200 / (800/5) = 48 * 200 / 160 = 60.
    assert values["pace_per_48"] == pytest.approx(60.0)
    assert values["offensive_rating"] == pytest.approx(110.0)  # 100*220/200
    assert values["three_attempt_share"] == pytest.approx(0.30)  # 60/200
    assert values["three_fg_pct"] == pytest.approx(0.40)  # 24/60
    assert values["free_throw_rate"] == pytest.approx(0.25)  # 50/200
    assert values["offensive_rebound_rate"] == pytest.approx(0.25)  # 40/160
    # 20 / (200 + 0.44*50 + 20) = 20/242 (frozen contract §4 formula, not poss).
    assert values["turnover_rate"] == pytest.approx(20.0 / 242.0)
    assert values["assisted_fg_rate"] == pytest.approx(0.70)  # 63/90
    assert values["rim_attempt_share"] == pytest.approx(0.40)  # 60/150
    assert values["rim_fg_pct"] == pytest.approx(0.60)  # 36/60
    assert values["average_score_margin"] == pytest.approx(5.0)  # 40/8
    assert values["close_game_share"] == pytest.approx(0.375)  # 3/8
    assert values["overtime_share"] == pytest.approx(0.25)  # 2/8
    # IQR of [95,105,110,120,130]: Q3(120) - Q1(105) = 15.
    assert values["team_ortg_iqr"] == pytest.approx(15.0)
    # Top decile of 4 identities = ceil(0.4)=1 → busiest.
    assert values["top_decile_minutes_share"] == pytest.approx(100.0 / 200.0)
    assert values["top_decile_points_share"] == pytest.approx(200.0 / 320.0)


def test_zero_denominators_return_none_not_zero() -> None:
    """Every rate with a zero denominator is disclosed as None, never 0.0."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    empty = _season_pool(
        pooled_box=_box(), team_game_rows=0, team_minutes=0.0, total_possessions=0.0
    )
    values = _environment_metric_values(empty)
    for key in (
        "points_per_team_game",
        "estimated_possessions",
        "pace_per_48",
        "offensive_rating",
        "three_attempt_share",
        "turnover_rate",
        "assisted_fg_rate",
        "rim_attempt_share",
        "average_score_margin",
        "overtime_share",
    ):
        assert values[key] is None, key


def test_turnover_rate_zero_plays_returns_none_even_with_possessions() -> None:
    """turnover_rate's denominator is FGA + 0.44*FTA + TOV, not pooled
    possessions -- a pool with zero plays but nonzero estimated possessions
    must still disclose None, never a value borrowed from ``poss``."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    pool = _season_pool(
        pooled_box=_box(tov=0, fga=0, fta=0),
        total_possessions=50.0,
        team_game_rows=2,
    )
    assert _environment_metric_values(pool)["turnover_rate"] is None


def test_turnover_rate_uses_plays_denominator_not_possessions() -> None:
    """turnover_rate is decoupled from the pooled opponent-adjusted possession
    estimate: two unequal-volume pools with the same (deliberately different)
    ``total_possessions`` still resolve to distinct, formula-exact ratios."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    high_volume = _season_pool(
        pooled_box=_box(fga=100, fta=40, tov=15), total_possessions=999.0
    )
    # 15 / (100 + 0.44*40 + 15) = 15/132.6
    assert _environment_metric_values(high_volume)["turnover_rate"] == pytest.approx(
        15.0 / 132.6
    )

    low_volume = _season_pool(
        pooled_box=_box(fga=50, fta=10, tov=8), total_possessions=999.0
    )
    # 8 / (50 + 0.44*10 + 8) = 8/62.4
    assert _environment_metric_values(low_volume)["turnover_rate"] == pytest.approx(
        8.0 / 62.4
    )


def test_overtime_unknown_state_is_none() -> None:
    """Zero games with a known OT state yields None (not 0% overtime)."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    pool = _season_pool(games_with_known_ot=0, overtime_games=0, games_with_score=5)
    assert _environment_metric_values(pool)["overtime_share"] is None


def test_ortg_iqr_needs_minimum_sample() -> None:
    """Fewer than four team-game ratings cannot report a spread."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    pool = _season_pool(team_ortgs=[100.0, 110.0, 120.0])
    assert _environment_metric_values(pool)["team_ortg_iqr"] is None


def test_unmapped_shot_zone_excluded_from_rim_share() -> None:
    """rim_attempt_share divides by mapped non-backcourt FGA only."""
    from app.services.summer_league_environment_service import (
        _environment_metric_values,
    )

    pool = _season_pool(rim_fga=10, rim_fgm=5, mapped_fga=40)
    values = _environment_metric_values(pool)
    assert values["rim_attempt_share"] == pytest.approx(0.25)
    assert values["rim_fg_pct"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Coverage verdicts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("covered", "eligible", "expected"),
    [
        (10, 10, COVERAGE_COMPLETE),
        (4, 10, COVERAGE_PARTIAL),
        (0, 10, COVERAGE_UNAVAILABLE),
        (0, 0, COVERAGE_UNAVAILABLE),
    ],
)
def test_coverage_verdict(covered: int, eligible: int, expected: str) -> None:
    """Covered/eligible pairs map to the frozen coverage vocabulary."""
    assert _coverage_verdict(covered, eligible) == expected


# --------------------------------------------------------------------------- #
# Field composition (event-time draft / age / position / first-time)
# --------------------------------------------------------------------------- #
def _comp_pool(year: int) -> _PooledScope:
    member = _CompetitionInputs(
        competition_id=7,
        year=year,
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas",
        starts_on=date(year, 7, 10),
    )
    return _PooledScope(
        scope=EnvironmentScope.for_competition(7, year),
        display_name=member.display_name,
        venue_slug="las_vegas",
        members=[member],
    )


def test_field_composition_draft_bands_and_first_time() -> None:
    """Draft bands, first-time/returner, and unknown attributes are event-time."""
    year = 2025
    pool = _comp_pool(year)
    pool.resolved_player_ids = {1, 2, 3, 4, 5}
    pool.unresolved_source_ids = {900, 901}
    pool.event_position_by_player = {1: "G", 2: "F"}
    attrs = {
        # Lottery pick this year → drafted, first round, lottery, rookie.
        1: _PlayerAttributes(
            birthdate=date(2004, 1, 1),
            draft_year=2025,
            draft_round=1,
            draft_pick=3,
            canonical_position="PG",
            first_sl_year=2025,
        ),
        # First round non-lottery, returner (appeared before).
        2: _PlayerAttributes(
            birthdate=date(2002, 6, 1),
            draft_year=2023,
            draft_round=1,
            draft_pick=22,
            canonical_position="SF",
            first_sl_year=2023,
        ),
        # Second round.
        3: _PlayerAttributes(
            birthdate=date(2001, 3, 1),
            draft_year=2024,
            draft_round=2,
            draft_pick=40,
            first_sl_year=2024,
        ),
        # Not yet drafted (draft_year in the future) → not undrafted.
        4: _PlayerAttributes(
            draft_year=2026, draft_round=1, draft_pick=5, first_sl_year=2025
        ),
        # No draft record → undrafted; no birthdate → age unknown.
        5: _PlayerAttributes(first_sl_year=2025),
    }
    comp = _field_composition(pool, attrs)

    assert comp.appeared_players == 5
    assert comp.appeared_unresolved == 2
    assert comp.drafted_count == 3  # players 1,2,3
    assert comp.first_round_count == 2  # players 1,2
    assert comp.second_round_count == 1  # player 3
    assert comp.lottery_count == 1  # player 1
    assert comp.undrafted_count == 1  # player 5
    assert comp.rookie_count == 3  # players 1,4,5 (first_sl_year == 2025)
    assert comp.returner_count == 2  # players 2,3

    draft = comp.attributes["draft"]
    assert draft["distribution"]["not_yet_drafted"] == 1
    assert draft["total"] == 5

    age = comp.attributes["age"]
    assert age["known"] == 3  # players 1,2,3 have birthdates
    assert age["unknown"] == 2

    position = comp.attributes["position"]
    # Event-time positions (players 1,2) win; canonical fallback covers player... none else.
    assert position["known"] == 2
    assert position["distribution"]["G"] == 1
    assert position["distribution"]["F"] == 1


def test_field_composition_position_canonical_fallback() -> None:
    """Canonical position is used only when no event-time position exists."""
    pool = _comp_pool(2025)
    pool.resolved_player_ids = {1}
    pool.event_position_by_player = {}  # no event-time signal
    attrs = {1: _PlayerAttributes(canonical_position="C", first_sl_year=2025)}
    comp = _field_composition(pool, attrs)
    assert comp.attributes["position"]["known"] == 1
    assert comp.attributes["position"]["distribution"]["C"] == 1


def test_field_composition_origin_unavailable_v1() -> None:
    """Origin is disclosed as fully unknown for v1 (no inferred origin)."""
    pool = _comp_pool(2025)
    pool.resolved_player_ids = {1, 2}
    comp = _field_composition(pool, {1: _PlayerAttributes(), 2: _PlayerAttributes()})
    origin = comp.attributes["origin"]
    assert origin["known"] == 0
    assert origin["unknown"] == 2
    assert origin["distribution"] is None


def test_median_age_event_time_at_competition_start() -> None:
    """Median age is computed at the competition start date."""
    pool = _comp_pool(2025)
    pool.resolved_player_ids = {1, 2, 3}
    attrs = {
        1: _PlayerAttributes(birthdate=date(2005, 7, 10), first_sl_year=2025),  # 20.0
        2: _PlayerAttributes(birthdate=date(2003, 7, 10), first_sl_year=2025),  # 22.0
        3: _PlayerAttributes(birthdate=date(2001, 7, 10), first_sl_year=2025),  # 24.0
    }
    comp = _field_composition(pool, attrs)
    assert comp.median_age == pytest.approx(22.0, abs=0.1)


# --------------------------------------------------------------------------- #
# New identity/field-composition disclosures (#638): draft class, appearance
# number, age/position fallback-source disclosure, repeat participants, and
# competition start/end dates.
# --------------------------------------------------------------------------- #
def test_ordered_buckets_appends_unmodeled_keys_after_fixed_order() -> None:
    """Fixed-order buckets come first; any unexpected key is appended, never dropped."""
    out = _ordered_buckets({"3": 1, "1": 2, "unexpected": 9}, ("1", "2", "3", "4+"))
    assert out is not None
    assert list(out.items()) == [("1", 2), ("3", 1), ("unexpected", 9)]


def test_ordered_buckets_empty_is_none() -> None:
    """An empty distribution renders as ``None``, never an empty dict."""
    assert _ordered_buckets({}, ("1", "2")) is None


def test_year_buckets_sorts_ascending_with_unknown_trailing() -> None:
    """Draft-class year buckets sort ascending with 'unknown' always last."""
    out = _year_buckets({"2024": 1, "2022": 2, "unknown": 3, "2023": 4})
    assert out is not None
    assert list(out.keys()) == ["2022", "2023", "2024", "unknown"]


def test_year_buckets_empty_is_none() -> None:
    """An empty draft-class distribution renders as ``None``, never an empty dict."""
    assert _year_buckets({}) is None


def test_field_composition_draft_class_distribution() -> None:
    """Draft class buckets by draft-year cohort, independent of event-time status."""
    pool = _comp_pool(2025)
    pool.resolved_player_ids = {1, 2, 3}
    attrs = {
        1: _PlayerAttributes(draft_year=2023, draft_round=1, draft_pick=5),
        2: _PlayerAttributes(draft_year=2026, draft_round=1, draft_pick=2),  # not yet drafted
        3: _PlayerAttributes(),  # no draft record at all
    }
    comp = _field_composition(pool, attrs)
    draft_class = comp.attributes["draft_class"]
    assert draft_class["known"] == 2
    assert draft_class["unknown"] == 1
    assert draft_class["total"] == 3
    assert draft_class["distribution"] == {"2023": 1, "2026": 1}


def test_field_composition_appearance_number_distribution() -> None:
    """Appearance rank buckets 1/2/3/4+ derive from distinct SL years <= profile year."""
    pool = _comp_pool(2026)
    pool.resolved_player_ids = {1, 2, 3, 4}
    attrs = {
        1: _PlayerAttributes(sl_years=(2026,)),  # rank 1
        2: _PlayerAttributes(sl_years=(2024, 2025, 2026)),  # rank 3
        3: _PlayerAttributes(sl_years=(2021, 2022, 2023, 2024, 2026)),  # rank 5 -> "4+"
        4: _PlayerAttributes(sl_years=()),  # no known SL history -> unknown rank
    }
    comp = _field_composition(pool, attrs)
    appearance = comp.attributes["appearance"]
    assert appearance["known"] == 3
    assert appearance["unknown"] == 1
    assert appearance["total"] == 4
    assert appearance["distribution"] == {"1": 1, "3": 1, "4+": 1}


def test_field_composition_age_reference_fallback_disclosure() -> None:
    """Age reference discloses July-1-fallback usage when no event date exists."""
    member = _CompetitionInputs(
        competition_id=7,
        year=2025,
        venue_slug="las_vegas",
        display_name="2025 Las Vegas",
        starts_on=None,  # no known competition date -> fallback
    )
    pool = _PooledScope(
        scope=EnvironmentScope.for_competition(7, 2025),
        display_name=member.display_name,
        venue_slug="las_vegas",
        members=[member],
    )
    pool.resolved_player_ids = {1, 2}
    attrs = {
        1: _PlayerAttributes(birthdate=date(2003, 1, 1)),
        2: _PlayerAttributes(),  # no birthdate -> excluded from age entirely
    }
    comp = _field_composition(pool, attrs)
    age_reference = comp.attributes["age_reference"]
    assert age_reference["total"] == 1  # only player 1 has a known age at all
    assert age_reference["known"] == 0
    assert age_reference["unknown"] == 1  # fallback used
    assert age_reference["reason"] is not None


def test_field_composition_age_reference_known_when_date_present() -> None:
    """A competition with a known start date resolves age from the event date."""
    pool = _comp_pool(2025)  # member.starts_on == date(2025, 7, 10)
    pool.resolved_player_ids = {1}
    attrs = {1: _PlayerAttributes(birthdate=date(2003, 1, 1))}
    comp = _field_composition(pool, attrs)
    age_reference = comp.attributes["age_reference"]
    assert age_reference["known"] == 1
    assert age_reference["unknown"] == 0


def test_field_composition_position_source_disclosure() -> None:
    """Position source discloses event-time vs canonical-fallback resolution."""
    pool = _comp_pool(2025)
    pool.resolved_player_ids = {1, 2, 3}
    pool.event_position_by_player = {1: "G"}  # only player 1 has an event-time position
    attrs = {
        1: _PlayerAttributes(canonical_position="PG"),  # event-time wins
        2: _PlayerAttributes(canonical_position="C"),  # canonical fallback only
        3: _PlayerAttributes(),  # no position at all
    }
    comp = _field_composition(pool, attrs)
    position_source = comp.attributes["position_source"]
    assert position_source["total"] == 2  # players 1 and 2 have a position at all
    assert position_source["known"] == 1  # player 1 (event-time)
    assert position_source["unknown"] == 1  # player 2 (fallback)


def test_field_composition_repeat_participants_season_scope() -> None:
    """A player appearing in >1 member competition counts once as a repeat."""
    member_a = _CompetitionInputs(
        competition_id=1,
        year=2025,
        venue_slug="las_vegas",
        display_name="a",
        starts_on=None,
    )
    member_a.resolved_player_ids = {1, 2}
    member_b = _CompetitionInputs(
        competition_id=2,
        year=2025,
        venue_slug="california_classic",
        display_name="b",
        starts_on=None,
    )
    member_b.resolved_player_ids = {2, 3}
    pool = _PooledScope(
        scope=EnvironmentScope.for_season(2025),
        display_name="2025 Summer League",
        venue_slug=None,
        members=[member_a, member_b],
    )
    pool.resolved_player_ids = {1, 2, 3}
    comp = _field_composition(pool, {})
    assert comp.repeat_participants == 1  # only player 2 appeared in both


def test_field_composition_repeat_participants_none_for_competition_scope() -> None:
    """Repeat participants is not applicable (None) for a single-competition scope."""
    pool = _comp_pool(2025)
    pool.resolved_player_ids = {1, 2}
    comp = _field_composition(pool, {})
    assert comp.repeat_participants is None


def test_age_reference_info_competition_uses_start_date() -> None:
    """A competition scope with a known start date never falls back."""
    pool = _comp_pool(2025)
    reference, used_fallback = _age_reference_info(pool, player_id=1)
    assert reference == date(2025, 7, 10)
    assert used_fallback is False


def test_age_reference_info_falls_back_to_july_first() -> None:
    """A missing competition date falls back to July 1 of the profile year."""
    member = _CompetitionInputs(
        competition_id=7,
        year=2025,
        venue_slug="las_vegas",
        display_name="x",
        starts_on=None,
    )
    pool = _PooledScope(
        scope=EnvironmentScope.for_competition(7, 2025),
        display_name="x",
        venue_slug="las_vegas",
        members=[member],
    )
    reference, used_fallback = _age_reference_info(pool, player_id=1)
    assert reference == date(2025, 7, 1)
    assert used_fallback is True


def test_build_candidate_competition_dates_from_member() -> None:
    """Competition scope stamps starts_on/ends_on from its one member."""
    pool = _comp_pool(2025)
    pool.members[0].ends_on = date(2025, 7, 20)
    candidate = _build_candidate(pool, {})
    assert candidate.profile.starts_on == date(2025, 7, 10)
    assert candidate.profile.ends_on == date(2025, 7, 20)


def test_build_candidate_season_dates_span_members() -> None:
    """Season scope spans the earliest start / latest end among known member dates."""
    member_a = _CompetitionInputs(
        competition_id=1,
        year=2025,
        venue_slug="las_vegas",
        display_name="a",
        starts_on=date(2025, 7, 10),
        ends_on=date(2025, 7, 20),
    )
    member_b = _CompetitionInputs(
        competition_id=2,
        year=2025,
        venue_slug="california_classic",
        display_name="b",
        starts_on=date(2025, 7, 6),
        ends_on=None,  # a missing end date never blanks the window
    )
    pool = _PooledScope(
        scope=EnvironmentScope.for_season(2025),
        display_name="2025 Summer League",
        venue_slug=None,
        members=[member_a, member_b],
    )
    candidate = _build_candidate(pool, {})
    assert candidate.profile.starts_on == date(2025, 7, 6)
    assert candidate.profile.ends_on == date(2025, 7, 20)


def test_validate_candidate_rejects_negative_not_yet_drafted_count() -> None:
    """A tampered negative not-yet-drafted count fails validation."""
    pool = _comp_pool(2025)
    candidate = _build_candidate(pool, {})
    candidate.profile.not_yet_drafted_count = -1
    with pytest.raises(ValueError):
        _validate_candidate(candidate)


def test_validate_candidate_rejects_negative_repeat_participants() -> None:
    """A tampered negative repeat-participant count fails validation."""
    pool = _season_pool()
    candidate = _build_candidate(pool, {})
    candidate.profile.repeat_participants = -1
    with pytest.raises(ValueError):
        _validate_candidate(candidate)


# --------------------------------------------------------------------------- #
# Candidate assembly + validation
# --------------------------------------------------------------------------- #
def test_partial_box_coverage_publishes_null_value() -> None:
    """A box metric under partial coverage is stored as NULL with a reason."""
    pool = _comp_pool(2025)
    pool.final_games = 4
    pool.box_complete_games = 2  # partial box coverage
    pool.pooled_box = _box(pts=200, fga=180, fgm=80, tov=20)
    pool.team_game_rows = 4
    pool.team_minutes = 800.0
    pool.total_possessions = 200.0
    candidate = _build_candidate(pool, {})
    # Value withheld because coverage is not complete.
    assert candidate.profile.points_per_team_game is None
    coverage = {c.metric_key: c for c in candidate.coverage}
    assert coverage["points_per_team_game"].coverage == COVERAGE_PARTIAL
    assert coverage["points_per_team_game"].reason is not None
    _validate_candidate(candidate)  # partial+null is valid


def test_complete_box_coverage_publishes_value() -> None:
    """A box metric with complete coverage stores the rounded pooled value."""
    pool = _comp_pool(2025)
    pool.final_games = 2
    pool.box_complete_games = 2  # complete
    pool.pooled_box = _box(pts=200, fga=180, fgm=80, tov=20)
    pool.team_game_rows = 4
    pool.team_minutes = 800.0
    pool.total_possessions = 200.0
    candidate = _build_candidate(pool, {})
    assert candidate.profile.points_per_team_game == pytest.approx(50.0)  # 200/4
    coverage = {c.metric_key: c for c in candidate.coverage}
    assert coverage["points_per_team_game"].coverage == COVERAGE_COMPLETE


def test_validate_rejects_value_under_partial_coverage() -> None:
    """A box value present under non-complete coverage fails validation."""
    pool = _comp_pool(2025)
    pool.final_games = 4
    pool.box_complete_games = 2
    pool.pooled_box = _box(pts=200, fga=180, fgm=80)
    pool.team_game_rows = 4
    pool.total_possessions = 200.0
    candidate = _build_candidate(pool, {})
    # Tamper: publish a value the coverage does not certify.
    candidate.profile.points_per_team_game = 50.0
    with pytest.raises(ValueError):
        _validate_candidate(candidate)


def test_registry_metric_count_covered() -> None:
    """Every registered metric gets a coverage row in a built candidate."""
    pool = _comp_pool(2025)
    candidate = _build_candidate(pool, {})
    assert len(candidate.coverage) == len(METRICS_BY_KEY)


# --------------------------------------------------------------------------- #
# Distinct calculation version + exact raw-run/source provenance (#641)
# --------------------------------------------------------------------------- #


def test_build_candidate_stamps_distinct_calculation_version() -> None:
    """A built candidate's calculation_version is the registry constant, and
    is never equal to registry_version (the two must stay independently
    meaningful, even though both currently come from the same module)."""
    pool = _comp_pool(2025)
    candidate = _build_candidate(pool, {})
    assert candidate.profile.calculation_version == CALCULATION_VERSION
    assert candidate.profile.registry_version == REGISTRY_VERSION
    assert candidate.profile.calculation_version != candidate.profile.registry_version


def test_build_candidate_carries_pooled_raw_run_ids() -> None:
    """Distinct contributing raw_run_ids pool onto the profile, sorted."""
    pool = _comp_pool(2025)
    pool.raw_run_ids = {9, 3, 3}
    candidate = _build_candidate(pool, {})
    assert candidate.profile.raw_run_ids == [3, 9]


def test_build_candidate_raw_run_ids_none_when_absent() -> None:
    """No linked raw run anywhere in the scope stores None, not an empty list."""
    pool = _comp_pool(2025)
    candidate = _build_candidate(pool, {})
    assert candidate.profile.raw_run_ids is None


def test_build_candidate_provenance_carries_parse_and_source_status() -> None:
    """Provenance rows disclose per-source parse status and the pooled source
    (raw-run) status, populated from the pooled aggregation inputs."""
    pool = _comp_pool(2025)
    pool.provenance["box"].row_count = 4
    pool.parse_status_by_source = {"box": "PARSED"}
    pool.raw_run_status = "PARTIAL"
    candidate = _build_candidate(pool, {})
    box_row = next(r for r in candidate.provenance_rows if r.source_kind == "box")
    assert box_row.parse_status == "PARSED"
    assert box_row.source_status == "PARTIAL"


def test_build_candidate_provenance_status_none_when_unmodeled() -> None:
    """A source with no per-file parse status (e.g. score) stays None, never
    fabricated."""
    pool = _comp_pool(2025)
    pool.provenance["score"].row_count = 2
    candidate = _build_candidate(pool, {})
    score_row = next(r for r in candidate.provenance_rows if r.source_kind == "score")
    assert score_row.parse_status is None
    assert score_row.source_status is None


def test_worse_status_prefers_ranked_worse_value() -> None:
    """The worst-case status wins; ties/better candidates never regress it."""
    assert _worse_status(None, "COMPLETE", _RAW_RUN_STATUS_VALUE_RANK) == "COMPLETE"
    assert (
        _worse_status("COMPLETE", "PARTIAL", _RAW_RUN_STATUS_VALUE_RANK) == "PARTIAL"
    )
    assert _worse_status("FAILED", "COMPLETE", _RAW_RUN_STATUS_VALUE_RANK) == "FAILED"


def test_pooled_scope_pool_aggregates_raw_run_and_parse_status() -> None:
    """`_PooledScope.pool()` unions raw_run_ids and worst-cases status across
    every member competition (season scopes pool several competitions)."""
    member_a = _CompetitionInputs(
        competition_id=1,
        year=2025,
        venue_slug="las_vegas",
        display_name="a",
        starts_on=None,
        raw_run_id=10,
        raw_run_status="COMPLETE",
        parse_status_by_source={"box": "PARSED"},
    )
    member_b = _CompetitionInputs(
        competition_id=2,
        year=2025,
        venue_slug="california_classic",
        display_name="b",
        starts_on=None,
        raw_run_id=11,
        raw_run_status="PARTIAL",
        parse_status_by_source={"box": "PARSE_FAILED"},
    )
    pooled = _PooledScope(
        scope=EnvironmentScope.for_season(2025),
        display_name="2025 Summer League",
        venue_slug=None,
        members=[member_a, member_b],
    )
    pooled.pool()
    assert pooled.raw_run_ids == {10, 11}
    assert pooled.raw_run_status == "PARTIAL"
    assert pooled.parse_status_by_source["box"] == "PARSE_FAILED"


# --------------------------------------------------------------------------- #
# Page-ready DTO shaping for season/venue reuse (#610)
# --------------------------------------------------------------------------- #
def _season_profile(**overrides: object) -> SummerLeagueEnvironmentProfile:
    """A minimal *season_all_competitions* profile row, no DB required."""
    defaults: dict[str, object] = dict(
        id=1,
        scope_key="season:2025",
        scope_kind=SCOPE_KIND_SEASON,
        year=2025,
        competition_id=None,
        venue_slug=None,
        display_name="2025 Summer League (All Competitions)",
        version=3,
        is_current=True,
        registry_version="2026.07.1",
        calculation_version="2026.07.2",
        included_competitions=2,
        final_games=4,
        scheduled_games=1,
        distinct_teams=6,
        box_complete_games=4,
        shot_covered_games=1,
        appeared_players=42,
        appeared_unresolved=2,
    )
    defaults.update(overrides)
    return SummerLeagueEnvironmentProfile(**defaults)  # type: ignore[arg-type]


def test_explorer_competitions_href_season_scope() -> None:
    """A season scope links to the Explorer with `profile_scope=season`."""
    href = explorer_competitions_href(EnvironmentScope.for_season(2025))
    assert href == (
        "/stats/summer-league/explorer"
        "?subject=competitions&profile_scope=season&detail_year=2025"
    )


def test_explorer_competitions_href_competition_scope() -> None:
    """A competition scope links to the Explorer with `profile_scope=competition`."""
    href = explorer_competitions_href(EnvironmentScope.for_competition(77, 2025))
    assert href == (
        "/stats/summer-league/explorer"
        "?subject=competitions&profile_scope=competition&competition_id=77"
    )


def test_build_profile_summary_view_season_identity_and_link() -> None:
    """A season profile's summary carries the right identity + Explorer link."""
    profile = _season_profile(
        pace_per_48=94.5, offensive_rating=110.2, rim_attempt_share=None
    )
    view = build_profile_summary_view(profile)
    assert view.scope_key == "season:2025"
    assert view.scope_kind == SCOPE_KIND_SEASON
    assert view.included_competitions == 2
    assert view.registry_version == "2026.07.1"
    assert view.calculation_version == "2026.07.2"
    assert view.explorer_href == (
        "/stats/summer-league/explorer"
        "?subject=competitions&profile_scope=season&detail_year=2025"
    )


def test_build_profile_summary_view_competition_identity_and_link() -> None:
    """A competition profile's summary carries the right identity + Explorer link."""
    profile = _season_profile(
        id=2,
        scope_key="competition:77",
        scope_kind=SCOPE_KIND_COMPETITION,
        competition_id=77,
        venue_slug="las_vegas",
        display_name="2025 las_vegas",
        included_competitions=1,
    )
    view = build_profile_summary_view(profile)
    assert view.scope_key == "competition:77"
    assert view.scope_kind == SCOPE_KIND_COMPETITION
    assert view.competition_id == 77
    assert view.explorer_href == (
        "/stats/summer-league/explorer"
        "?subject=competitions&profile_scope=competition&competition_id=77"
    )


def test_build_profile_summary_view_partial_metric_does_not_suppress_complete() -> None:
    """A partial-coverage headline metric stays individually unavailable.

    It never hides a sibling metric with complete coverage (contract §3).
    """
    # box_complete_games == final_games -> BOX coverage complete.
    # shot_covered_games (1) < final_games (4) -> SHOT coverage partial.
    profile = _season_profile(pace_per_48=94.5, rim_attempt_share=None)
    view = build_profile_summary_view(profile)
    env = next(s for s in view.sections if s.key == "environment")
    by_key = {m.key: m for m in env.metrics}

    pace = by_key["pace_per_48"]
    assert pace.coverage == COVERAGE_COMPLETE
    assert pace.formatted_value == "94.5"

    rim = by_key["rim_attempt_share"]
    assert rim.coverage == COVERAGE_PARTIAL
    assert rim.formatted_value == "—"  # em dash — never a fabricated zero
    assert rim.reason is not None


def test_build_profile_summary_view_stale_flag() -> None:
    """`is_stale` flips once `calculated_at` exceeds the freshness threshold."""
    fresh = _season_profile(calculated_at=datetime.utcnow() - timedelta(hours=1))
    stale = _season_profile(calculated_at=datetime.utcnow() - timedelta(hours=200))
    assert build_profile_summary_view(fresh, stale_after_hours=72).is_stale is False
    assert build_profile_summary_view(stale, stale_after_hours=72).is_stale is True
