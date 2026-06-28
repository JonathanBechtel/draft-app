"""Render a Summer League shot heat map to a base64 PNG for share cards.

Mirrors the on-page kernel-smoothed heat component
(``app/static/summer-league-shotchart.js``): a Gaussian kernel is sampled over a
fine grid so colour varies smoothly across the floor. Colour = efficiency vs the
SL pool (diverging red→cream→green) when a pool baseline exists, else a
sequential FG% scale; opacity = shot density. Court lines are drawn in the SVG
template on top of this image, so this renders only the translucent heat field.

resvg (the card rasteriser) can embed raster ``<image>`` data URIs, but cannot
run JS/canvas — hence the Python reimplementation here.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

# Court coordinate system (1 unit = 1 tenth-foot), matching the JS component.
HOOP_X = 250.0
HOOP_Y = 418.0
VB_W = 500
CROP_TOP = 95
CROP_H = 375  # baseline (470) → just above the arc (95)

SIGMA = 28.0
GW = 160
GH = int(round(GW * CROP_H / VB_W))
OUT_W = 1000
OUT_H = int(round(OUT_W * CROP_H / VB_W))
MAX_DELTA = 0.10

# Colour ramps (RGB), identical to the JS component.
C_COLD = np.array([225, 29, 72], float)
C_MID = np.array([238, 232, 220], float)
C_HOT = np.array([16, 185, 129], float)
SEQ = [
    np.array([37, 99, 235], float),
    np.array([56, 189, 248], float),
    np.array([250, 204, 21], float),
    np.array([239, 68, 68], float),
]

EXCLUDED = {"Backcourt"}


@dataclass
class _Dot:
    loc_x: float
    loc_y: float
    made: bool


def _classify_zone(x: float, y: float) -> str:
    dist = (x * x + y * y) ** 0.5
    if abs(x) >= 220 and y <= 92:
        return "Left Corner 3" if x < 0 else "Right Corner 3"
    if dist >= 237.5:
        return "Above the Break 3"
    if dist <= 40:
        return "Restricted Area"
    if abs(x) <= 80 and y <= 190:
        return "In The Paint (Non-RA)"
    return "Mid-Range"


def _seq_color(fg: np.ndarray) -> np.ndarray:
    """Vectorised sequential ramp blue→cyan→amber→red over fg∈[0,0.65]."""
    t = np.clip(fg / 0.65, 0.0, 1.0)
    out = np.zeros(t.shape + (3,), float)
    seg = [
        (0.0, 0.34, SEQ[0], SEQ[1]),
        (0.34, 0.67, SEQ[1], SEQ[2]),
        (0.67, 1.01, SEQ[2], SEQ[3]),
    ]
    for lo, hi, a, b in seg:
        mask = (t >= lo) & (t < hi)
        local = ((t[mask] - lo) / (hi - lo))[:, None]
        out[mask] = a + (b - a) * local
    return out


def _div_color(fg: np.ndarray, pool: np.ndarray) -> np.ndarray:
    """Vectorised diverging ramp red→cream→green around the pool baseline."""
    tt = np.clip((fg - pool) / MAX_DELTA, -1.0, 1.0)
    out = np.empty(tt.shape + (3,), float)
    pos = tt >= 0
    out[pos] = C_MID + (C_HOT - C_MID) * tt[pos][:, None]
    neg = ~pos
    out[neg] = C_MID + (C_COLD - C_MID) * (-tt[neg])[:, None]
    return out


def render_shot_heat_data_uri(
    dots: list[_Dot],
    zone_pool: dict[str, float | None],
    has_pool: bool,
) -> str | None:
    """Render the heat field to a base64 PNG data URI, or None if no shots."""
    pts = [d for d in dots if -55 <= d.loc_y <= 405]
    if not pts:
        return None

    px = np.array([HOOP_X + d.loc_x for d in pts])
    py = np.array([HOOP_Y - d.loc_y for d in pts])
    made = np.array([1.0 if d.made else 0.0 for d in pts])

    # Grid cell centres in court coords.
    cell_x = VB_W / GW
    cell_y = CROP_H / GH
    gx = (np.arange(GW) + 0.5) * cell_x
    gy = CROP_TOP + (np.arange(GH) + 0.5) * cell_y
    GX, GY = np.meshgrid(gx, gy)  # (GH, GW)

    W = np.zeros((GH, GW))
    M = np.zeros((GH, GW))
    two_sig2 = 2 * SIGMA * SIGMA
    for i in range(len(pts)):
        d2 = (GX - px[i]) ** 2 + (GY - py[i]) ** 2
        e = np.exp(-d2 / two_sig2)
        W += e
        M += e * made[i]

    max_w = W.max() or 1.0
    eff = np.divide(M, W, out=np.zeros_like(M), where=W > 0)

    flat_eff = eff.ravel()
    if has_pool:
        # pool baseline per cell, by the cell's zone
        lx = (GX - HOOP_X).ravel()
        ly = (HOOP_Y - GY).ravel()
        pool = np.array(
            [(zone_pool.get(_classify_zone(x, y)) or np.nan) for x, y in zip(lx, ly)]
        )
        rgb = np.where(
            np.isnan(pool)[:, None],
            _seq_color(flat_eff),
            _div_color(flat_eff, np.nan_to_num(pool)),
        )
    else:
        rgb = _seq_color(flat_eff)
    rgb = rgb.reshape(GH, GW, 3)

    intensity = np.power(W / max_w, 0.6)
    alpha = np.clip(0.18 + 0.78 * intensity, 0.0, 0.92)
    alpha[W < max_w * 0.05] = 0.0

    rgba = np.zeros((GH, GW, 4), np.uint8)
    rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)

    img = Image.fromarray(rgba, "RGBA").resize(
        (OUT_W, OUT_H), Image.Resampling.BILINEAR
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
