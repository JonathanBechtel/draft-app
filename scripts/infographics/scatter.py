"""Template T1 — "Who Beat the Board?" outcome-vs-consensus scatter.

Mirrors the live /draft-recap chart geometry (linear axes, SQUARE plot, span =
max(rank, pick) - 1, 45-degree reference line) so player positions match the web
page, then layers the share chrome: title, a "Biggest Moves" side panel,
highlighted callouts for the biggest movers, a legend, and branding.

The template is data-driven: the highlighted players and their callouts come
from ``biggest_risers[:3]`` / ``biggest_fallers[:3]`` in the data, so it renders
for any draft year without code changes.
"""

from __future__ import annotations

import math

from . import theme as T

# Layout — chart card and side panel share a top/bottom so they line up.
CARD_X, CARD_Y, CARD_W, CARD_H = 40, 160, 1370, 815
PX0, PX1 = 395, 1055  # SQUARE plot box (centered in the card)
PY0, PY1 = 215, 875
R = 26  # player-face radius
SP_X, SP_W = 1430, 520  # side panel
CW, CH, PAD = 280, 70, 18  # callout card

# Fixed callout slots (top-to-bottom). Movers are assigned to slots ordered by
# their dot height so the connectors fan out without crossing.
_LEFT_SLOTS = [(84, 500), (84, 620), (84, 740)]  # fallers
_RIGHT_SLOTS = [(1100, 480), (1100, 600), (1100, 720)]  # risers


