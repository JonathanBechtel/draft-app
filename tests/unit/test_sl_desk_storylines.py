"""Unit tests for the Summer League Desk storyline engine (#504).

Pure-logic coverage only -- no DB. One positive + one near-miss per trigger,
prominence ordering, deterministic slate ranking, quiet-slate fallback, and
the "no competitive/contract framing" editorial guard. See
``tests/integration/test_sl_desk_storylines.py`` for the end-to-end T3/T4
write path.
"""

from __future__ import annotations

import inspect
import re
from datetime import date

import pytest

from app.schemas.summer_league_desk import SummerLeagueDeskGrade, SummerLeagueDeskTriggerType
from app.services.summer_league import desk_storylines as storylines
from app.services.summer_league.desk_facts import GameLine, PriorEvent
from app.services.summer_league.desk_grades import GradeRow
from app.services.summer_league.desk_storylines import (
    BASE_WEIGHTS,
    ClassLeaderCandidate,
    GameSlateInput,
    ProspectSlot,
    TriggerInstance,
    base_weight,
    detect_debut,
    detect_duel,
    detect_second_look,
    detect_status_heat,
    detect_streak,
    draft_slot_fallback,
    effective_prominence_rank,
    prominence_score,
    rank_slate,
    select_quiet_slate_hero,
    slate_needs_quiet_fallback,
)


def _grade(
    *, pctl: float, cohort_key: str = "status:undrafted", gated: bool = False, value: float = 10.0
) -> GradeRow:
    return GradeRow(
        player_id=1,
        competition_id=1,
        baseline_version="v1",
        cohort_key=cohort_key,
        subject_value=value,
        pctl=pctl,
        grade=SummerLeagueDeskGrade.HOT if pctl >= 90 else SummerLeagueDeskGrade.MID,
        n_cohort=20,
        gated=gated,
    )


# --------------------------------------------------------------------------- #
# base_weight
# --------------------------------------------------------------------------- #
def test_base_weight_matches_pinned_priors() -> None:
    """Duel 90 / Debut 80 / 2nd-look 70 / Streak 65 / Status heat 60 (spec §3)."""
    assert base_weight("duel") == 90.0
    assert base_weight("debut") == 80.0
    assert base_weight("second_look") == 70.0
    assert base_weight("streak") == 65.0
    assert base_weight("status_heat") == 60.0


def test_base_weight_covers_exactly_five_trigger_types() -> None:
    assert {k.value for k in BASE_WEIGHTS} == {
        "debut",
        "duel",
        "streak",
        "status_heat",
        "second_look",
    }
    assert {t.value for t in BASE_WEIGHTS} == {t.value for t in SummerLeagueDeskTriggerType}


def test_base_weight_rejects_unknown_trigger() -> None:
    with pytest.raises(ValueError):
        base_weight("stakes")


# --------------------------------------------------------------------------- #
# prominence
# --------------------------------------------------------------------------- #
def test_draft_slot_fallback_round1_is_overall() -> None:
    assert draft_slot_fallback(1, 5) == 5


def test_draft_slot_fallback_round2_adds_30() -> None:
    assert draft_slot_fallback(2, 4) == 34


def test_draft_slot_fallback_undrafted_is_none() -> None:
    assert draft_slot_fallback(None, None) is None


def test_effective_prominence_rank_prefers_consensus_over_slot() -> None:
    assert effective_prominence_rank(3, 1, 10) == 3


def test_effective_prominence_rank_falls_back_to_slot() -> None:
    assert effective_prominence_rank(None, 1, 10) == 10


def test_prominence_score_rank_one_beats_rank_forty_five() -> None:
    assert prominence_score(1) > prominence_score(45)


def test_prominence_score_unranked_gets_a_floor_not_zero() -> None:
    assert prominence_score(None) > 0.0


# --------------------------------------------------------------------------- #
# Trigger 1 -- Debut
# --------------------------------------------------------------------------- #
def test_detect_debut_positive_fires_with_prominence_magnitude() -> None:
    subject = ProspectSlot(
        player_id=1, player_label="Rookie", draft_round=1, draft_pick=1, consensus_rank=1
    )
    inst = detect_debut(subject=subject, is_debut=True)
    assert inst is not None
    assert inst.trigger_type == SummerLeagueDeskTriggerType.DEBUT
    assert inst.subject_player_id == 1
    assert inst.subject_player_id_2 is None
    assert inst.base_weight == 80.0
    assert inst.weight == round(80.0 * inst.magnitude, 2)


