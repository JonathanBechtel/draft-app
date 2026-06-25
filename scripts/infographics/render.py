"""Render a share infographic to a self-contained HTML file.

Directed (you pick the template):

    conda run -n draftguru python -m scripts.infographics.render \
        --template scatter --year 2026 --out /tmp/recap.html
    conda run -n draftguru python -m scripts.infographics.render \
        --template hero --mode reach --out /tmp/reach.html

Autonomous (let the fact engine pick the most share-worthy angle):

    conda run -n draftguru python -m scripts.infographics.render --auto --out /tmp/auto.html

Pulls live /draft-recap data, embeds player faces as base64, and writes HTML
that screenshots cleanly for X/Twitter (see ``scripts.infographics.screenshot``).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import assets
from . import data as data_mod
from . import facts, hero, leaderboard, scatter

TEMPLATES = {
    "scatter": scatter.render,  # "Who Beat the Board?" outcome-vs-consensus
    "leaderboard": leaderboard.render,  # "Biggest Movers"
    "hero": hero.render,  # "Steal of the Draft" / "Biggest Reach"
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", default="scatter", choices=sorted(TEMPLATES))
    ap.add_argument(
        "--auto",
        action="store_true",
        help="Let facts.py pick the template + params (overrides --template).",
    )
    ap.add_argument(
        "--mode",
        choices=["steal", "reach"],
        default=None,
        help="Hero template subject (default: steal).",
    )
    ap.add_argument(
        "--year",
        type=int,
        default=None,
        help="Draft year (default: latest with results).",
    )
    ap.add_argument("--out", required=True, help="Output HTML path.")
    args = ap.parse_args()

    recap = data_mod.load_recap(args.year)
    faces = assets.build_faces(recap["picks"])

    if args.auto:
        pick = facts.best(recap)
        if pick is None:
            raise SystemExit("No facts to render for this data.")
        template, params, why = pick.template, pick.params, pick.headline
    else:
        template, params, why = args.template, {}, "(directed)"
        if args.mode:
            params["mode"] = args.mode

    html = TEMPLATES[template](recap, faces, params)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(
        f"template={template} params={params} year={recap['draft_year']} "
        f"picks={len(recap['picks'])} faces={len(faces)} angle={why!r} "
        f"-> {out} ({out.stat().st_size // 1024} KB)"
    )


if __name__ == "__main__":
    main()
