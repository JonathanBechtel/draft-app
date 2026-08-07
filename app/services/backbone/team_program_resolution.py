"""Franchise -> team_program natural-key resolution (journey-graph backbone, #796).

Promotes the franchise -> team_program natural key that
``scripts/populate_org_model_from_nba_teams.py`` (T3) introduced out of
``scripts/`` and into the shipped package, so ingest can populate
``summer_league_team_entries.nba_team_id``/``team_program_id`` at write time
instead of relying on a periodic operator backfill sweep -- the gap #796
closes (see ``docs/plans/summer-league-phase4-journey-graph-conversion-spec.md``
§5.1 decision D3 and ``docs/plans/global-player-journey-graph.md`` §7a).

``derive_org_slug`` is moved here verbatim -- byte-identical output is load
bearing, because every ``organizations``/``team_programs`` row T3 created, and
519 of 622 dev ``summer_league_team_entries`` rows, are already keyed on the
exact string this function produces.
``scripts/populate_org_model_from_nba_teams.py`` re-exports it from here
rather than keeping a second copy.

The franchise -> team_program map is keyed on ``team_programs.slug`` (unique
by DB constraint), never on ``organization_id``. The two ``scripts/backfill_*``
map builders this ticket does NOT touch (#799's job) key their map on
``organization_id`` via a dict comprehension with no ``ORDER BY`` -- when an
organization owns more than one ``team_program``, the comprehension silently
keeps whichever row the database happened to return last. This module's
:func:`build_franchise_team_program_map` instead raises
:class:`AmbiguousTeamProgramError` the moment that happens: per this repo's
entity-resolution rule, an ambiguous or unknown target resolves to ``NULL``,
never a guess.
"""

# discipline: file-size single natural-key resolver; splitting the map builder
# from the per-row resolver would separate two halves of one small contract

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, TeamProgram

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# derive_org_slug -- moved verbatim from scripts/populate_org_model_from_nba_teams.py (T3)
# ---------------------------------------------------------------------------

# Distinguishes the NBA-club namespace from a future non-NBA organization
# whose name-derived slug happens to collide (e.g. a college program also
# named "Lakers"). Population is closed to one row per franchise, so the
# same base slug is reused for the organization and its team_program --
# uniqueness is enforced independently by each table.
ORG_SLUG_PREFIX = "nba-"


def derive_org_slug(nba_team_slug: str) -> str:
    """Return the stable ``organizations.slug`` for one NBA franchise.

    Moved verbatim from ``scripts/populate_org_model_from_nba_teams.py`` (T3);
    the exact output string is load-bearing -- existing ``organizations`` and
    ``team_programs`` rows, and 519 of 622 dev ``summer_league_team_entries``
    rows, are already keyed on it. Do not change this format without a
    coordinated re-key of every dependent row.

    Args:
        nba_team_slug: The immutable ``nba_teams.slug`` value (e.g. ``"lakers"``).

    Returns:
        The natural key T3 and this module key idempotency on (e.g. ``"nba-lakers"``).
    """
    return f"{ORG_SLUG_PREFIX}{nba_team_slug}"


# ---------------------------------------------------------------------------
# Provider team id -> nba_teams.abbreviation
# ---------------------------------------------------------------------------

# Canonical stats.nba.com franchise team ids, keyed to the standard 3-letter
# abbreviation ``scripts/seed_nba_teams.py`` seeds ``nba_teams.abbreviation``
# with. This is the repo's single copy of that id set:
# ``app.services.sources.summer_league.team_logos.NBA_FRANCHISE_STATS_IDS``
# derives its frozenset from these keys rather than re-listing them, so the
# two can never drift. It lives here, not in the spoke, because import-linter
# contract 5 ("spoke independence") forbids ``app/services/backbone/`` from
# importing a Summer League module -- a spoke importing the backbone is the
# legal direction, so the shared constant must sit on this side of the edge.
# The id set is provider shape, not domain shape (north-star P3).
NBA_STATS_TEAM_ID_TO_ABBREVIATION: dict[str, str] = {
    "1610612737": "ATL",
    "1610612738": "BOS",
    "1610612739": "CLE",
    "1610612740": "NOP",
    "1610612741": "CHI",
    "1610612742": "DAL",
    "1610612743": "DEN",
    "1610612744": "GSW",
    "1610612745": "HOU",
    "1610612746": "LAC",
    "1610612747": "LAL",
    "1610612748": "MIA",
    "1610612749": "MIL",
    "1610612750": "MIN",
    "1610612751": "BKN",
    "1610612752": "NYK",
    "1610612753": "ORL",
    "1610612754": "IND",
    "1610612755": "PHI",
    "1610612756": "PHX",
    "1610612757": "POR",
    "1610612758": "SAC",
    "1610612759": "SAS",
    "1610612760": "OKC",
    "1610612761": "TOR",
    "1610612762": "UTA",
    "1610612763": "MEM",
    "1610612764": "WAS",
    "1610612765": "DET",
    "1610612766": "CHA",
}


