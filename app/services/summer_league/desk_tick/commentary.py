"""Commentary/roster wiring for the Desk projection class (#699 extraction).

Split out of the former `app/cli/sl_desk_tick.py` verbatim so
`app.services.summer_league.desk_tick.projection` stays a readable
orchestrator rather than carrying ~350 lines of detector wiring inline. No
behavior changed in the move -- this is the same eight-detector pass, fed by
the same batched reads, persisting through the same bulk upserts.

The CRITICAL constraint this module inherits: every context read is issued
ONCE for the whole competition/roster per run, never once per player. See
`app/services/summer_league/desk_fact_queries.py` for exactly how each query
is batched, and `tests/integration/perf/test_desk_tick_query_growth.py` for
the behavioral guard that none of it scales with roster size.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueParticipation,
)
from app.schemas.summer_league_desk import SummerLeagueDeskGrain
from app.services.summer_league.cohort_baselines import cohort_key_for
from app.services.summer_league.desk_commentary import (
    persist_grade_facts_bulk,
    persist_slate_facts_bulk,
)
from app.services.summer_league.desk_fact_queries import (
    club_members_clearing,
    cohort_peers,
    count_club_threshold,
    field_peers,
    fetch_cohort_members,
    fetch_current_event_gp,
    fetch_debut_baselines,
    fetch_debut_status,
    fetch_event_baselines,
    fetch_game_baselines,
    fetch_game_lines,
    fetch_prior_events,
    fetch_tonight_field,
    most_recent_prior_holder,
)
from app.services.summer_league.desk_facts import (
    Fact,
    FactSubject,
    detect_cohort_rank,
    detect_count_club,
    detect_debut_vs_bar,
    detect_first_since,
    detect_leads_field,
    detect_percentile,
    detect_self_delta,
    detect_streak,
)
from app.services.summer_league.desk_grades import GradeRow
from app.services.summer_league.desk_storylines import SlateRow
from app.services.summer_league.desk_tick.shared import ROSTER_ACTIVE_STATUSES


async def active_roster_player_ids(db: AsyncSession, competition_id: int) -> list[int]:
    """Every distinct player_id with an active roster row for ``competition_id``."""
    stmt = select(  # type: ignore[call-overload]
        SummerLeagueParticipation.player_id, SummerLeagueParticipation.roster_status
    ).where(
        SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
        SummerLeagueParticipation.player_id.is_not(None),  # type: ignore[union-attr]
    )
    rows = (await db.execute(stmt)).all()
    ordered: dict[int, None] = {}
    for player_id, roster_status in rows:
        if player_id is None or roster_status not in ROSTER_ACTIVE_STATUSES:
            continue
        ordered[player_id] = None
    return list(ordered.keys())


async def game_roster_player_ids(
    db: AsyncSession, *, competition_id: int, game_date: date
) -> dict[int, list[int]]:
    """Map each of ``game_date``'s games in ``competition_id`` to its rostered player_ids.

    Reads the same roster shape ``desk_storylines.compute_desk_storylines``
    builds internally (not exposed by that module) so commentary Facts can be
    grouped per game for
    :func:`~app.services.summer_league.desk_commentary.persist_slate_facts_bulk`.
    """
    games = (
        (
            await db.execute(
                select(SummerLeagueGame).where(
                    SummerLeagueGame.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueGame.game_date == game_date,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    if not games:
        return {}

    team_entry_ids = {
        tid
        for g in games
        for tid in (g.home_team_entry_id, g.away_team_entry_id)
        if tid is not None
    }
    roster_rows = (
        (
            await db.execute(
                select(  # type: ignore[call-overload]
                    SummerLeagueParticipation.team_entry_id,
                    SummerLeagueParticipation.player_id,
                    SummerLeagueParticipation.roster_status,
                ).where(
                    SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueParticipation.team_entry_id.in_(team_entry_ids),  # type: ignore[attr-defined]
                    SummerLeagueParticipation.player_id.is_not(None),  # type: ignore[union-attr]
                )
            )
        ).all()
        if team_entry_ids
        else []
    )
    roster_by_team: dict[int, list[int]] = {}
    for team_entry_id, player_id, roster_status in roster_rows:
        if roster_status not in ROSTER_ACTIVE_STATUSES or player_id is None:
            continue
        roster_by_team.setdefault(team_entry_id, []).append(player_id)

    out: dict[int, list[int]] = {}
    for g in games:
        assert g.id is not None
        ids = list(
            dict.fromkeys(
                roster_by_team.get(g.home_team_entry_id or -1, [])
                + roster_by_team.get(g.away_team_entry_id or -1, [])
            )
        )
        out[g.id] = ids
    return out


def season_range_start(season_range: str) -> int:
    """``"2017-2025"`` -> ``2017`` (mirrors ``cohort_baselines._parse_season_range``)."""
    start_str, _sep, _end_str = season_range.partition("-")
    return int(start_str)


async def commentary_for_competition(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    baseline_version: str,
    game_date: date,
    grade_by_player: dict[int, GradeRow],
    slate: Sequence[SlateRow],
) -> None:
    """All eight #520 Facts onto graded T2 rows and their T4 game rows.

    Wires every detector `desk_facts.py` (#520) ships, each fed by the batched
    read layer `desk_fact_queries.py` (#524) -- ``percentile`` (always fires,
    straight off the T2 :class:`GradeRow`), ``cohort_rank``, ``streak``,
    ``self_delta``, ``leads_field``, ``debut_vs_bar``, ``count_club``, and
    ``first_since``.

    Args:
        db: Active database session.
        competition: The competition being graded/storylined -- ``.year``
            scopes prior-event/debut-status/count-club lookups.
        baseline_version: The T1 baseline version graded/storylines ran against.
        game_date: Today's (Eastern) slate date.
        grade_by_player: Every player graded this run for this competition.
        slate: This competition's ranked slate rows
            (``desk_storylines.SlateRow``) -- persisted onto only if non-empty.
    """
    if not grade_by_player:
        return

    assert competition.id is not None
    competition_id = competition.id
    player_ids = list(grade_by_player.keys())

    players = (
        (
            await db.execute(
                select(PlayerMaster).where(  # type: ignore[call-overload]
                    PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    player_by_id = {p.id: p for p in players if p.id is not None}
    label_by_id = {p.id: (p.display_name or f"Player {p.id}") for p in players}

    # -- Batched context fetches (once per competition/run, never per player) --
    cohort_keys = {g.cohort_key for g in grade_by_player.values()}
    event_baselines = await fetch_event_baselines(
        db, baseline_version=baseline_version, cohort_keys=list(cohort_keys)
    )
    cohort_members_by_key = await fetch_cohort_members(
        db, baselines_by_cohort=event_baselines
    )

    debut_status = await fetch_debut_status(
        db, player_ids=player_ids, before_year=competition.year
    )
    debut_cohort_keys = {
        cohort_key_for(
            player_by_id[pid].draft_round,
            player_by_id[pid].draft_pick,
            grain=SummerLeagueDeskGrain.DEBUT,
        )
        for pid, is_debut in debut_status.items()
        if is_debut and pid in player_by_id
    }
    debut_baselines = await fetch_debut_baselines(
        db, baseline_version=baseline_version, cohort_keys=list(debut_cohort_keys)
    )

    prior_events = await fetch_prior_events(
        db, player_ids=player_ids, before_year=competition.year
    )
    current_gp_by_player = await fetch_current_event_gp(
        db, player_ids=player_ids, year=competition.year
    )

    # Streak's per-game bar/percentile ranks against the game-grain baseline
    # (#525), not the event-grain one -- a separate cohort-key map since a
    # player's game-grain key (`game:...`) differs from their event-grain key
    # (`slot:.../round:.../status:...`) even though both derive from the same
    # draft slot.
    game_cohort_key_by_player = {
        pid: cohort_key_for(
            player_by_id[pid].draft_round,
            player_by_id[pid].draft_pick,
            grain=SummerLeagueDeskGrain.GAME,
        )
        for pid in player_ids
        if pid in player_by_id
    }
    game_baselines = await fetch_game_baselines(
        db,
        baseline_version=baseline_version,
        cohort_keys=list(set(game_cohort_key_by_player.values())),
    )
    baseline_by_player = {
        pid: game_baselines.get(game_cohort_key_by_player[pid])
        for pid in player_ids
        if pid in game_cohort_key_by_player
    }
    game_lines_by_player = await fetch_game_lines(
        db,
        player_ids=player_ids,
        competition_id=competition_id,
        game_date=game_date,
        baseline_by_player=baseline_by_player,
    )

    field_entries = await fetch_tonight_field(
        db, competition_id=competition_id, game_date=game_date
    )
    field_value_by_player = {e.player_id: e.value for e in field_entries}

    fact_by_player: dict[int, list[Fact]] = {}
    for player_id, grade in grade_by_player.items():
        subject = FactSubject(
            player_id=player_id,
            player_label=label_by_id.get(player_id, f"Player {player_id}"),
            competition_id=competition_id,
        )
        facts: list[Fact] = [detect_percentile(subject=subject, grade=grade)]

        members = cohort_members_by_key.get(grade.cohort_key, [])
        baseline = event_baselines.get(grade.cohort_key)

        cohort_rank_fact = detect_cohort_rank(
            subject=subject,
            subject_value=grade.subject_value,
            metric="gmsc",
            cohort_key=grade.cohort_key,
            peers=cohort_peers(members, exclude_player_id=player_id),
            baseline_version=baseline_version,
        )
        if cohort_rank_fact is not None:
            facts.append(cohort_rank_fact)

        if baseline is not None:
            threshold = count_club_threshold(baseline)
            if threshold is not None:
                count_club_fact = detect_count_club(
                    subject=subject,
                    metric="gmsc",
                    cohort_key=grade.cohort_key,
                    subject_value=grade.subject_value,
                    threshold=threshold,
                    since_year=season_range_start(baseline.season_range),
                    other_members=club_members_clearing(
                        members, exclude_player_id=player_id, threshold=threshold
                    ),
                    baseline_version=baseline_version,
                )
                if count_club_fact is not None:
                    facts.append(count_club_fact)

                # `detect_first_since` always returns a Fact -- it's the
                # caller's job to only invoke it when the subject itself
                # clears the same qualifying bar (else it would read as a
                # superlative for an unremarkable performance).
                if grade.subject_value >= threshold:
                    prior_holder = most_recent_prior_holder(
                        members,
                        exclude_player_id=player_id,
                        threshold=threshold,
                        before_year=competition.year,
                    )
                    facts.append(
                        detect_first_since(
                            subject=subject,
                            metric="gmsc",
                            cohort_key=grade.cohort_key,
                            subject_value=grade.subject_value,
                            current_year=competition.year,
                            since_year=season_range_start(baseline.season_range),
                            most_recent_prior=prior_holder,
                            baseline_version=baseline_version,
                        )
                    )

        streak_fact = detect_streak(
            subject=subject,
            metric="gmsc",
            cohort_key=game_cohort_key_by_player.get(player_id, grade.cohort_key),
            games=game_lines_by_player.get(player_id, []),
            baseline_version=baseline_version,
        )
        if streak_fact is not None:
            facts.append(streak_fact)

        prior = prior_events.get(player_id)
        if prior is not None:
            self_delta_fact = detect_self_delta(
                subject=subject,
                metric="gmsc",
                cohort_key=grade.cohort_key,
                current_value=grade.subject_value,
                current_gp=current_gp_by_player.get(player_id, 0),
                prior=prior,
                baseline_version=baseline_version,
            )
            if self_delta_fact is not None:
                facts.append(self_delta_fact)

        if debut_status.get(player_id) and player_id in player_by_id:
            player = player_by_id[player_id]
            debut_key = cohort_key_for(
                player.draft_round, player.draft_pick, grain=SummerLeagueDeskGrain.DEBUT
            )
            debut_baseline = debut_baselines.get(debut_key)
            if debut_baseline is not None:
                facts.append(
                    detect_debut_vs_bar(
                        subject=subject,
                        metric="gmsc",
                        debut_cohort_key=debut_key,
                        subject_value=grade.subject_value,
                        debut_bar=debut_baseline.mean_value,
                        baseline_version=baseline_version,
                    )
                )

        subject_field_value = field_value_by_player.get(player_id)
        if subject_field_value is not None:
            leads_field_fact = detect_leads_field(
                subject=subject,
                subject_value=subject_field_value,
                metric="gmsc",
                field_label="tonight's slate",
                field=field_peers(field_entries, exclude_player_id=player_id),
            )
            if leads_field_fact is not None:
                facts.append(leads_field_fact)

        fact_by_player[player_id] = facts

    # One batched select + one batched update for every graded player's T2
    # row (#548) -- never a per-player `select`+`flush` inside this loop.
    await persist_grade_facts_bulk(
        db,
        competition_id=competition_id,
        baseline_version=baseline_version,
        facts_by_player=fact_by_player,
    )

    if not slate:
        return

    roster_by_game = await game_roster_player_ids(
        db, competition_id=competition_id, game_date=game_date
    )
    facts_by_game: dict[int, list[Fact]] = {
        slate_row.game_id: [
            fact
            for pid in roster_by_game.get(slate_row.game_id, [])
            for fact in fact_by_player.get(pid, [])
        ]
        for slate_row in slate
    }
    hero_game_ids = [slate_row.game_id for slate_row in slate if slate_row.is_hero]
    # One batched select + one batched update for every touched T4 slate row
    # (#548) -- never a per-game `select`+`flush` inside this loop.
    await persist_slate_facts_bulk(
        db, facts_by_game=facts_by_game, hero_game_ids=hero_game_ids
    )
