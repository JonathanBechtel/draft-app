"""Archive raw NBA Stats Summer League snapshots to S3.

Run:

    conda run -n draftguru python scripts/archive_summer_league_raw.py \
      --raw-root data/raw/nba_stats/summer_league \
      --s3-prefix s3://<bucket>/raw/nba_stats/summer_league \
      --dry-run
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app.services.s3_client import S3Client
from app.services.summer_league.archive import (
    archive_summer_league_raw,
    parse_s3_archive_prefix,
    summarize_report,
    write_archive_report,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/raw/nba_stats/summer_league"),
        help="Local Summer League raw snapshot root",
    )
    parser.add_argument(
        "--s3-prefix",
        required=True,
        help="Destination prefix like s3://bucket/raw/nba_stats/summer_league",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan archive keys without S3 reads or writes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upload even when destination metadata appears unchanged",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional JSON report output path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the archive CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        destination = parse_s3_archive_prefix(args.s3_prefix)
    except ValueError as exc:
        parser.error(str(exc))

    s3_client = None if args.dry_run else S3Client().client
    try:
        report = archive_summer_league_raw(
            raw_root=args.raw_root,
            s3_prefix=destination,
            dry_run=args.dry_run,
            force=args.force,
            s3_client=s3_client,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.report_path is not None:
        write_archive_report(report, args.report_path)

    print(summarize_report(report), flush=True)
    return 1 if report.error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