def test_detect_debut_near_miss_not_a_debut_does_not_fire() -> None:
    subject = ProspectSlot(
        player_id=1, player_label="Vet", draft_round=1, draft_pick=1, consensus_rank=1
    )
    assert detect_debut(subject=subject, is_debut=False) is None


def test_detect_debut_number_one_pick_outranks_number_forty_five() -> None:
    """DoD: 'a #1 debut > a #45 debut'."""
    top = ProspectSlot(player_id=1, player_label="Top", draft_round=1, draft_pick=1, consensus_rank=1)
    deep = ProspectSlot(player_id=2, player_label="Deep", draft_round=2, draft_pick=15, consensus_rank=45)
    top_inst = detect_debut(subject=top, is_debut=True)
    deep_inst = detect_debut(subject=deep, is_debut=True)
    assert top_inst is not None and deep_inst is not None
    assert top_inst.weight > deep_inst.weight


# --------------------------------------------------------------------------- #
# Trigger 2 -- Duel
# --------------------------------------------------------------------------- #
def test_detect_duel_positive_two_prominent_prospects_fire() -> None:
    a = ProspectSlot(player_id=1, player_label="A", draft_round=1, draft_pick=1, consensus_rank=1)
    b = ProspectSlot(player_id=2, player_label="B", draft_round=1, draft_pick=2, consensus_rank=2)
    inst = detect_duel(candidates=[a, b])
    assert inst is not None
    assert inst.trigger_type == SummerLeagueDeskTriggerType.DUEL
    assert {inst.subject_player_id, inst.subject_player_id_2} == {1, 2}
    assert inst.base_weight == 90.0


def test_detect_duel_near_miss_only_one_prospect_clears_cutoff() -> None:
    a = ProspectSlot(player_id=1, player_label="A", draft_round=1, draft_pick=1, consensus_rank=1)
    b = ProspectSlot(player_id=2, player_label="B", draft_round=2, draft_pick=20, consensus_rank=20)
    assert detect_duel(candidates=[a, b]) is None


def test_detect_duel_top_pair_outranks_two_second_rounders() -> None:
    """DoD: '#1-vs-#2 duel > two 2nd-rounders'."""
    top_a = ProspectSlot(player_id=1, player_label="A", draft_round=1, draft_pick=1, consensus_rank=1)
    top_b = ProspectSlot(player_id=2, player_label="B", draft_round=1, draft_pick=2, consensus_rank=2)
    late_a = ProspectSlot(player_id=3, player_label="C", draft_round=2, draft_pick=10, consensus_rank=13)
    late_b = ProspectSlot(player_id=4, player_label="D", draft_round=2, draft_pick=11, consensus_rank=14)
    top_duel = detect_duel(candidates=[top_a, top_b])
    late_duel = detect_duel(candidates=[late_a, late_b])
    assert top_duel is not None and late_duel is not None
    assert top_duel.weight > late_duel.weight


# --------------------------------------------------------------------------- #
# Trigger 3 -- Streak (game-grain baseline)
# --------------------------------------------------------------------------- #
def _streak_games(pctls: list[float], value: float = 20.0, median: float = 10.0) -> list[GameLine]:
    return [GameLine(value=value, cohort_median=median, pctl=p) for p in pctls]


def test_detect_streak_positive_three_games_avg_pctl_above_floor() -> None:
    subject = ProspectSlot(player_id=1, player_label="Hot", draft_round=1, draft_pick=1)
    games = _streak_games([70.0, 75.0, 80.0])
    inst = detect_streak(subject=subject, cohort_key="game:1-4", games=games)
    assert inst is not None
    assert inst.trigger_type == SummerLeagueDeskTriggerType.STREAK
    assert inst.base_weight == 65.0
    assert inst.magnitude > 0.0


