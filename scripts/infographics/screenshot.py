"""Screenshot an infographic HTML to a Twitter-ready PNG (Playwright).

    conda run -n draftguru python -m scripts.infographics.screenshot \
        /tmp/recap.html /tmp/recap.png

Renders the 1980x1080 (16:9) canvas at 2x for a crisp retina image that drops
straight into an X/Twitter post.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

W, H = 1980, 1080


def shoot(html_path: str, png_path: str, scale: int = 2) -> None:
    url = Path(html_path).resolve().as_uri()
    Path(png_path).parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": W, "height": H}, device_scale_factor=scale
        )
        page.goto(url)
        page.wait_for_timeout(300)  # let base64 images decode + layout settle
        page.screenshot(path=png_path, clip={"x": 0, "y": 0, "width": W, "height": H})
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("html", help="Input HTML path.")
    ap.add_argument("png", help="Output PNG path.")
    ap.add_argument(
        "--scale", type=int, default=2, help="Device scale factor (default: 2)."
    )
    args = ap.parse_args()
    shoot(args.html, args.png, args.scale)
    print(f"wrote {args.png}")


if __name__ == "__main__":
    main()
