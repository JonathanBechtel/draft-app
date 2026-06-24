"""Unit tests for the draft-recap chart builders and pure recap helpers.

These exercise the server-side face scatter, the predictability buckets, and
the slid/jumped mover split without a database — they operate on RecapPick
objects built in-memory.
"""

from __future__ import annotations

from typing import Optional

from app.models.draft_results import RecapPick
from app.services.draft_results_service import (
    _classify_range,
    depth_buckets,
    split_movers,
)
from app.utils.recap_charts import bar_width_pct, build_recap_scatter_svg


def _pick(
    pick: int,
    rank: Optional[int],
    *,
    high: Optional[int] = None,
    low: Optional[int] = None,
    photo: Optional[str] = None,
) -> RecapPick:
    """Build a RecapPick with range classification computed like the service."""
    classification, surprise = _classify_range(pick, rank, high, low)
    return RecapPick(
        overall_pick=pick,
        round=1 if pick <= 30 else 2,
        round_pick=pick if pick <= 30 else pick - 30,
        player_name=f"Player {pick}",
        raw_player_name=f"Player {pick}",
        photo_url=photo,
        consensus_rank=rank,
        high_rank=high,
        low_rank=low,
        delta=(pick - rank) if rank is not None else None,
        range_surprise=surprise,
        classification=classification,
    )


def test_classify_range_buckets() -> None:
    """A pick is earlier/later/in_range by where it falls in the [high,low] band."""
    assert _classify_range(3, 8, 6, 12) == ("earlier", -3)  # before the band
    assert _classify_range(20, 8, 6, 12) == ("later", 8)  # past the band
    assert _classify_range(9, 8, 6, 12) == ("in_range", 0)  # inside the band
    assert _classify_range(5, None, None, None) == ("unranked", None)


def test_scatter_plots_a_face_per_ranked_player() -> None:
    """Every ranked pick is a face (riser/faller/even); unranked excluded."""
    picks = [
        _pick(1, 1, high=1, low=3),  # in range -> even face
        _pick(12, 3, high=2, low=5),  # faller
        _pick(4, 20, high=15, low=25),  # riser
        _pick(16, None),  # unranked -> excluded
    ]
    svg = build_recap_scatter_svg(picks)
    assert svg.startswith("<svg")
    assert svg.count('<g class="recap-scatter__face') == 3  # one per ranked pick
    assert "recap-scatter__face--faller" in svg
    assert "recap-scatter__face--riser" in svg
    assert "recap-scatter__face--even" in svg
    # Hover data attributes are present for the JS card.
    assert 'data-name="Player 12"' in svg
    assert 'data-dir="faller"' in svg
    assert 'data-exp="3"' in svg and 'data-act="12"' in svg


def test_scatter_uses_photo_image_when_present() -> None:
    """A face with a photo renders a clipped <image>, escaped href + name."""
    p = _pick(12, 3, high=2, low=5, photo="https://cdn.test/a.png?x=1&y=2")
    p.player_name = "A <b> & C"
    svg = build_recap_scatter_svg([p])
    assert "<image" in svg
    assert "x=1&amp;y=2" in svg  # href ampersand escaped
    assert "&lt;b&gt;" in svg and "<b>" not in svg  # name escaped
    assert "data-name=" in svg


def test_scatter_empty_without_ranked_picks() -> None:
    """No ranked picks -> empty string (template skips the section)."""
    assert build_recap_scatter_svg([_pick(5, None)]) == ""


def test_depth_buckets_share_in_range() -> None:
    """Depth buckets report the share of in-range picks per draft range."""
    # Top 5: two in-range, one earlier -> 2/3 within range.
    picks = [
        _pick(1, 1, high=1, low=5),
        _pick(2, 2, high=1, low=5),
        _pick(3, 11, high=8, low=14),  # earlier -> outside range
    ]
    buckets = depth_buckets(picks)
    top = next(b for b in buckets if b.label == "Top 5")
    assert top.num_picks == 3
    assert top.pct == 67


def test_split_movers_orders_and_caps() -> None:
    """Later sorts by descending surprise, earlier ascending; limit respected."""
    picks = [
        _pick(20, 3, high=2, low=5),  # later, surprise +15
        _pick(12, 3, high=2, low=8),  # later, surprise +4
        _pick(3, 12, high=9, low=15),  # earlier, surprise -6
        _pick(1, 1, high=1, low=3),  # in range
    ]
    later, earlier = split_movers(picks, limit=1)
    assert len(later) == 1 and later[0].overall_pick == 20
    assert len(earlier) == 1 and earlier[0].overall_pick == 3


def test_bar_width_pct_clamps() -> None:
    """bar_width_pct clamps to 0..100 and handles None."""
    assert bar_width_pct(None) == 0.0
    assert bar_width_pct(150) == 100.0
    assert bar_width_pct(50) == 50.0