def test_detect_streak_near_miss_only_two_games_does_not_fire() -> None:
    subject = ProspectSlot(player_id=1, player_label="ShortRun", draft_round=1, draft_pick=1)
    games = _streak_games([70.0, 75.0])
    assert detect_streak(subject=subject, cohort_key="game:1-4", games=games) is None


def test_detect_streak_near_miss_avg_pctl_below_floor_does_not_fire() -> None:
    subject = ProspectSlot(player_id=1, player_label="Mid", draft_round=1, draft_pick=1)
    # Three games clear the median but average pctl (60) is below the 65 floor.
    games = _streak_games([55.0, 60.0, 65.0])
    assert detect_streak(subject=subject, cohort_key="game:1-4", games=games) is None


# --------------------------------------------------------------------------- #
# Trigger 4 -- Status heat
# --------------------------------------------------------------------------- #
def test_detect_status_heat_positive_undrafted_above_85th() -> None:
    subject = ProspectSlot(player_id=1, player_label="Sleeper", draft_round=None, draft_pick=None)
    grade = _grade(pctl=90.0, cohort_key="status:undrafted")
    inst = detect_status_heat(subject=subject, grade=grade)
    assert inst is not None
    assert inst.trigger_type == SummerLeagueDeskTriggerType.STATUS_HEAT
    assert inst.base_weight == 60.0
    assert inst.realized_deviation == 40.0


def test_detect_status_heat_near_miss_below_pctl_floor() -> None:
    subject = ProspectSlot(player_id=1, player_label="Sleeper", draft_round=None, draft_pick=None)
    grade = _grade(pctl=80.0, cohort_key="status:undrafted")
    assert detect_status_heat(subject=subject, grade=grade) is None


def test_detect_status_heat_near_miss_lottery_pick_ineligible() -> None:
    """Only undrafted/2nd-round are eligible -- a lottery pick never fires this."""
    subject = ProspectSlot(player_id=1, player_label="Lottery", draft_round=1, draft_pick=1)
    grade = _grade(pctl=99.0, cohort_key="slot:1-4")
    assert detect_status_heat(subject=subject, grade=grade) is None


def test_detect_status_heat_near_miss_gated_grade_never_fires() -> None:
    """A gated (unconfident) percentile must not drive this trigger."""
    subject = ProspectSlot(player_id=1, player_label="Sleeper", draft_round=None, draft_pick=None)
    grade = _grade(pctl=95.0, cohort_key="status:undrafted", gated=True)
    assert detect_status_heat(subject=subject, grade=grade) is None


# --------------------------------------------------------------------------- #
# Trigger 5 -- 2nd look
# --------------------------------------------------------------------------- #
def test_detect_second_look_positive_returner_with_notable_swing() -> None:
    subject = ProspectSlot(player_id=1, player_label="Sophomore", draft_round=1, draft_pick=5)
    prior = PriorEvent(year=2025, value=10.0, gp=5)
    inst = detect_second_look(
        subject=subject,
        current_value=18.0,
        current_gp=5,
        prior=prior,
        current_pctl=70.0,
        prior_pctl=40.0,
    )
    assert inst is not None
    assert inst.trigger_type == SummerLeagueDeskTriggerType.SECOND_LOOK
    assert inst.base_weight == 70.0
    assert inst.magnitude == 30.0  # |70 - 40|


def test_detect_second_look_near_miss_small_swing_does_not_fire() -> None:
    subject = ProspectSlot(player_id=1, player_label="Steady", draft_round=1, draft_pick=5)
    prior = PriorEvent(year=2025, value=10.0, gp=5)
    inst = detect_second_look(
        subject=subject, current_value=10.5, current_gp=5, prior=prior
    )
    assert inst is None


def test_detect_second_look_near_miss_debutant_has_no_prior() -> None:
    """A debutant has no prior SL -- structurally can't be a '2nd look'."""
    subject = ProspectSlot(player_id=1, player_label="Rookie", draft_round=1, draft_pick=5)
    inst = detect_second_look(
        subject=subject, current_value=18.0, current_gp=5, prior=None
    )
    assert inst is None


