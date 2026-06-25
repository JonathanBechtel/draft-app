"""Pydantic response models for the draft-recap read surface.

These shape the join of actual draft outcomes (``draft_results``) against the
pre-draft consensus (``big_board_consensus``).

Framing is deliberately neutral: we report *where a player went versus where
the field expected him*, not whether the pick was good. ``delta`` is the point
gap (``overall_pick - consensus_rank``); ``classification`` buckets a pick by
where it landed relative to the consensus *range* (the best/worst rank any
source gave) — ``"earlier"`` (ahead of the whole range), ``"later"`` (past it),
``"in_range"`` (within what the field expected), or ``"unranked"``.
"""

from typing import Optional

from sqlmodel import SQLModel


class RecapPick(SQLModel):
    """One actual pick, annotated with how it compared to consensus.

    ``consensus_rank`` / ``delta`` / ``classification`` are ``None`` /
    ``"unranked"`` when the drafted player was not on the consensus board.
    """

    overall_pick: int
    round: int
    round_pick: int

    # Selecting team (None when the team token could not be resolved).
    team_name: Optional[str] = None
    team_abbreviation: Optional[str] = None
    team_slug: Optional[str] = None
    team_logo_url: Optional[str] = None
    team_primary_color: Optional[str] = None

    # Drafted player. player_id/name are None only for an unresolved pick.
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    raw_player_name: str = ""
    slug: Optional[str] = None
    school: Optional[str] = None
    photo_url: Optional[str] = None
    position: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[int] = None

    # Pre-draft consensus signal (None when the player was unranked).
    consensus_rank: Optional[int] = None
    num_sources: Optional[int] = None
    high_rank: Optional[int] = None  # best (lowest) rank any source gave
    low_rank: Optional[int] = None  # worst (highest) rank any source gave
    # overall_pick - consensus_rank (point gap; positive = drafted later).
    delta: Optional[int] = None
    # Signed distance OUTSIDE the consensus range: 0 when within range,
    # positive when drafted later than the worst projection, negative when
    # earlier than the best. None when unranked. Drives the neutral "chalk" stat
    # (how many picks landed within the field's whole spread) and the
    # predictability-by-tier bars — NOT the rise/fall direction.
    range_surprise: Optional[int] = None
    # Range-based bucket for the chalk/predictability stats:
    # "earlier" | "later" | "in_range" | "unranked".
    classification: str = "unranked"
    # Delta-based rise/fall direction — the intuitive point gap, not the range
    # band. Drives the table arrows/colour, scatter rings, and movers boards:
    # "earlier" (rose), "later" (fell), "even" (on the number), "unranked".
    direction: str = "unranked"
    # Gradient intensity 0..1 scaled to |delta|; 0 for even/unranked. Powers the
    # smooth colour ramp on the pick-by-pick board (bigger move = stronger tint).
    delta_shade: float = 0.0


class RecapSummary(SQLModel):
    """Headline numbers for the recap page header / share card."""

    draft_year: int
    num_picks: int
    num_ranked: int
    num_unranked: int
    # Picks that landed within their consensus range (the neutral "chalk" stat).
    num_in_range: int = 0
    pct_in_range: Optional[int] = None
    # Furthest-later and furthest-earlier surprises vs. the consensus range.
    biggest_later: Optional[RecapPick] = None
    biggest_earlier: Optional[RecapPick] = None


class SourceAccuracyRow(SQLModel):
    """How well one source's pre-draft board predicted the actual order."""

    news_source_id: int
    source_name: str
    source_display_name: str
    board_kind: str
    # Players appearing on both the board and the actual results.
    num_shared: int
    # Headline metric: Spearman order-match between predicted and actual order,
    # mapped to 0-100 ((rho+1)/2*100). None when fewer than 3 shared players.
    order_match: Optional[int] = None
    # Mean absolute |board_position - overall_pick| over shared players.
    mean_abs_error: float
    exact_hits: int
    within_three: int
    within_five: int = 0
    # Did this source nail the #1 overall pick?
    nailed_first_overall: bool = False
    # True for the synthetic "DraftGuru Consensus" row in the leaderboard.
    is_consensus: bool = False


class DepthBucket(SQLModel):
    """Predictability of one slice of the draft (e.g. lottery, 2nd round).

    ``pct`` is the share of ranked picks in the range that landed within their
    consensus range — high near the top (the field agrees), lower deeper in the
    draft where projections spread out.
    """

    label: str
    range_text: str
    pct: int  # 0-100
    num_picks: int
