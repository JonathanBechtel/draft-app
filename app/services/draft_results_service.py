"""Read-layer for the draft-recap surfaces.

Joins actual outcomes (``draft_results``) against the latest pre-draft
consensus snapshot (``big_board_consensus``) to produce the pick-by-pick recap,
the steals/reaches leaderboards, and the per-source accuracy scoring that
powers the two draft-recap pages.

The write path (ingesting actual picks) lives in
``scripts/ingest_draft_results.py``; this module only reads.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.draft_results import (
    DepthBucket,
    RecapPick,
    RecapSummary,
    SourceAccuracyRow,
)
from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.consensus import BigBoardConsensus
from app.schemas.draft_results import DraftResult
from app.schemas.nba_teams import NbaTeam
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services.consensus_read_service import (
    _alignment_score,
    _player_status_map,
    _resolve_snapshot_id,
)
from app.services.consensus_service import _spearman
from app.services.image_assets_service import get_current_image_urls_for_players

# Pick-range buckets for the accuracy-by-depth chart: (label, lo, hi inclusive).
_DEPTH_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("Top 5", 1, 5),
    ("Lottery", 6, 14),
    ("Mid 1st", 15, 30),
    ("2nd round", 31, 60),
)

# Half-width of the fallback consensus band when a player has no high/low
# spread (single-source rank): picks within this many slots read as in-range.
_RANGE_FALLBACK_PAD = 3

_PHOTO_STYLE = "default"


async def _latest_consensus_map(
    db: AsyncSession, *, draft_year: int
) -> dict[int, BigBoardConsensus]:
    """Return ``player_id -> BigBoardConsensus`` for the latest snapshot.

    Empty when no consensus snapshot exists for the year — the recap then
    renders every pick as "unranked" rather than erroring.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return {}
    rows = (
        (
            await db.execute(
                select(BigBoardConsensus).where(  # type: ignore[call-overload]
                    BigBoardConsensus.snapshot_id == sid  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.player_id: r for r in rows}


async def has_draft_results(db: AsyncSession, *, draft_year: int) -> bool:
    """Return whether any actual picks have been recorded for the year.

    Drives the post-draft handoff banner on the consensus page (the live board
    flips to pointing at the recap once results start landing).
    """
    found = await db.scalar(
        select(DraftResult.id)  # type: ignore[call-overload]
        .where(DraftResult.draft_year == draft_year)  # type: ignore[arg-type]
        .limit(1)
    )
    return found is not None


async def get_recap_years(db: AsyncSession) -> list[int]:
    """Return draft years that have recorded results, newest first.

    Powers the year switcher / archive: ``/draft-recap`` resolves to the most
    recent of these, and each year is reachable at ``/draft-recap/<year>``.
    """
    rows = (
        (
            await db.execute(
                select(DraftResult.draft_year)  # type: ignore[call-overload]
                .distinct()
                .order_by(DraftResult.draft_year.desc())  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )
    return [int(y) for y in rows]


def _classify_range(
    pick: int,
    consensus_rank: Optional[int],
    high_rank: Optional[int],
    low_rank: Optional[int],
) -> tuple[str, Optional[int]]:
    """Classify a pick by where it landed relative to the consensus range.

    Returns ``(classification, range_surprise)`` where ``range_surprise`` is the
    signed distance outside the [best, worst] projection band (0 within range,
    positive when drafted later than the worst projection, negative when earlier
    than the best). Neutral framing — direction only, no value judgement.

    Falls back to a ±5 band around the point ``consensus_rank`` when the range
    is unavailable, so a player ranked by a single source still classifies.
    """
    if consensus_rank is None:
        return "unranked", None
    hi = high_rank if high_rank is not None else consensus_rank
    lo = low_rank if low_rank is not None else consensus_rank
    # Widen a degenerate (single-source) band so exact-rank picks read in-range.
    if hi == lo:
        hi = max(1, consensus_rank - _RANGE_FALLBACK_PAD)
        lo = consensus_rank + _RANGE_FALLBACK_PAD
    if pick < hi:
        return "earlier", pick - hi
    if pick > lo:
        return "later", pick - lo
    return "in_range", 0


async def _recap_picks(db: AsyncSession, *, draft_year: int) -> list[RecapPick]:
    """Build the ordered pick-by-pick recap rows for a draft year."""
    results = (
        (
            await db.execute(
                select(DraftResult)  # type: ignore[call-overload]
                .where(DraftResult.draft_year == draft_year)  # type: ignore[arg-type]
                .order_by(DraftResult.overall_pick)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    if not results:
        return []

    player_ids = [r.player_id for r in results if r.player_id is not None]
    team_ids = [r.team_id for r in results if r.team_id is not None]

    players = await _player_map(db, player_ids)
    teams = await _team_map(db, team_ids)
    status = await _player_status_map(db, player_ids)
    photos = (
        await get_current_image_urls_for_players(
            db, player_ids=player_ids, style=_PHOTO_STYLE
        )
        if player_ids
        else {}
    )
    consensus = await _latest_consensus_map(db, draft_year=draft_year)

    picks: list[RecapPick] = []
    for r in results:
        player = players.get(r.player_id) if r.player_id is not None else None
        team = teams.get(r.team_id) if r.team_id is not None else None
        bbc = consensus.get(r.player_id) if r.player_id is not None else None
        st = status.get(r.player_id, {}) if r.player_id is not None else {}

        cr = bbc.consensus_rank if bbc else None
        delta = r.overall_pick - cr if cr is not None else None
        classification, range_surprise = _classify_range(
            r.overall_pick,
            cr,
            bbc.high_rank if bbc else None,
            bbc.low_rank if bbc else None,
        )
        picks.append(
            RecapPick(
                overall_pick=r.overall_pick,
                round=r.round,
                round_pick=r.round_pick,
                team_name=team.name if team else None,
                team_abbreviation=team.abbreviation if team else None,
                team_slug=team.slug if team else None,
                team_logo_url=team.logo_url if team else None,
                team_primary_color=team.primary_color if team else None,
                player_id=r.player_id,
                player_name=player.display_name if player else None,
                raw_player_name=r.raw_player_name,
                slug=player.slug if player else None,
                school=player.school if player else None,
                photo_url=photos.get(r.player_id) if r.player_id else None,
                position=st.get("position"),
                height=st.get("height"),
                weight=st.get("weight"),
                consensus_rank=cr,
                num_sources=bbc.num_sources if bbc else None,
                high_rank=bbc.high_rank if bbc else None,
                low_rank=bbc.low_rank if bbc else None,
                delta=delta,
                range_surprise=range_surprise,
                classification=classification,
            )
        )
    return picks


async def get_draft_recap(
    db: AsyncSession, *, draft_year: int
) -> tuple[list[RecapPick], RecapSummary]:
    """Return the ordered pick-by-pick recap plus headline summary numbers.

    Picks are ordered by ``overall_pick``. The summary surfaces the furthest
    slide (drafted later than the field's range) and jump (earlier), plus the
    neutral "chalk" stat — how many picks landed within their consensus range.
    """
    picks = await _recap_picks(db, draft_year=draft_year)
    later, earlier = split_movers(picks, limit=1)
    ranked = [p for p in picks if p.consensus_rank is not None]
    in_range = sum(1 for p in ranked if p.classification == "in_range")

    summary = RecapSummary(
        draft_year=draft_year,
        num_picks=len(picks),
        num_ranked=len(ranked),
        num_unranked=len(picks) - len(ranked),
        num_in_range=in_range,
        pct_in_range=round(100 * in_range / len(ranked)) if ranked else None,
        biggest_later=later[0] if later else None,
        biggest_earlier=earlier[0] if earlier else None,
    )
    return picks, summary


def split_movers(
    picks: list[RecapPick], *, limit: int = 10
) -> tuple[list[RecapPick], list[RecapPick]]:
    """Split recap picks into ``(later, earlier)`` movers vs. the consensus range.

    Pure helper so callers holding the picks list don't rebuild it. ``later`` =
    drafted further past the field's worst projection (largest positive
    ``range_surprise``); ``earlier`` = drafted further ahead of the best
    projection (most negative). Neutral framing — direction, not value.
    """
    movers = [p for p in picks if p.range_surprise]
    later = sorted(
        (p for p in movers if (p.range_surprise or 0) > 0),
        key=lambda p: p.range_surprise or 0,
        reverse=True,
    )[:limit]
    earlier = sorted(
        (p for p in movers if (p.range_surprise or 0) < 0),
        key=lambda p: p.range_surprise or 0,
    )[:limit]
    return later, earlier


async def get_movers(
    db: AsyncSession, *, draft_year: int, limit: int = 10
) -> tuple[list[RecapPick], list[RecapPick]]:
    """Return ``(later, earlier)`` mover leaderboards for the recap page."""
    picks = await _recap_picks(db, draft_year=draft_year)
    return split_movers(picks, limit=limit)


def depth_buckets(picks: list[RecapPick]) -> list[DepthBucket]:
    """Predictability by draft range — share of picks within their consensus range.

    Shows where the field agrees (the top) and where projections spread out
    (deeper in the draft). Buckets with no ranked picks are omitted, so tonight's
    first round renders before the second round fills in tomorrow.
    """
    out: list[DepthBucket] = []
    for label, lo, hi in _DEPTH_BUCKETS:
        ranked = [
            p
            for p in picks
            if p.consensus_rank is not None and lo <= p.overall_pick <= hi
        ]
        if not ranked:
            continue
        inside = sum(1 for p in ranked if p.classification == "in_range")
        out.append(
            DepthBucket(
                label=label,
                range_text=f"picks {lo}–{hi}",
                pct=round(100 * inside / len(ranked)),
                num_picks=len(ranked),
            )
        )
    return out


def _score_predictions(
    predicted: dict[int, int],
    actual: dict[int, int],
    first_overall_player: Optional[int],
) -> Optional[dict[str, object]]:
    """Score one set of player→predicted-slot guesses against actual picks.

    ``predicted`` and ``actual`` both map player_id → slot. Returns the metric
    bundle (order-match, MAE, exact/within-3/within-5, nailed #1) over the
    players they share, or ``None`` when they share nothing.
    """
    pairs: list[tuple[float, float]] = []
    errors: list[int] = []
    exact = within3 = within5 = 0
    for pid, pred_slot in predicted.items():
        actual_pick = actual.get(pid)
        if actual_pick is None:
            continue
        pairs.append((pred_slot, actual_pick))
        err = abs(pred_slot - actual_pick)
        errors.append(err)
        exact += err == 0
        within3 += err <= 3
        within5 += err <= 5
    if not errors:
        return None
    nailed_first = (
        first_overall_player is not None and predicted.get(first_overall_player) == 1
    )
    return {
        "num_shared": len(errors),
        "order_match": _alignment_score(_spearman(pairs)),
        "mean_abs_error": round(sum(errors) / len(errors), 2),
        "exact_hits": exact,
        "within_three": within3,
        "within_five": within5,
        "nailed_first_overall": nailed_first,
    }


async def get_source_accuracy(
    db: AsyncSession,
    *,
    draft_year: int,
    min_shared: int = 5,
    include_consensus: bool = True,
) -> list[SourceAccuracyRow]:
    """Rank each source's latest approved board against the actual results.

    Each source's most recent APPROVED board is scored by how well its order
    matched what actually happened (Spearman order-match, the headline metric),
    plus supporting hit rates. When ``include_consensus`` is set, the DraftGuru
    blended consensus is scored the same way and folded into the ranking so the
    page can show whether the crowd beat the individual analysts.

    Returns rows sorted by order-match (most accurate first), keeping sources
    that shared at least ``min_shared`` drafted players so the metric is
    meaningful. The consensus row is kept at a lower threshold (3) since it is
    the reference blend.
    """
    actual = {
        pid: pick
        for pid, pick in (
            (r.player_id, r.overall_pick)
            for r in (
                await db.execute(
                    select(DraftResult).where(  # type: ignore[call-overload]
                        DraftResult.draft_year == draft_year  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )
        if pid is not None
    }
    if not actual:
        return []
    first_overall_player = next(
        (pid for pid, pick in actual.items() if pick == 1), None
    )

    boards = (
        (
            await db.execute(
                select(Board)  # type: ignore[call-overload]
                .where(Board.draft_year == draft_year)  # type: ignore[arg-type]
                .where(Board.status == BoardStatus.APPROVED)  # type: ignore[arg-type]
                .order_by(Board.published_at.desc())  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )
    # Keep the most recent approved board per source.
    latest_by_source: dict[int, Board] = {}
    for b in boards:
        latest_by_source.setdefault(b.news_source_id, b)

    sources = await _source_map(db, list(latest_by_source.keys()))

    rows: list[SourceAccuracyRow] = []
    for source_id, board in latest_by_source.items():
        entries = (
            (
                await db.execute(
                    select(BoardEntry).where(  # type: ignore[call-overload]
                        BoardEntry.board_id == board.id  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )
        predicted = {
            e.player_id: e.position for e in entries if e.player_id is not None
        }
        metrics = _score_predictions(predicted, actual, first_overall_player)
        if metrics is None or int(metrics["num_shared"]) < min_shared:  # type: ignore[call-overload]
            continue
        src = sources.get(source_id)
        rows.append(
            SourceAccuracyRow(
                news_source_id=source_id,
                source_name=src.name if src else f"source-{source_id}",
                source_display_name=(
                    src.display_name if src else f"Source {source_id}"
                ),
                board_kind=board.kind.value,
                **metrics,  # type: ignore[arg-type]
            )
        )

    if include_consensus:
        consensus_map = await _latest_consensus_map(db, draft_year=draft_year)
        predicted_c = {pid: bbc.consensus_rank for pid, bbc in consensus_map.items()}
        metrics_c = _score_predictions(predicted_c, actual, first_overall_player)
        if metrics_c is not None and int(metrics_c["num_shared"]) >= 3:  # type: ignore[call-overload]
            rows.append(
                SourceAccuracyRow(
                    news_source_id=0,
                    source_name="draftguru-consensus",
                    source_display_name="DraftGuru Consensus",
                    board_kind="CONSENSUS",
                    is_consensus=True,
                    **metrics_c,  # type: ignore[arg-type]
                )
            )

    # Sort by order-match (headline) desc; None last; tie-break by lower MAE.
    rows.sort(
        key=lambda r: (
            r.order_match is None,
            -(r.order_match or 0),
            r.mean_abs_error,
        )
    )
    return rows


async def _player_map(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, PlayerMaster]:
    """Return a ``player_id -> PlayerMaster`` map for a batch of ids."""
    if not player_ids:
        return {}
    rows = (
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
    return {p.id: p for p in rows if p.id is not None}


async def _team_map(db: AsyncSession, team_ids: list[int]) -> dict[int, NbaTeam]:
    """Return a ``team_id -> NbaTeam`` map for a batch of ids."""
    if not team_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(NbaTeam).where(  # type: ignore[call-overload]
                    NbaTeam.id.in_(team_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    return {t.id: t for t in rows if t.id is not None}


async def _source_map(db: AsyncSession, source_ids: list[int]) -> dict[int, NewsSource]:
    """Return a ``news_source_id -> NewsSource`` map for a batch of ids."""
    if not source_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(NewsSource).where(  # type: ignore[call-overload]
                    NewsSource.id.in_(source_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    return {s.id: s for s in rows if s.id is not None}
