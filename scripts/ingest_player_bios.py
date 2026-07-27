"""Operator CLI for ingesting a scraped BBR bio CSV into the database.

The ingest itself lives in ``app.services.player_bio.ingest`` so the shipped
roster cron (``app/cli/summer_league_roster_runner.py``) and this CLI share one
implementation; this module only handles argument parsing.
"""

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.player_bio.ingest import ingest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest BBR bio CSV into database")
    parser.add_argument("--file", required=True, type=str, help="Path to bbio CSV file")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="data/scraper-cache/players",
        help="Directory containing cached player HTML for snapshots",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not commit DB changes"
    )
    parser.add_argument(
        "--overwrite-master",
        action="store_true",
        help="Allow overwriting master fields",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Create players_master rows for unmatched records",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--fix-ambiguities",
        type=str,
        default=None,
        help="Path to JSON mapping of slug -> player_id for manual resolutions",
    )
    parser.add_argument(
        "--summer-league-year",
        type=int,
        default=None,
        help=(
            "Restrict ingestion to the Summer League rostered cohort for this"
            " competition year (players without a bbref id are written to"
            " bbio_manual_review.json instead of being ingested)"
        ),
    )
    parser.add_argument(
        "--summer-league-league-id",
        type=str,
        default=None,
        help="Restrict ingestion to the Summer League cohort for this NBA.com LeagueID",
    )
    parser.add_argument(
        "--summer-league-venue-slug",
        type=str,
        default=None,
        help="Restrict ingestion to the Summer League cohort for this venue slug",
    )
    args = parser.parse_args()

    asyncio.run(
        ingest(
            csv_path=Path(args.file),
            cache_dir=Path(args.cache_dir),
            dry_run=args.dry_run,
            verbose=args.verbose,
            overwrite_master=args.overwrite_master,
            fix_ambiguities_path=Path(args.fix_ambiguities)
            if args.fix_ambiguities
            else None,
            create_missing=args.create_missing,
            summer_league_year=args.summer_league_year,
            summer_league_league_id=args.summer_league_league_id,
            summer_league_venue_slug=args.summer_league_venue_slug,
        )
    )


if __name__ == "__main__":
    main()