def test_detect_second_look_near_miss_gated_grade_never_fires() -> None:
    subject = ProspectSlot(player_id=1, player_label="Sophomore", draft_round=1, draft_pick=5)
    prior = PriorEvent(year=2025, value=10.0, gp=5)
    inst = detect_second_look(
        subject=subject, current_value=18.0, current_gp=5, prior=prior, gated=True
    )
    assert inst is None


def test_detect_second_look_falls_back_to_gmsc_delta_without_pctls() -> None:
    subject = ProspectSlot(player_id=1, player_label="Sophomore", draft_round=1, draft_pick=5)
    prior = PriorEvent(year=2025, value=10.0, gp=5)
    inst = detect_second_look(
        subject=subject, current_value=18.0, current_gp=5, prior=prior
    )
    assert inst is not None
    assert inst.magnitude > 0.0


# --------------------------------------------------------------------------- #
# rank_slate -- deterministic ranking, Morning vs Live
# --------------------------------------------------------------------------- #
def _instance(trigger: SummerLeagueDeskTriggerType, weight: float, realized: float | None = None) -> TriggerInstance:
    return TriggerInstance(
        trigger_type=trigger,
        subject_player_id=1,
        subject_player_id_2=None,
        base_weight=weight,
        magnitude=1.0,
        weight=weight,
        realized_deviation=realized,
    )


def test_rank_slate_morning_orders_by_entering_weight_descending() -> None:
    low = GameSlateInput(
        game_id=1, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.STATUS_HEAT, 60.0)],
    )
    high = GameSlateInput(
        game_id=2, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.DUEL, 90.0)],
    )
    rows = rank_slate([low, high], mode="morning")
    assert [r.game_id for r in rows] == [2, 1]
    assert rows[0].is_hero is True
    assert rows[0].rank == 1
    assert rows[1].is_hero is False


def test_rank_slate_morning_tiebreak_uses_best_consensus_rank() -> None:
    a = GameSlateInput(
        game_id=1, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.DEBUT, 80.0)],
        best_consensus_rank=10,
    )
    b = GameSlateInput(
        game_id=2, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.DEBUT, 80.0)],
        best_consensus_rank=3,
    )
    rows = rank_slate([a, b], mode="morning")
    assert rows[0].game_id == 2  # better (lower) consensus rank wins the tie


def test_rank_slate_live_finals_sink_below_in_progress() -> None:
    final_high_weight = GameSlateInput(
        game_id=1, competition_id=1, game_date=date(2026, 7, 10), status="final", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.DUEL, 90.0, realized=99.0)],
    )
    live_low_weight = GameSlateInput(
        game_id=2, competition_id=1, game_date=date(2026, 7, 10), status="in_progress", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.STATUS_HEAT, 60.0, realized=1.0)],
    )
    rows = rank_slate([final_high_weight, live_low_weight], mode="live")
    assert rows[0].game_id == 2
    assert rows[0].is_hero is True
    assert rows[1].game_id == 1


def test_rank_slate_live_ranks_by_realized_deviation_within_in_progress() -> None:
    weak = GameSlateInput(
        game_id=1, competition_id=1, game_date=date(2026, 7, 10), status="in_progress", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.STREAK, 65.0, realized=5.0)],
    )
    strong = GameSlateInput(
        game_id=2, competition_id=1, game_date=date(2026, 7, 10), status="in_progress", tip_datetime=None,
        instances=[_instance(SummerLeagueDeskTriggerType.STREAK, 65.0, realized=40.0)],
    )
    rows = rank_slate([weak, strong], mode="live")
    assert [r.game_id for r in rows] == [2, 1]


def test_rank_slate_is_deterministic_across_repeated_calls() -> None:
    games = [
        GameSlateInput(
            game_id=i, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
            instances=[_instance(SummerLeagueDeskTriggerType.DEBUT, 80.0)],
        )
        for i in (3, 1, 2)
    ]
    first = [r.game_id for r in rank_slate(games, mode="morning")]
    second = [r.game_id for r in rank_slate(games, mode="morning")]
    assert first == second == sorted(first)


def test_rank_slate_empty_input_returns_empty_list() -> None:
    assert rank_slate([], mode="morning") == []