# ---------------------------------------------------------------------------
# Franchise -> team_program map (the ambiguity guard)
# ---------------------------------------------------------------------------


class AmbiguousTeamProgramError(RuntimeError):
    """Raised when an organization owns more than one ``team_programs`` row.

    Per this repo's entity-resolution rule (ambiguous or unknown -> NULL,
    never a guess), a resolver that cannot tell which program represents a
    franchise must stop rather than pick one -- unlike the ``organization_id``
    dict comprehension in the ``scripts/backfill_*`` map builders this
    promotion replaces, which silently keeps whichever row the database
    happened to return last.
    """


@dataclass(frozen=True, slots=True)
class TeamProgramRow:
    """One ``team_programs`` row as read for franchise-map construction."""

    team_program_id: int
    team_program_slug: str
    organization_id: int


def build_franchise_team_program_map(
    program_rows: Iterable[TeamProgramRow],
) -> dict[str, int]:
    """Build ``{team_program_slug: team_program_id}`` from raw program rows.

    Pure and DB-free so the ambiguity guard is unit-testable without a
    database. Rows are grouped by ``organization_id``; an organization with
    more than one ``team_programs`` row is ambiguous -- which one represents
    the franchise? -- and this raises rather than keeping an arbitrary last
    row, the bug this promotion fixes relative to the two
    ``scripts/backfill_*`` map builders.

    Args:
        program_rows: Every ``team_programs`` row belonging to a resolvable
            franchise organization (typically every program owned by one or
            more NBA-franchise organizations).

    Returns:
        A map keyed on the unique ``team_programs.slug`` column -- never on
        ``organization_id``, which is exactly what let the old scripts stay
        silently wrong. Empty when ``program_rows`` is empty.

    Raises:
        AmbiguousTeamProgramError: If any ``organization_id`` appears on more
            than one row.
    """
    rows_by_org: dict[int, list[TeamProgramRow]] = {}
    for row in program_rows:
        rows_by_org.setdefault(row.organization_id, []).append(row)

    result: dict[str, int] = {}
    for organization_id, rows in rows_by_org.items():
        if len(rows) > 1:
            slugs = sorted(row.team_program_slug for row in rows)
            raise AmbiguousTeamProgramError(
                f"organization_id={organization_id} owns {len(rows)} "
                f"team_programs ({slugs!r}); cannot resolve a franchise to a "
                "single program unambiguously."
            )
        result[rows[0].team_program_slug] = rows[0].team_program_id
    return result


# ---------------------------------------------------------------------------
# Per-row resolution -- the ingest-time write path
# ---------------------------------------------------------------------------


async def _find_nba_team_by_abbreviation(
    db: AsyncSession, abbreviation: str
) -> NbaTeam | None:
    """Return the ``nba_teams`` row for a standard 3-letter abbreviation, if any."""
    result = await db.execute(
        select(NbaTeam).where(NbaTeam.abbreviation == abbreviation)  # type: ignore[arg-type]
    )
    return result.scalar_one_or_none()


async def _find_organization_id(db: AsyncSession, org_slug: str) -> int | None:
    """Return the id of the ``organizations`` row with ``org_slug``, if any."""
    return await db.scalar(select(Organization.id).where(Organization.slug == org_slug))  # type: ignore[call-overload,arg-type]


async def _find_team_program_rows(
    db: AsyncSession, organization_id: int
) -> list[TeamProgramRow]:
    """Return every ``team_programs`` row owned by ``organization_id``."""
    result = await db.execute(
        select(  # type: ignore[call-overload]
            TeamProgram.id, TeamProgram.slug, TeamProgram.organization_id
        ).where(TeamProgram.organization_id == organization_id)  # type: ignore[arg-type]
    )
    return [
        TeamProgramRow(
            team_program_id=program_id,
            team_program_slug=program_slug,
            organization_id=org_id,
        )
        for program_id, program_slug, org_id in result.all()
    ]


