"""Server-side SVG builders for the draft-recap visuals.

Rendering charts as SVG strings on the server (rather than client-side JS)
keeps them in the initial HTML, so they screenshot reliably for sharing. The
scatter carries per-point ``data-*`` attributes so a small amount of JS can show
an interactive hover card without re-fetching anything.
"""

from __future__ import annotations

import html
from typing import Optional

from app.models.draft_results import RecapPick

# Plot geometry (SVG user units). Square viewBox with a margin for axis labels
# on the left and bottom and room for face thumbnails near the edges.
_VB = 380
_X0, _X1 = 60.0, 352.0  # left/right of plot area
_Y0, _Y1 = 24.0, 312.0  # top/bottom of plot area

_FACE_R = 11.5  # radius of a player-face thumbnail

# Direction ring + hover label per delta direction. "earlier" (drafted ahead of
# the consensus rank) reads as a riser; "later" as a faller; "even" sat on it.
_DIR = {
    "earlier": ("recap-scatter__face--riser", "riser"),
    "later": ("recap-scatter__face--faller", "faller"),
    "even": ("recap-scatter__face--even", "in range"),
    "unranked": ("recap-scatter__face--even", "unranked"),
}
# Movers draw last so their rings sit above the on-the-line crowd.
_DRAW_ORDER = {"even": 0, "earlier": 1, "later": 1, "unranked": 0}


def _nice_ticks(max_n: int) -> list[int]:
    """Return axis tick values (1, then multiples of 10) up to ``max_n``."""
    ticks = [1]
    step = 10 if max_n <= 60 else 20
    v = step
    while v <= max_n:
        ticks.append(v)
        v += step
    return ticks


def build_recap_scatter_svg(picks: list[RecapPick]) -> str:
    """Build the ranked-vs-drafted scatter as an SVG string, a face per player.

    X = consensus rank, Y = actual pick, with a 45° reference line. *Every*
    ranked player is plotted as a circular photo, ringed by direction (riser =
    drafted ahead of the consensus range, faller = past it, neutral = within).
    Each face carries ``data-*`` attributes for the JS hover card. Returns an
    empty string when there are no ranked picks to plot.
    """
    pts = [p for p in picks if p.consensus_rank is not None]
    if not pts:
        return ""

    max_n = max(max(p.consensus_rank or 1, p.overall_pick) for p in pts)
    max_n = max(max_n, 2)
    span = max_n - 1

    def x_of(rank: int) -> float:
        return _X0 + (rank - 1) / span * (_X1 - _X0)

    def y_of(pick: int) -> float:
        return _Y0 + (pick - 1) / span * (_Y1 - _Y0)

    parts: list[str] = [
        f'<svg class="recap-scatter" viewBox="0 0 {_VB} {_VB}" '
        f'role="img" aria-label="Consensus rank versus actual draft pick">'
    ]
    # Axes + 45° reference (drafted exactly where ranked).
    parts.append(
        f'<line class="recap-scatter__axis" x1="{_X0}" y1="{_Y0}" x2="{_X0}" y2="{_Y1}" />'
        f'<line class="recap-scatter__axis" x1="{_X0}" y1="{_Y1}" x2="{_X1}" y2="{_Y1}" />'
        f'<line class="recap-scatter__diag" x1="{_X0}" y1="{_Y0}" x2="{_X1}" y2="{_Y1}" />'
    )
    # Ticks.
    for t in _nice_ticks(max_n):
        tx, ty = x_of(t), y_of(t)
        parts.append(
            f'<text class="recap-scatter__tick" x="{tx:.1f}" y="{_Y1 + 14:.1f}" '
            f'text-anchor="middle">{t}</text>'
            f'<text class="recap-scatter__tick" x="{_X0 - 9:.1f}" y="{ty + 3:.1f}" '
            f'text-anchor="end">{t}</text>'
        )
    # Captions.
    parts.append(
        f'<text class="recap-scatter__caption" x="{(_X0 + _X1) / 2:.1f}" y="{_VB - 5}" '
        f'text-anchor="middle">Consensus rank →</text>'
        f'<text class="recap-scatter__caption" x="15" y="{(_Y0 + _Y1) / 2:.1f}" '
        f'text-anchor="middle" transform="rotate(-90 15 {(_Y0 + _Y1) / 2:.1f})">'
        f"Actual pick →</text>"
    )

    # A face per player; in-range first, movers last so their rings sit on top.
    ordered = sorted(enumerate(pts), key=lambda ip: _DRAW_ORDER.get(ip[1].direction, 0))
    for i, p in ordered:
        parts.append(_face(p, i, x_of(p.consensus_rank or 1), y_of(p.overall_pick)))

    parts.append("</svg>")
    return "".join(parts)


def _face(p: RecapPick, idx: int, cx: float, cy: float) -> str:
    """Render one player's face marker (photo or avatar) with hover data."""
    ring_cls, direction = _DIR.get(p.direction, _DIR["even"])
    name = p.player_name or p.raw_player_name or "Unknown"
    attrs = (
        f'data-name="{html.escape(name, quote=True)}" '
        f'data-pos="{html.escape(p.position or "", quote=True)}" '
        f'data-exp="{p.consensus_rank}" data-act="{p.overall_pick}" '
        f'data-dir="{direction}"'
    )
    out = [
        f'<g class="recap-scatter__face {ring_cls}" {attrs}>'
        f"<title>{html.escape(name)} — expected #{p.consensus_rank}, "
        f"drafted #{p.overall_pick}</title>"
        f'<circle class="recap-scatter__face-bg" cx="{cx:.1f}" cy="{cy:.1f}" '
        f'r="{_FACE_R + 1.3:.1f}" />'
    ]
    if p.photo_url:
        cid = f"rcf{idx}"
        out.append(
            f'<clipPath id="{cid}"><circle cx="{cx:.1f}" cy="{cy:.1f}" '
            f'r="{_FACE_R:.1f}" /></clipPath>'
            f'<image href="{html.escape(p.photo_url, quote=True)}" '
            f'x="{cx - _FACE_R:.1f}" y="{cy - _FACE_R:.1f}" '
            f'width="{2 * _FACE_R:.1f}" height="{2 * _FACE_R:.1f}" '
            f'clip-path="url(#{cid})" preserveAspectRatio="xMidYMid slice" />'
        )
    else:
        initial = html.escape(name[:1])
        out.append(
            f'<circle class="recap-scatter__face-blank" cx="{cx:.1f}" cy="{cy:.1f}" '
            f'r="{_FACE_R:.1f}" />'
            f'<text class="recap-scatter__face-initial" x="{cx:.1f}" y="{cy + 3.5:.1f}" '
            f'text-anchor="middle">{initial}</text>'
        )
    out.append(
        f'<circle class="recap-scatter__face-ring" cx="{cx:.1f}" cy="{cy:.1f}" '
        f'r="{_FACE_R:.1f}" fill="none" /></g>'
    )
    return "".join(out)


def bar_width_pct(value: Optional[int], *, out_of: int = 100) -> float:
    """Clamp a 0..out_of value to a 0–100 percent for a CSS bar width."""
    if value is None:
        return 0.0
    return max(0.0, min(100.0, value / out_of * 100.0))
