"""Crop/thumbnail helper for the infographic-qa agent.

A real module (not a stdin heredoc) so it runs reliably under ``conda run``:

    conda run -n draftguru python -m scripts.infographics.qa_crop \
        --full /tmp/qa_full.png \
        --crop 0,150,420,900,/tmp/qa_left.png \
        --crop 1410,150,1980,985,/tmp/qa_panel.png \
        --thumb 520,/tmp/qa_thumb.png

Crop boxes are given in CANVAS coordinates (the 1980x1080 design space); they're
scaled to the real screenshot resolution via ``--canvas-scale`` (screenshots are
2x by default), so the QA agent can reason in canvas coords throughout.
"""

from __future__ import annotations

import argparse

from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", required=True, help="Full screenshot PNG.")
    ap.add_argument(
        "--canvas-scale",
        type=int,
        default=2,
        help="Screenshot px per canvas unit (default: 2).",
    )
    ap.add_argument(
        "--crop",
        action="append",
        default=[],
        metavar="x0,y0,x1,y1,out",
        help="Crop box in canvas coords + output path. Repeatable.",
    )
    ap.add_argument(
        "--thumb",
        metavar="width,out",
        help="Downscale the full image to <width>px and save to <out>.",
    )
    args = ap.parse_args()

    im = Image.open(args.full)
    sc = args.canvas_scale
    for spec in args.crop:
        x0, y0, x1, y1, out = spec.split(",", 4)
        box = (int(x0) * sc, int(y0) * sc, int(x1) * sc, int(y1) * sc)
        im.crop(box).save(out)
        print(f"crop -> {out}")
    if args.thumb:
        width_s, out = args.thumb.split(",", 1)
        width = int(width_s)
        im.resize(
            (width, round(im.height * width / im.width)), Image.Resampling.LANCZOS
        ).save(out)
        print(f"thumb -> {out}")


if __name__ == "__main__":
    main()