async def resolve_team_targets(
    db: AsyncSession,
    *,
    nba_stats_team_id: str | None,
) -> tuple[int | None, int | None]:
    """Resolve one Summer League source team to its dual-read targets.

    An unknown or ambiguous team resolves to ``NULL``, never a guess: a
    non-NBA / select squad with no franchise mapping (103 of 622 dev rows)
    is a *correct* NULL, not a failure, and this never invents a program for
    one.

    Args:
        db: Async database session (read-only; issues no writes).
        nba_stats_team_id: The provider team id from
            ``summer_league_team_entries.nba_stats_team_id`` (part of the
            table's unique constraint alongside ``competition_id``).

    Returns:
        ``(nba_team_id, team_program_id)``. Either or both are ``None`` when:
        the id is not a known NBA franchise stats id; ``nba_teams`` has no
        matching row yet (unseeded environment); the org-model population
        (T3) has not yet run for that franchise; or the franchise's
        organization owns more than one ``team_programs`` row. The last case
        is logged (:class:`AmbiguousTeamProgramError` is caught here, not
        raised) because a per-row ingest call must not abort the whole ingest
        run over one ambiguous organization -- bulk callers that want the
        loud failure should call :func:`build_franchise_team_program_map`
        directly instead.
    """
    abbreviation = (
        NBA_STATS_TEAM_ID_TO_ABBREVIATION.get(nba_stats_team_id)
        if nba_stats_team_id
        else None
    )
    if abbreviation is None:
        return (None, None)

    nba_team = await _find_nba_team_by_abbreviation(db, abbreviation)
    if nba_team is None or nba_team.id is None:
        return (None, None)

    org_slug = derive_org_slug(nba_team.slug)
    organization_id = await _find_organization_id(db, org_slug)
    if organization_id is None:
        return (nba_team.id, None)

    program_rows = await _find_team_program_rows(db, organization_id)
    if not program_rows:
        return (nba_team.id, None)

    try:
        franchise_map = build_franchise_team_program_map(program_rows)
    except AmbiguousTeamProgramError:
        logger.warning(
            "team_program_resolution.ambiguous_franchise "
            "organization_id=%s nba_stats_team_id=%s",
            organization_id,
            nba_stats_team_id,
        )
        return (nba_team.id, None)

    team_program_id = next(iter(franchise_map.values()), None)
    return (nba_team.id, team_program_id)


async def resolve_franchise_team_program_id(
    db: AsyncSession,
    *,
    nba_team_slug: str,
) -> int | None:
    """Resolve one franchise's ``team_programs.id`` from its ``nba_teams.slug``.

    The read-side counterpart to :func:`resolve_team_targets` (#795). Ingest
    starts from a provider team id and needs both targets; a *reader* already
    holds the canonical franchise row and needs only the program target, so it
    can hand both to
    ``app.services.player_affiliation.resolve_team_target_filter``. Both walk
    the same T3 natural key (:func:`derive_org_slug`) and share the same
    ambiguity guard, so the two sides can never disagree about which program
    represents a franchise.

    Issues exactly one query: the ``organizations`` -> ``team_programs`` join
    is resolved in the database rather than as two round trips, because this
    runs on a page render, not in a batch job.

    Args:
        db: Async database session (read-only; issues no writes).
        nba_team_slug: The immutable ``nba_teams.slug`` (e.g. ``"lakers"``).

    Returns:
        The franchise's ``team_programs.id``, or ``None`` when the org model
        has not been populated for it (T3 not yet run for this franchise) or
        when its organization owns more than one program. Ambiguity is logged
        and resolved to ``None`` rather than raised: a franchise page must
        still render off the legacy ``nba_team_id`` target, and per this
        repo's entity-resolution rule an ambiguous target is refused, never
        guessed.
    """
    org_slug = derive_org_slug(nba_team_slug)
    result = await db.execute(
        select(  # type: ignore[call-overload]
            TeamProgram.id, TeamProgram.slug, TeamProgram.organization_id
        )
        .join(Organization, Organization.id == TeamProgram.organization_id)  # type: ignore[arg-type]
        .where(Organization.slug == org_slug)  # type: ignore[arg-type]
    )
    program_rows = [
        TeamProgramRow(
            team_program_id=program_id,
            team_program_slug=program_slug,
            organization_id=org_id,
        )
        for program_id, program_slug, org_id in result.all()
    ]
    if not program_rows:
        return None

    try:
        franchise_map = build_franchise_team_program_map(program_rows)
    except AmbiguousTeamProgramError:
        logger.warning(
            "team_program_resolution.ambiguous_franchise nba_team_slug=%s org_slug=%s",
            nba_team_slug,
            org_slug,
        )
        return None
    return next(iter(franchise_map.values()), None)
