"""Operator CLI for scraping Basketball-Reference player bios.

Scrapes index pages (``--letters``/``--all``), explicit slugs
(``--extra-slugs``/``--extra-slugs-file``), and/or a Summer League cohort
(``--summer-league-year`` and friends), then writes one ``bbio_*.csv`` for
``scripts/ingest_player_bios.py`` to load.

The scraping and parsing logic lives in ``app.services.player_bio`` so the
shipped roster cron (``app/cli/summer_league_roster_runner.py``) and this CLI
share one implementation; this module only handles argument parsing, target
selection, and CSV output.
"""

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.player_bio.bbref_scrape import scrape_letters  # noqa: E402
from app.services.summer_league.bio_enrichment_targets import (  # noqa: E402
    select_bio_enrichment_targets,
)
from app.utils.db_async import SessionLocal  # noqa: E402


async def _resolve_summer_league_slugs(
    year: Optional[int],
    league_id: Optional[str],
    venue_slug: Optional[str],
) -> Tuple[List[str], List[int]]:
    """Resolve bbref slugs + manual-review player_ids for an SL cohort scope.

    Args:
        year: Optional Summer League competition year filter.
        league_id: Optional NBA.com ``LeagueID`` filter.
        venue_slug: Optional venue slug filter.

    Returns:
        A tuple of ``(slugs, manual_review_player_ids)`` where ``slugs`` are
        the bbref slugs for cohort players with a resolved bbref external id,
        and ``manual_review_player_ids`` are cohort players with no bbref id.
    """
    async with SessionLocal() as db:
        targets = await select_bio_enrichment_targets(
            db, year=year, league_id=league_id, venue_slug=venue_slug
        )
    return sorted(targets.slugs), sorted(targets.manual_review_player_ids)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Basketball-Reference player bios (index + player pages)"
    )
    parser.add_argument(
        "--letters",
        type=str,
        default="",
        help="Comma-separated letters to scrape (a-z)",
    )
    parser.add_argument(
        "--all", dest="all_letters", action="store_true", help="Scrape all letters a-z"
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="data/scraper-output",
        help="Output directory for CSV",
    )
    parser.add_argument(
        "--throttle", type=float, default=3.0, help="Seconds to sleep between requests"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Request timeout seconds"
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logs")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-download pages even if cache files exist",
    )
    parser.add_argument(
        "--from-index-dir",
        type=str,
        default=None,
        help="Directory containing players_{letter}.html files to parse instead of fetching",
    )
    parser.add_argument(
        "--from-index-file",
        type=str,
        default=None,
        help="Single index HTML file to parse (e.g., index_page_example.html)",
    )
    parser.add_argument(
        "--from-player-dir",
        type=str,
        default=None,
        help="Directory containing individual player HTML files (slug.html) to parse instead of fetching",
    )
    parser.add_argument(
        "--from-player-file",
        type=str,
        default=None,
        help="Single player HTML file to parse for a sample player (e.g., player_page_example.html)",
    )
    parser.add_argument(
        "--extra-slugs",
        type=str,
        default="",
        help="Comma-separated list of BRef slugs to fetch even if the index pages omit them",
    )
    parser.add_argument(
        "--extra-slugs-file",
        type=str,
        default=None,
        help="Path to a newline-delimited file of additional BRef slugs to scrape",
    )
    parser.add_argument(
        "--summer-league-year",
        type=int,
        default=None,
        help=(
            "Add BBRef slugs for the Summer League rostered cohort's"
            " bbref-having players for this competition year (players"
            " without a bbref id are written to bbio_manual_review.json"
            " instead of being scraped)"
        ),
    )
    parser.add_argument(
        "--summer-league-league-id",
        type=str,
        default=None,
        help="Scope the Summer League cohort to this NBA.com LeagueID",
    )
    parser.add_argument(
        "--summer-league-venue-slug",
        type=str,
        default=None,
        help="Scope the Summer League cohort to this venue slug",
    )

    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    extra_slugs: List[str] = []
    if args.extra_slugs:
        extra_slugs.extend(
            [
                slug.strip().lower()
                for slug in args.extra_slugs.split(",")
                if slug.strip()
            ]
        )
    if args.extra_slugs_file:
        slug_path = Path(args.extra_slugs_file)
        if not slug_path.exists():
            raise SystemExit(f"extra slugs file not found: {slug_path}")
        extra_slugs.extend(
            [
                line.strip().lower()
                for line in slug_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )
    # preserve order while deduplicating
    seen_extra: Set[str] = set()
    deduped_extra: List[str] = []
    for slug in extra_slugs:
        if slug in seen_extra:
            continue
        seen_extra.add(slug)
        deduped_extra.append(slug)
    extra_slugs = deduped_extra

    summer_league_scoped = (
        args.summer_league_year is not None
        or args.summer_league_league_id is not None
        or args.summer_league_venue_slug is not None
    )
    if summer_league_scoped:
        cohort_slugs, manual_review = asyncio.run(
            _resolve_summer_league_slugs(
                year=args.summer_league_year,
                league_id=args.summer_league_league_id,
                venue_slug=args.summer_league_venue_slug,
            )
        )
        for slug in cohort_slugs:
            if slug in seen_extra:
                continue
            seen_extra.add(slug)
            extra_slugs.append(slug)
        if manual_review:
            (out_dir / "bbio_manual_review.json").write_text(
                json.dumps(manual_review, indent=2), encoding="utf-8"
            )
        if args.verbose:
            print(
                f"[info] SL-cohort scope: {len(cohort_slugs)} bbref-having"
                f" target slug(s), {len(manual_review)} flagged for manual review"
            )

    letters: List[str]
    if args.all_letters:
        letters = [chr(c) for c in range(ord("a"), ord("z") + 1)]
    else:
        letters = [
            token.strip().lower() for token in args.letters.split(",") if token.strip()
        ]
        if not letters and not extra_slugs:
            raise SystemExit(
                "Must pass --letters/--all, provide --extra-slugs, or scope"
                " with --summer-league-year/--summer-league-league-id"
            )
    from_index_dir = Path(args.from_index_dir) if args.from_index_dir else None
    from_index_file = Path(args.from_index_file) if args.from_index_file else None
    from_player_dir = Path(args.from_player_dir) if args.from_player_dir else None
    from_player_file = Path(args.from_player_file) if args.from_player_file else None

    rows = scrape_letters(
        letters=letters,
        out_dir=out_dir,
        throttle=args.throttle,
        from_index_dir=from_index_dir,
        from_player_dir=from_player_dir,
        from_index_file=from_index_file,
        from_player_file=from_player_file,
        verbose=args.verbose,
        timeout=args.timeout,
        refresh=args.refresh,
        extra_slugs=extra_slugs,
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    if args.all_letters:
        scope = "all"
    elif letters:
        scope = "".join(letters)
    else:
        scope = "custom"
    out_path = out_dir / f"bbio_{scope}_{ts}.csv"
    # Write CSV
    fieldnames = [
        "slug",
        "url",
        "full_name",
        "birth_date",
        "birth_city",
        "birth_state_province",
        "birth_country",
        "shoots",
        "school",
        "high_school",
        "draft_year",
        "draft_round",
        "draft_pick",
        "draft_team",
        "nba_debut_date",
        "nba_debut_season",
        "is_active_nba",
        "current_team",
        "nba_last_season",
        "position",
        "height_in",
        "weight_lb",
        "social_x_handle",
        "social_x_url",
        "social_instagram_handle",
        "social_instagram_url",
        "source_url",
        "scraped_at",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            # Ensure only expected keys
            row = {k: r.get(k) for k in fieldnames}
            writer.writerow(row)

    print(f"[info] wrote {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
