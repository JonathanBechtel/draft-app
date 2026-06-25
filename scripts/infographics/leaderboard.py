"""Template T2 — "Biggest Movers" leaderboard.

Two face-forward columns (risers vs fallers) ranked by how far each player moved
off consensus, each row carrying a proportional delta bar. Reads
``biggest_risers`` / ``biggest_fallers`` straight from the recap data.
"""

from __future__ import annotations

from . import theme as T

CARD_Y, CARD_H = 160, 815
ROW_H = 120
FACE_R = 38
ROWS = 5  # rows per column


def _face(cx: float, cy: float, img, col: str, r: int = FACE_R) -> str:
    cid = f"lf{int(cx)}_{int(cy)}"
    out = [
        f'<clipPath id="{cid}"><circle cx="{cx}" cy="{cy}" r="{r - 3}"/></clipPath>',
        f'<g filter="url(#soft)"><circle cx="{cx}" cy="{cy}" r="{r}" fill="#fff"/>',
    ]
    if img:
        out.append(
            f'<image href="{img}" x="{cx - (r - 3)}" y="{cy - (r - 3)}" '
            f'width="{2 * (r - 3)}" height="{2 * (r - 3)}" clip-path="url(#{cid})" '
            f'preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r - 3}" fill="#e2e8f0"/>')
    out.append(
        f'<circle cx="{cx}" cy="{cy}" r="{r - 1}" fill="none" stroke="{col}" stroke-width="4"/></g>'
    )
    return "".join(out)


def render(data: dict, faces: dict, params: dict | None = None) -> str:
    risers = data.get("biggest_risers", [])
    fallers = data.get("biggest_fallers", [])
    year = data.get("draft_year", "")
    max_d = max([1] + [abs(p["delta"]) for p in (risers + fallers)])

    s = [
        T.background(),
        T.header(
            "Biggest Movers",
            "How far players jumped or slid vs. consensus",
            "Move = consensus rank minus where they actually went.",
        ),
    ]

    def column(x0: int, w: int, title: str, items: list, color: str, up: bool) -> str:
        o = [
            f'<rect x="{x0}" y="{CARD_Y}" width="{w}" height="{CARD_H}" rx="22" '
            f'fill="{T.CARD}" filter="url(#cardshadow)"/>'
        ]
        o.append(f'<circle cx="{x0 + 56}" cy="{CARD_Y + 60}" r="22" fill="{color}"/>')
        o.append(
            f'<text x="{x0 + 56}" y="{CARD_Y + 69}" text-anchor="middle" font-family="Arial" '
            f'font-weight="900" font-size="26" fill="#fff">{"&#8593;" if up else "&#8595;"}</text>'
        )
        o.append(
            f'<text x="{x0 + 92}" y="{CARD_Y + 72}" font-family="Arial Black, Arial, sans-serif" '
            f'font-weight="900" font-size="36" fill="{T.NAVY}">{title}</text>'
        )
        ry = CARD_Y + 112
        br = x0 + w - 92  # delta-bar right edge
        bmax = w * 0.26  # longest bar
        for i, p in enumerate(items[:ROWS], 1):
            cy = ry + ROW_H / 2
            o.append(
                f'<text x="{x0 + 34}" y="{cy + 9:.0f}" font-family="Arial" font-weight="800" '
                f'font-size="30" fill="#94a3b8">{i}</text>'
            )
            o.append(_face(x0 + 120, cy, faces.get(p["player_id"]), color))
            tx = x0 + 176
            o.append(
                f'<text x="{tx}" y="{cy - 6:.0f}" font-family="Arial, sans-serif" font-weight="800" '
                f'font-size="28" fill="{T.NAVY}">{T.esc(p["player_name"])}</text>'
            )
            o.append(
                f'<text x="{tx}" y="{cy + 24:.0f}" font-family="monospace" font-size="20" '
                f'fill="{T.MUTE}">#{p["consensus_rank"]} &#8594; #{p["overall_pick"]} &#183; '
                f"{T.esc(p['team_abbreviation'])}</text>"
            )
            blen = abs(p["delta"]) / max_d * bmax
            o.append(
                f'<rect x="{br - blen:.1f}" y="{cy - 13:.0f}" width="{blen:.1f}" height="26" '
                f'rx="6" fill="{color}" opacity="0.85"/>'
            )
            arrow = "&#9650;" if up else "&#9660;"
            o.append(
                f'<text x="{x0 + w - 26}" y="{cy + 9:.0f}" text-anchor="end" font-family="Arial" '
                f'font-weight="900" font-size="26" fill="{color}">{arrow}{abs(p["delta"])}</text>'
            )
            if i < ROWS:
                o.append(
                    f'<line x1="{x0 + 34}" y1="{ry + ROW_H:.0f}" x2="{x0 + w - 26}" '
                    f'y2="{ry + ROW_H:.0f}" stroke="#eef1f6" stroke-width="1.5"/>'
                )
            ry += ROW_H
        return "".join(o)

    s.append(column(40, 930, "Biggest Risers", risers, T.INDIGO, True))
    s.append(column(1010, 930, "Biggest Fallers", fallers, T.ORANGE, False))
    s.append(T.footer())
    return T.document(T.DEFS + "".join(s), f"Biggest Movers — {year} NBA Draft")
