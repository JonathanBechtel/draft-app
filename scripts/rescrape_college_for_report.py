"""Re-scrape college stats for the players repaired by fix_namesake_contamination.

After ``fix_namesake_contamination.py --apply`` deletes the contaminated
college-stats rows of each ``resolvable`` record, this driver re-scrapes the
(now single, correct) Basketball-Reference page for exactly those player ids,
reusing the existing ``run_college_stats_sweep`` service one player at a time.

Usage::

    scripts/with-db-env.sh conda run -n draftguru python scripts/rescrape_college_for_report.py
    scripts/with-db-env.sh conda run -n draftguru python scripts/rescrape_college_for_report.py --report path.json --throttle 3
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.college_stats_service import run_college_stats_sweep  # noqa: E402
from app.utils.db_async import SessionLocal, dispose_engine  # noqa: E402

DEFAULT_REPORT = REPO_ROOT / "scripts" / "data" / "namesake_contamination_report.json"


def _resolvable_ids(report_path: Path) -> list[int]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    return sorted(
        c["player_id"] for c in data["cases"] if c["classification"] == "resolvable"
    )


async def _main(args: argparse.Namespace) -> int:
    ids = _resolvable_ids(args.report)
    print(f"Re-scraping college stats for {len(ids)} repaired players...")
    attempted = scraped = skipped = failed = seasons = 0
    errors: list[str] = []
    try:
        for i, pid in enumerate(ids, 1):
            res = await run_college_stats_sweep(
                SessionLocal,
                player_id=pid,
                throttle=args.throttle,
                refresh=True,
            )
            attempted += res.players_attempted
            scraped += res.players_scraped
            skipped += res.players_skipped
            failed += res.players_failed
            seasons += res.seasons_upserted
            errors.extend(res.errors)
            if i % 25 == 0 or i == len(ids):
                print(f"  {i}/{len(ids)} processed — {seasons} seasons so far")
    finally:
        await dispose_engine()

    print(
        f"\nDone: {attempted} attempted, {scraped} scraped, {skipped} skipped, "
        f"{failed} failed, {seasons} seasons upserted"
    )
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors[:50]:
            print(f"  - {e}")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument(
        "--throttle", type=float, default=3.0, help="Seconds between requests"
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