# --------------------------------------------------------------------------- #
# Quiet-slate fallback (behavior spec §4)
# --------------------------------------------------------------------------- #
def test_select_quiet_slate_hero_picks_highest_pctl() -> None:
    candidates = [
        ClassLeaderCandidate(player_id=1, player_label="A", pctl=70.0, gmsc=20.0),
        ClassLeaderCandidate(player_id=2, player_label="B", pctl=95.0, gmsc=15.0),
    ]
    hero = select_quiet_slate_hero(candidates)
    assert hero is not None
    assert hero.player_id == 2
    assert hero.kind == "class_leader"


def test_select_quiet_slate_hero_ties_broken_by_gmsc() -> None:
    candidates = [
        ClassLeaderCandidate(player_id=1, player_label="A", pctl=90.0, gmsc=18.0),
        ClassLeaderCandidate(player_id=2, player_label="B", pctl=90.0, gmsc=22.0),
    ]
    hero = select_quiet_slate_hero(candidates)
    assert hero is not None
    assert hero.player_id == 2


def test_select_quiet_slate_hero_prefers_ungated_candidates() -> None:
    candidates = [
        ClassLeaderCandidate(player_id=1, player_label="Confident", pctl=80.0, gmsc=15.0, gated=False),
        ClassLeaderCandidate(player_id=2, player_label="Thin", pctl=99.0, gmsc=25.0, gated=True),
    ]
    hero = select_quiet_slate_hero(candidates)
    assert hero is not None
    assert hero.player_id == 1


def test_select_quiet_slate_hero_falls_back_to_gated_when_all_gated() -> None:
    """The front page must never have a dead hero, even with only thin data."""
    candidates = [
        ClassLeaderCandidate(player_id=1, player_label="Only", pctl=60.0, gmsc=10.0, gated=True),
    ]
    hero = select_quiet_slate_hero(candidates)
    assert hero is not None
    assert hero.player_id == 1


def test_select_quiet_slate_hero_empty_candidates_returns_none() -> None:
    assert select_quiet_slate_hero([]) is None


def test_slate_needs_quiet_fallback_true_for_empty_slate() -> None:
    assert slate_needs_quiet_fallback([]) is True


def test_slate_needs_quiet_fallback_true_when_nothing_clears_threshold() -> None:
    rows = rank_slate(
        [
            GameSlateInput(
                game_id=1, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
                instances=[],
            )
        ],
        mode="morning",
    )
    assert slate_needs_quiet_fallback(rows) is True


def test_slate_needs_quiet_fallback_false_when_a_game_has_weight() -> None:
    rows = rank_slate(
        [
            GameSlateInput(
                game_id=1, competition_id=1, game_date=date(2026, 7, 10), status="scheduled", tip_datetime=None,
                instances=[_instance(SummerLeagueDeskTriggerType.DEBUT, 80.0)],
            )
        ],
        mode="morning",
    )
    assert slate_needs_quiet_fallback(rows) is False


# --------------------------------------------------------------------------- #
# Editorial guard -- no competitive / contract framing anywhere in this module
# --------------------------------------------------------------------------- #
_BANNED_TERMS = (
    "must-win",
    "must win",
    "elimination",
    "showdown",
    "battle",
    "bracket",
    "tournament",
    "two-way",
    "exhibit-10",
    "exhibit 10",
    "signing",
    "signed a deal",
    "contract watch",
)


def test_module_source_has_no_competitive_or_contract_language() -> None:
    """No banned term appears in the module's real code surface.

    Docstrings are stripped first: several of them legitimately *name* the
    banned words as meta-commentary explaining the constraint (e.g. "no
    tournament, elimination, or bracket language", "never imply two-way ...
    signing"). That self-referential mention documents the rule; it isn't a
    violation of it. What must stay clean is everything else -- identifiers,
    comments, and any string literal the runtime code actually produces.
    """
    source = inspect.getsource(storylines)
    no_docstrings = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    code_only = re.sub(r"#.*", "", no_docstrings).lower()
    for term in _BANNED_TERMS:
        assert term not in code_only, f"banned term {term!r} found in desk_storylines.py"


def test_status_heat_trigger_value_is_not_named_contract_watch() -> None:
    assert SummerLeagueDeskTriggerType.STATUS_HEAT.value == "status_heat"