def render(data: dict, faces: dict, params: dict | None = None) -> str:
    picks = data["picks"]
    risers = data.get("biggest_risers", [])
    fallers = data.get("biggest_fallers", [])
    year = data.get("draft_year", "")

    ranked = [p for p in picks if p.get("consensus_rank") is not None]
    if not ranked:
        # No consensus-ranked picks (e.g. a year with results but no consensus
        # snapshot, or an empty DB). Render a clear no-data state rather than
        # crashing on max() over an empty sequence.
        body = (
            T.DEFS
            + T.background()
            + T.header(
                "Who Beat the Board?",
                "Actual draft slot vs. consensus expectation",
                "No consensus-ranked picks for this draft year yet.",
            )
            + T.footer()
        )
        return T.document(body, f"Who Beat the Board? — {year} NBA Draft Recap")

    max_n = max(2, max(max(p["consensus_rank"], p["overall_pick"]) for p in ranked))
    span = max_n - 1

    def x(rank):
        return PX0 + (rank - 1) / span * (PX1 - PX0)

    def y(pick):
        return PY0 + (pick - 1) / span * (PY1 - PY0)

    def ticks():
        step = 10 if max_n <= 60 else 20
        out, v = [1], step
        while v <= max_n:
            out.append(v)
            v += step
        return out

    # --- which players get highlighted + a callout (data-driven) --------------
    fall3 = sorted(
        fallers[:3], key=lambda p: p["overall_pick"]
    )  # higher on chart first
    rise3 = sorted(risers[:3], key=lambda p: p["overall_pick"])
    labeled = list(zip(fall3, _LEFT_SLOTS)) + list(zip(rise3, _RIGHT_SLOTS))
    highlight_ids = {p["player_id"] for p, _ in labeled}

    s = [
        T.background(),
        f'<rect x="{CARD_X}" y="{CARD_Y}" width="{CARD_W}" height="{CARD_H}" rx="22" '
        f'fill="{T.CARD}" filter="url(#cardshadow)"/>',
        T.header(
            "Who Beat the Board?",
            "Actual draft slot vs. consensus expectation",
            "Risers were drafted earlier than expected. Fallers slid past consensus.",
        ),
    ]

    # --- axes (linear) --------------------------------------------------------
    s.append(
        f'<line x1="{PX0}" y1="{PY0 - 22}" x2="{PX0}" y2="{PY1}" stroke="#cbd5e1" stroke-width="1.5"/>'
    )
    s.append(
        f'<line x1="{PX0}" y1="{PY1}" x2="{PX1 + 12}" y2="{PY1}" stroke="#cbd5e1" stroke-width="1.5"/>'
    )
    for t in ticks():
        gy = y(t)
        s.append(
            f'<line x1="{PX0}" y1="{gy:.1f}" x2="{PX1}" y2="{gy:.1f}" stroke="#eef1f6" stroke-width="1"/>'
        )
        s.append(
            f'<text x="{PX0 - 16}" y="{gy + 7:.1f}" text-anchor="end" font-family="monospace" '
            f'font-size="22" fill="#94a3b8">{t}</text>'
        )
        gx = x(t)
        s.append(
            f'<line x1="{gx:.1f}" y1="{PY0 - 22}" x2="{gx:.1f}" y2="{PY1}" stroke="#eef1f6" stroke-width="1"/>'
        )
        s.append(
            f'<text x="{gx:.1f}" y="{PY1 + 34}" text-anchor="middle" font-family="monospace" '
            f'font-size="22" fill="#94a3b8">{t}</text>'
        )
    s.append(
        f'<text transform="translate(62,{(PY0 + PY1) / 2:.0f}) rotate(-90)" text-anchor="middle" '
        f'font-family="monospace" font-weight="700" font-size="23" fill="{T.INK}">Actual pick  &#8594;</text>'
    )
    s.append(
        f'<text x="{(PX0 + PX1) / 2:.0f}" y="{PY1 + 72}" text-anchor="middle" font-family="monospace" '
        f'font-weight="700" font-size="26" fill="{T.INK}">Consensus rank  &#8594;</text>'
    )

    # --- 45-degree reference line + zone labels -------------------------------
    dx0, dy0, dx1, dy1 = x(1), y(1), x(max_n), y(max_n)
    s.append(
        f'<line x1="{dx0:.1f}" y1="{dy0:.1f}" x2="{dx1:.1f}" y2="{dy1:.1f}" '
        f'stroke="#b8c4dc" stroke-width="2.5" stroke-dasharray="7 7"/>'
    )
    ang = math.degrees(math.atan2(dy1 - dy0, dx1 - dx0))
    lx, ly = x(min(29, max_n - 2)), y(min(29, max_n - 2))
    s.append(
        f'<text transform="translate({lx:.1f},{ly - 13:.1f}) rotate({ang:.1f})" text-anchor="middle" '
        f'font-family="Arial, sans-serif" font-style="italic" font-size="20" fill="#8493ad" '
        f"{T.BIGHALO}>Drafted as expected</text>"
    )
    ex, ey = x(round(max_n * 0.64)), y(5)
    s.append(
        f'<text x="{ex:.0f}" y="{ey:.0f}" text-anchor="middle" font-family="Arial, sans-serif" '
        f'font-weight="800" font-size="22" fill="{T.INDIGO_DK}" {T.HALO}>Went earlier'
        f'<tspan x="{ex:.0f}" dy="26">than expected</tspan></text>'
    )
    lx2, ly2 = x(round(max_n * 0.42)), y(round(max_n * 0.86))
    s.append(
        f'<text x="{lx2:.0f}" y="{ly2:.0f}" text-anchor="middle" font-family="Arial, sans-serif" '
        f'font-weight="800" font-size="22" fill="{T.ORANGE_DK}" {T.HALO}>Went later'
        f'<tspan x="{lx2:.0f}" dy="26">than expected</tspan></text>'
    )

    # --- player faces ---------------------------------------------------------
    def face(p, highlight=False):
        rank = p.get("consensus_rank")
        if rank is None:
            return ""
        cx, cy = x(rank), y(p["overall_pick"])
        col = T.DIR_COLOR.get(p["direction"], T.SLATE)
        sw = 6 if highlight else (5 if p["direction"] in ("earlier", "later") else 3.5)
        img = faces.get(p["player_id"])
        cid = f"c{p['player_id']}"
        out = []
        if highlight:
            out.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R + 4}" fill="{col}" '
                f'opacity="0.55" filter="url(#glow)"/>'
            )
        out.append('<g filter="url(#soft)">')
        out.append(
            f'<clipPath id="{cid}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R - 4}"/></clipPath>'
        )
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R}" fill="#fff"/>')
        if img:
            out.append(
                f'<image href="{img}" x="{cx - (R - 4):.1f}" y="{cy - (R - 4):.1f}" '
                f'width="{2 * (R - 4)}" height="{2 * (R - 4)}" clip-path="url(#{cid})" '
                f'preserveAspectRatio="xMidYMid slice"/>'
            )
        else:
            out.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R - 4}" fill="#e2e8f0"/>'
            )
        if highlight:
            out.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R + 1}" fill="none" stroke="#fff" stroke-width="3"/>'
            )
        out.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R - 2 if not highlight else R}" '
            f'fill="none" stroke="{col}" stroke-width="{sw}"/>'
        )
        out.append("</g>")
        return "".join(out)

    # z-order: neutral, then movers, then highlighted players on top
    for p in picks:
        if p["player_id"] not in highlight_ids and p["direction"] not in (
            "earlier",
            "later",
        ):
            s.append(face(p))
    for p in picks:
        if p["player_id"] not in highlight_ids and p["direction"] in (
            "earlier",
            "later",
        ):
            s.append(face(p))
    for p in picks:
        if p["player_id"] in highlight_ids:
            s.append(face(p, highlight=True))

    # --- callout cards (all cards first, then arrows on top) ------------------
    def callout_card(p, ax, ay):
        col = T.DIR_COLOR[p["direction"]]
        arrow = "&#9650;" if p["direction"] == "earlier" else "&#9660;"
        sub = (
            f"#{p['consensus_rank']} &#8594; #{p['overall_pick']} &#183; "
            f"{T.esc(p['team_abbreviation'])}"
        )
        out = [
            f'<g filter="url(#cardshadow)"><rect x="{ax:.1f}" y="{ay:.1f}" width="{CW}" height="{CH}" '
            f'rx="12" fill="#fff" stroke="{col}" stroke-width="2.5"/></g>'
        ]
        out.append(
            f'<text x="{ax + PAD:.1f}" y="{ay + 30:.1f}" font-family="Arial, sans-serif" '
            f'font-weight="800" font-size="21" fill="{T.NAVY}">{T.esc(p["player_name"])}</text>'
        )
        out.append(
            f'<text x="{ax + PAD:.1f}" y="{ay + 55:.1f}" font-family="monospace" font-size="18" '
            f'fill="{T.MUTE}">{sub}  <tspan fill="{col}" font-weight="700" font-family="Arial">'
            f"{arrow}{abs(p['delta'])}</tspan></text>"
        )
        return "".join(out)

    def callout_arrow(p, ax, ay):
        col = T.DIR_COLOR[p["direction"]]
        cx, cy = x(p["consensus_rank"]), y(p["overall_pick"])
        sx = min(max(cx, ax), ax + CW)
        sy = min(max(cy, ay), ay + CH)
        vx, vy = cx - sx, cy - sy
        d = max(1.0, math.hypot(vx, vy))
        bx, by = vx / d, vy / d
        tx, ty = cx - bx * (R + 5), cy - by * (R + 5)
        ah = 9
        px, py = -by, bx
        return "".join(
            [
                f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" stroke="#fff" stroke-width="6"/>',
                f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" stroke="{col}" stroke-width="2.75"/>',
                f'<path d="M {tx:.1f} {ty:.1f} L {tx - bx * ah + px * ah * 0.65:.1f} '
                f"{ty - by * ah + py * ah * 0.65:.1f} L {tx - bx * ah - px * ah * 0.65:.1f} "
                f'{ty - by * ah - py * ah * 0.65:.1f} Z" fill="{col}"/>',
            ]
        )

    for p, (ax, ay) in labeled:
        s.append(callout_card(p, ax, ay))
    for p, (ax, ay) in labeled:
        s.append(callout_arrow(p, ax, ay))

    # --- side panel: Biggest Moves -------------------------------------------
    s.append(
        f'<rect x="{SP_X}" y="{CARD_Y}" width="{SP_W}" height="{CARD_H}" rx="22" fill="#fbfcfe" '
        f'stroke="#e2e8f0" stroke-width="1.5" filter="url(#cardshadow)"/>'
    )
    s.append(
        f'<text x="{SP_X + 34}" y="{CARD_Y + 62}" font-family="Arial Black, Arial, sans-serif" '
        f'font-weight="900" font-size="38" fill="{T.NAVY}">Biggest Moves</text>'
    )

    def mover_section(title, items, color, up, y0):
        o = [
            f'<circle cx="{SP_X + 50}" cy="{y0}" r="20" fill="{color}"/>',
            f'<text x="{SP_X + 50}" y="{y0 + 9}" text-anchor="middle" font-family="Arial" '
            f'font-weight="900" font-size="24" fill="#fff">{"&#8593;" if up else "&#8595;"}</text>',
            f'<text x="{SP_X + 82}" y="{y0 + 11}" font-family="Arial, sans-serif" font-weight="800" '
            f'font-size="30" fill="{T.NAVY}">{title}</text>',
        ]
        ry = y0 + 58
        for i, p in enumerate(items[:3], 1):
            arrow = "&#8593;" if up else "&#8595;"
            o.append(
                f'<text x="{SP_X + 30}" y="{ry + 8}" font-family="Arial" font-weight="800" '
                f'font-size="26" fill="#334155">{i}</text>'
            )
            o.append(
                f'<text x="{SP_X + 58}" y="{ry + 8}" font-family="Arial, sans-serif" font-weight="700" '
                f'font-size="22" fill="{T.NAVY}">{T.esc(p["player_name"])}</text>'
            )
            o.append(
                f'<text x="{SP_X + 284}" y="{ry + 8}" font-family="Arial" font-weight="800" '
                f'font-size="22" fill="{color}">{arrow}{abs(p["delta"])}</text>'
            )
            o.append(
                f'<text x="{SP_X + 334}" y="{ry + 7}" font-family="monospace" font-size="16" '
                f'fill="{T.MUTE}">#{p["consensus_rank"]}&#8594;#{p["overall_pick"]}</text>'
            )
            o.append(
                f'<text x="{SP_X + SP_W - 30}" y="{ry + 7}" text-anchor="end" font-family="monospace" '
                f'font-weight="700" font-size="16" fill="#475569">{T.esc(p["team_abbreviation"])}</text>'
            )
            ry += 54
        return "".join(o), ry

    block, y_after = mover_section(
        "Biggest Risers", risers, T.INDIGO, True, CARD_Y + 150
    )
    s.append(block)
    s.append(
        f'<line x1="{SP_X + 34}" y1="{y_after + 16}" x2="{SP_X + SP_W - 34}" y2="{y_after + 16}" '
        f'stroke="#e2e8f0" stroke-width="1.5"/>'
    )
    block, y_after2 = mover_section(
        "Biggest Fallers", fallers, T.ORANGE, False, y_after + 72
    )
    s.append(block)

    lg_y = y_after2 + 16
    for col, txt in [
        (T.INDIGO, "drafted ahead of consensus"),
        (T.ORANGE, "drafted later than consensus"),
        (T.SLATE, "about expected"),
    ]:
        s.append(
            f'<circle cx="{SP_X + 50}" cy="{lg_y}" r="13" fill="#f8fafc" stroke="{col}" stroke-width="4"/>'
        )
        s.append(
            f'<text x="{SP_X + 80}" y="{lg_y + 8}" font-family="Arial, sans-serif" font-size="22" '
            f'fill="#475569">{txt}</text>'
        )
        lg_y += 46

    s.append(T.footer())

    body = T.DEFS + "".join(s)
    return T.document(body, f"Who Beat the Board? — {year} NBA Draft Recap")
