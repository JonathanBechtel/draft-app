"""Template T3 — single-player hero card ("Steal of the Draft" / "Biggest Reach").

One dominant face + one big number. ``params["mode"]`` selects ``"steal"`` (top
riser, default) or ``"reach"`` (top faller). A small strip of runner-ups grounds
the claim. Maximally screenshot-friendly for a single bold post.
"""

from __future__ import annotations

from . import theme as T

CARD_X, CARD_Y, CARD_W, CARD_H = 40, 160, 1900, 815
HERO_CX, HERO_CY, HERO_R = 410, 560, 205


def _big_face(cx, cy, img, col, r) -> str:
    cid = "heroface"
    out = [
        f'<circle cx="{cx}" cy="{cy}" r="{r + 10}" fill="{col}" opacity="0.5" filter="url(#glow)"/>',
        f'<clipPath id="{cid}"><circle cx="{cx}" cy="{cy}" r="{r - 6}"/></clipPath>',
        f'<g filter="url(#soft)"><circle cx="{cx}" cy="{cy}" r="{r}" fill="#fff"/>',
    ]
    if img:
        out.append(
            f'<image href="{img}" x="{cx - (r - 6)}" y="{cy - (r - 6)}" '
            f'width="{2 * (r - 6)}" height="{2 * (r - 6)}" clip-path="url(#{cid})" '
            f'preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r - 6}" fill="#e2e8f0"/>')
    out.append(
        f'<circle cx="{cx}" cy="{cy}" r="{r - 2}" fill="none" stroke="#fff" stroke-width="5"/>'
    )
    out.append(
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{col}" stroke-width="7"/></g>'
    )
    return "".join(out)


def _runner(x, y, p, img, col, up) -> str:
    r = 30
    cid = f"run{int(x)}"
    arrow = "&#9650;" if up else "&#9660;"
    out = [
        f'<clipPath id="{cid}"><circle cx="{x}" cy="{y}" r="{r - 3}"/></clipPath>',
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff"/>',
    ]
    if img:
        out.append(
            f'<image href="{img}" x="{x - (r - 3)}" y="{y - (r - 3)}" width="{2 * (r - 3)}" '
            f'height="{2 * (r - 3)}" clip-path="url(#{cid})" preserveAspectRatio="xMidYMid slice"/>'
        )
    else:
        out.append(f'<circle cx="{x}" cy="{y}" r="{r - 3}" fill="#e2e8f0"/>')
    out.append(
        f'<circle cx="{x}" cy="{y}" r="{r - 1}" fill="none" stroke="{col}" stroke-width="3.5"/>'
    )
    out.append(
        f'<text x="{x + r + 14}" y="{y - 2}" font-family="Arial, sans-serif" font-weight="800" '
        f'font-size="24" fill="{T.NAVY}">{T.esc(p["player_name"])}</text>'
    )
    out.append(
        f'<text x="{x + r + 14}" y="{y + 24}" font-family="monospace" font-size="18" '
        f'fill="{T.MUTE}">#{p["consensus_rank"]}&#8594;#{p["overall_pick"]} &#183; '
        f"{T.esc(p['team_abbreviation'])} "
        f'<tspan fill="{col}" font-weight="700" font-family="Arial">{arrow}{abs(p["delta"])}</tspan></text>'
    )
    return "".join(out)


def render(data: dict, faces: dict, params: dict | None = None) -> str:
    params = params or {}
    mode = params.get("mode", "steal")
    risers = data.get("biggest_risers", [])
    fallers = data.get("biggest_fallers", [])
    year = data.get("draft_year", "")

    if mode == "reach":
        pool, color, color_dk, kicker, up = (
            fallers,
            T.ORANGE,
            T.ORANGE_DK,
            "BIGGEST REACH",
            False,
        )
    else:
        pool, color, color_dk, kicker, up = (
            risers,
            T.INDIGO,
            T.INDIGO_DK,
            "STEAL OF THE DRAFT",
            True,
        )

    if not pool:
        body = T.DEFS + T.background() + T.header("No movers yet", "", "") + T.footer()
        return T.document(body, f"Hero — {year} NBA Draft")

    hero = pool[0]
    runners = pool[1:4]
    d = abs(hero["delta"])
    where = "ahead of" if up else "behind"

    s = [
        T.background(),
        f'<rect x="{CARD_X}" y="{CARD_Y}" width="{CARD_W}" height="{CARD_H}" rx="22" '
        f'fill="{T.CARD}" filter="url(#cardshadow)"/>',
        # brand lockup (no big title — the kicker is the headline)
        f'<text x="{T.W - 60}" y="70" text-anchor="end" font-family="Arial Black, Arial, sans-serif" '
        f'font-weight="900" font-size="44" fill="{T.NAVY}">{T.BRAND}</text>',
        f'<text x="{T.W - 60}" y="104" text-anchor="end" font-family="Arial, sans-serif" font-size="20" '
        f'fill="{T.MUTE}">{T.BRAND_TAGLINE}</text>',
    ]

    # kicker
    s.append(
        f'<text x="100" y="290" font-family="Arial Black, Arial, sans-serif" font-weight="900" '
        f'font-size="58" fill="{color_dk}" letter-spacing="2">{kicker}</text>'
    )

    # hero face
    s.append(_big_face(HERO_CX, HERO_CY, faces.get(hero["player_id"]), color, HERO_R))

    # name + numbers (right of the face)
    tx = 700
    s.append(
        f'<text x="{tx}" y="430" font-family="Arial Black, Arial, sans-serif" font-weight="900" '
        f'font-size="88" fill="{T.NAVY}">{T.esc(hero["player_name"])}</text>'
    )
    # big move row
    s.append(
        f'<text x="{tx}" y="540" font-family="monospace" font-weight="700" font-size="56" '
        f'fill="{T.INK}">#{hero["consensus_rank"]} &#8594; #{hero["overall_pick"]}</text>'
    )
    # delta pill
    pill_x = tx + 430
    s.append(
        f'<rect x="{pill_x}" y="500" width="150" height="62" rx="31" fill="{color}"/>'
    )
    arrow = "&#9650;" if up else "&#9660;"
    s.append(
        f'<text x="{pill_x + 75}" y="544" text-anchor="middle" font-family="Arial" font-weight="900" '
        f'font-size="40" fill="#fff">{arrow}{d}</text>'
    )
    # team + descriptor
    s.append(
        f'<text x="{tx}" y="612" font-family="Arial, sans-serif" font-weight="700" font-size="32" '
        f'fill="{T.MUTE}">{T.esc(hero["team_abbreviation"])} &#183; went #{hero["overall_pick"]} overall</text>'
    )
    s.append(
        f'<text x="{tx}" y="672" font-family="Arial, sans-serif" font-size="30" fill="{T.INK}">'
        f"Drafted {d} spots {where} where the field had him.</text>"
    )

    # runner-ups strip
    if runners:
        s.append(
            f'<line x1="100" y1="780" x2="{T.W - 100}" y2="780" stroke="#e2e8f0" stroke-width="1.5"/>'
        )
        label = "Other steals" if up else "Other reaches"
        s.append(
            f'<text x="100" y="838" font-family="Arial, sans-serif" font-weight="800" font-size="24" '
            f'fill="{T.MUTE}">{label}:</text>'
        )
        rx = 340
        for p in runners:
            s.append(_runner(rx, 830, p, faces.get(p["player_id"]), color, up))
            rx += 520

    s.append(T.footer())
    return T.document(T.DEFS + "".join(s), f"{kicker.title()} — {year} NBA Draft")
