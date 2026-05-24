"""Gather material for the next X thread.

Run with::

    conda run -n draftguru python -m scripts.x_threads.gather

The script picks an angle, queries the DB for relevant facts, renders any
images for the chosen angle, and writes everything to a draft directory under
``scripts/x_threads/drafts/``. The directory path is printed to stdout on the
last line so the calling skill can resolve it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Import after load_dotenv so app.config resolves env-driven settings.
from app.services.x_threads.angle_picker import (  # noqa: E402
    DEFAULT_DEDUP_DAYS,
    pick_angle,
)
from app.services.x_threads.data_gatherer import gather_for_pick  # noqa: E402
from app.schemas.x_post_history import XPostAngle  # noqa: E402
from app.utils.db_async import SessionLocal  # noqa: E402

DRAFTS_ROOT = Path(__file__).resolve().parent / "drafts"

logger = logging.getLogger("x_threads.gather")


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "subject"


def _resolve_angle(arg: Optional[str]) -> Optional[XPostAngle]:
    if not arg:
        return None
    try:
        return XPostAngle(arg.lower())
    except ValueError as exc:
        raise SystemExit(f"Unknown angle: {arg}") from exc


def _draft_dir_for(angle: str, subject_slug: str) -> Path:
    now = datetime.utcnow()
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%H%M%S")
    return DRAFTS_ROOT / day / f"{stamp}_{angle}_{subject_slug}"


async def _run(args: argparse.Namespace) -> int:
    preferred = _resolve_angle(args.angle)

    async with SessionLocal() as db:
        pick = await pick_angle(
            db,
            window_days=args.window_days,
            preferred_angle=preferred,
        )
        if pick is None:
            print(
                json.dumps({"status": "no_viable_angle"}),
                file=sys.stderr,
            )
            return 2

        subject_slug = _slugify(pick.players[0].slug or pick.players[0].display_name)
        draft_dir = (
            Path(args.output_dir)
            if args.output_dir
            else _draft_dir_for(pick.angle, subject_slug)
        )
        draft_dir.mkdir(parents=True, exist_ok=True)

        result = await gather_for_pick(db, pick, output_dir=draft_dir)

    payload = {
        "angle": result.angle,
        "headline": result.headline,
        "players": [asdict(p) for p in result.players],
        "facts": [asdict(f) for f in result.facts],
        "comps": [asdict(c) for c in result.comps],
        "news": result.news,
        "images": [
            str(Path(p).relative_to(DRAFTS_ROOT.parent.parent))
            if Path(p).is_absolute()
            else p
            for p in result.images
        ],
        "extra": result.extra,
        "notes": result.notes,
        "draft_dir": str(draft_dir),
        "news_item_id": pick.news_item_id,
    }
    json_path = draft_dir / "gather.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    # Print the dir on the final line; everything before is informational logs.
    print(
        f"angle={payload['angle']} subject={pick.players[0].display_name}",
        file=sys.stderr,
    )
    print(str(draft_dir))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--angle",
        help="Force a specific angle (spotlight, h2h, outlier, news_tag).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_DEDUP_DAYS,
        help="Dedup window in days (default: %(default)s).",
    )
    parser.add_argument(
        "--output-dir",
        help="Override the draft directory location.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Python logging level (default: %(default)s).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
