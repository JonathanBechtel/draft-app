"""Shared visual theme + SVG document chrome for share infographics.

Palette, filters, and the title/footer/branding band are common to every
template; individual templates (scatter, leaderboard, hero) compose these with
their own chart body. Keeping them here means a brand tweak lands everywhere.
"""

from __future__ import annotations

import html

# Canvas — 16:9 so the screenshot drops straight into an X/Twitter card.
W, H = 1980, 1080

# Palette — mirrors the live /draft-recap styling.
INDIGO, INDIGO_DK = "#6366f1", "#4f46e5"  # riser / drafted ahead of consensus
ORANGE, ORANGE_DK = "#f97316", "#ea580c"  # faller / drafted later than consensus
SLATE = "#cbd5e1"  # neutral / about expected
NAVY = "#11224a"  # headings + branding
INK, MUTE = "#1e293b", "#64748b"
CARD, BG = "#ffffff", "#eef2f8"

DIR_COLOR = {"earlier": INDIGO, "later": ORANGE, "even": SLATE, "unranked": SLATE}

BRAND = "nbadraft.app"
BRAND_TAGLINE = "The Homepage for NBA Draft Obsessives"

# White text-casing so labels stay legible over faces/cards.
HALO = 'style="paint-order:stroke" stroke="#ffffff" stroke-width="5" stroke-linejoin="round"'
BIGHALO = 'style="paint-order:stroke" stroke="#ffffff" stroke-width="8" stroke-linejoin="round"'

# Reusable filters: soft face shadow, card shadow, and a colored glow for
# highlighting the players the graphic is actually about.
DEFS = (
    "<defs>"
    '<filter id="soft" x="-30%" y="-30%" width="160%" height="160%">'
    '<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0f172a" flood-opacity="0.18"/></filter>'
    '<filter id="cardshadow" x="-20%" y="-20%" width="140%" height="140%">'
    '<feDropShadow dx="0" dy="4" stdDeviation="9" flood-color="#1e293b" flood-opacity="0.16"/></filter>'
    '<filter id="glow" x="-80%" y="-80%" width="260%" height="260%">'
    '<feGaussianBlur stdDeviation="7"/></filter>'
    "</defs>"
)


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def background() -> str:
    return f'<rect width="{W}" height="{H}" fill="{BG}"/>'


def header(title: str, subtitle: str = "", tagline: str = "") -> str:
    """Top band: big title on the left, brand lockup on the right."""
    out = [
        f'<text x="60" y="78" font-family="Arial Black, Arial, sans-serif" font-weight="900" '
        f'font-size="62" fill="{NAVY}">{esc(title)}</text>',
        f'<text x="{W - 60}" y="70" text-anchor="end" font-family="Arial Black, Arial, sans-serif" '
        f'font-weight="900" font-size="44" fill="{NAVY}">{BRAND}</text>',
        f'<text x="{W - 60}" y="104" text-anchor="end" font-family="Arial, sans-serif" '
        f'font-size="20" fill="{MUTE}">{BRAND_TAGLINE}</text>',
    ]
    if subtitle:
        out.append(
            f'<text x="62" y="118" font-family="Arial, sans-serif" font-weight="700" '
            f'font-size="27" fill="#33405a">{esc(subtitle)}</text>'
        )
    if tagline:
        out.append(
            f'<text x="62" y="146" font-family="Arial, sans-serif" font-size="19" '
            f'fill="{MUTE}">{esc(tagline)}</text>'
        )
    return "".join(out)


def footer(fy: int = 1012) -> str:
    """Bottom branding band (a divider + the brand lockup, no dead links)."""
    return "".join(
        [
            f'<line x1="40" y1="{fy - 14}" x2="{W - 40}" y2="{fy - 14}" stroke="#d7deea" stroke-width="1.5"/>',
            f'<text x="{W - 130}" y="{fy + 36}" text-anchor="end" font-family="Arial Black, Arial, sans-serif" '
            f'font-weight="900" font-size="44" fill="{NAVY}">{BRAND}</text>',
            f'<circle cx="{W - 78}" cy="{fy + 24}" r="26" fill="{NAVY}"/>',
            f'<text x="{W - 78}" y="{fy + 33}" text-anchor="middle" font-family="Arial" '
            f'font-weight="900" font-size="28" fill="#fff">&#8594;</text>',
        ]
    )


def document(body: str, title: str) -> str:
    """Wrap an SVG body (DEFS + elements) into a self-contained HTML file."""
    svg = (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Arial, sans-serif" width="100%" style="height:auto;display:block">'
        f"{body}</svg>"
    )
    style = (
        "html,body{margin:0;background:%s;}"
        ".wrap{max-width:%dpx;margin:0 auto;}"
        "svg{width:100%%;height:auto;}"
    ) % (BG, W)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{style}</style></head>"
        f'<body><div class="wrap">{svg}</div></body></html>\n'
    )
