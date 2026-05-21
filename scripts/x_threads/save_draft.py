"""Persist a drafted X thread.

Run after the skill has authored tweets into a text file::

    conda run -n draftguru python -m scripts.x_threads.save_draft \
        --draft-dir scripts/x_threads/drafts/2026-05-19/142211_outlier_cooper-flagg \
        --tweets-file <same dir>/tweets.txt

Tweets file format: each tweet separated by a line containing only ``---``.
Blank lines inside a tweet are preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from app.schemas import news_items as _news_items  # noqa: E402, F401
from app.schemas.x_post_history import (  # noqa: E402
    XPostAngle,
    XPostHistory,
    XPostStatus,
)
from app.utils.db_async import SessionLocal  # noqa: E402

# Keep the news_items import alive — XPostHistory has an FK to that table and
# SQLModel needs the target registered before the mapper resolves.
_ = _news_items

logger = logging.getLogger("x_threads.save_draft")

_TWEET_SEPARATOR = "---"
_MAX_TWEET_CHARS = 280


def _parse_tweets(tweets_text: str) -> list[str]:
    """Split a tweets file on '---' lines and strip whitespace around each."""
    blocks: list[list[str]] = [[]]
    for line in tweets_text.splitlines():
        if line.strip() == _TWEET_SEPARATOR:
            blocks.append([])
        else:
            blocks[-1].append(line)
    tweets = ["\n".join(block).strip() for block in blocks]
    return [t for t in tweets if t]


def _check_tweet_lengths(tweets: list[str]) -> list[str]:
    """Return a list of human-readable warnings for over-length tweets."""
    warnings: list[str] = []
    for idx, tweet in enumerate(tweets, start=1):
        if len(tweet) > _MAX_TWEET_CHARS:
            warnings.append(
                f"tweet {idx}: {len(tweet)} chars (limit {_MAX_TWEET_CHARS})"
            )
    return warnings


async def _save(args: argparse.Namespace) -> int:
    draft_dir = Path(args.draft_dir).resolve()
    if not draft_dir.is_dir():
        print(f"draft_dir not found: {draft_dir}", file=sys.stderr)
        return 2

    gather_path = draft_dir / "gather.json"
    if not gather_path.is_file():
        print(f"gather.json missing in {draft_dir}", file=sys.stderr)
        return 2

    gather: dict[str, Any] = json.loads(gather_path.read_text())

    tweets_file = Path(args.tweets_file).resolve()
    if not tweets_file.is_file():
        print(f"tweets file not found: {tweets_file}", file=sys.stderr)
        return 2

    tweets = _parse_tweets(tweets_file.read_text())
    if not tweets:
        print("no tweets parsed from tweets file", file=sys.stderr)
        return 2

    warnings = _check_tweet_lengths(tweets)
    if warnings and not args.allow_long:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(
            "refusing to save; pass --allow-long to override",
            file=sys.stderr,
        )
        return 3
    for w in warnings:
        logger.warning("over-length tweet allowed: %s", w)

    thread_txt = draft_dir / "thread.txt"
    thread_txt.write_text(("\n" + _TWEET_SEPARATOR + "\n").join(tweets))

    angle_value = gather.get("angle")
    if angle_value is None:
        print("gather.json missing angle", file=sys.stderr)
        return 2

    angle = XPostAngle(angle_value)
    player_ids = [
        int(p["id"]) for p in gather.get("players", []) if p.get("id") is not None
    ]
    image_paths = [str(p) for p in gather.get("images", [])]
    headline = gather.get("headline")
    notes = gather.get("notes")
    news_item_id = gather.get("news_item_id")

    tweet_objects = [{"text": t} for t in tweets]

    async with SessionLocal() as db:
        async with db.begin():
            row = XPostHistory(
                angle=angle,
                status=XPostStatus.draft,
                player_ids=player_ids,
                news_item_id=int(news_item_id) if news_item_id else None,
                headline=headline,
                tweets=tweet_objects,
                image_paths=image_paths,
                draft_dir=str(draft_dir),
                notes=notes,
            )
            db.add(row)
            await db.flush()
            row_id = row.id

    print(
        json.dumps(
            {
                "status": "saved",
                "x_post_history_id": row_id,
                "draft_dir": str(draft_dir),
                "tweet_count": len(tweets),
                "warnings": warnings,
            }
        )
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-dir", required=True)
    parser.add_argument("--tweets-file", required=True)
    parser.add_argument(
        "--allow-long",
        action="store_true",
        help="Save the draft even if a tweet exceeds 280 chars.",
    )
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)
    sys.exit(asyncio.run(_save(args)))


if __name__ == "__main__":
    main()
