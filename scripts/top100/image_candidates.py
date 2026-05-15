"""Review and apply Top 100 likeness reference-image candidates.

The candidate list is intentionally curated from official roster/profile pages
or clearly identified Wikimedia Commons files. The default mode writes the
review CSV only. Use ``--execute`` after reviewing the artifact.

Usage:
    conda run -n draftguru python scripts/top100/image_candidates.py --date 2026-04-27
    conda run -n draftguru python scripts/top100/image_candidates.py --execute --date 2026-04-27
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.top100.refresh import OUTPUT_DIR, _prepare_connection  # noqa: E402


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """Reviewed reference-image candidate for one Top 100 prospect."""

    source_rank: int
    player_id: int
    source_name: str
    source_page_url: str
    image_url: str
    source_type: str
    confidence: str
    review_status: str
    review_note: str


IMAGE_CANDIDATES: tuple[ImageCandidate, ...] = (
    ImageCandidate(
        15,
        5519,
        "Cameron Carr",
        "https://baylorbears.com/sports/mens-basketball/roster/cameron-carr/14165",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fbaylorbears.com%2Fimages%2F2025%2F10%2F14%2FCarr_Cameron.png&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Replaces unrelated Wikimedia PDF URL with Baylor roster headshot.",
    ),
    ImageCandidate(
        30,
        6063,
        "Killyan Toure",
        "https://cyclones.com/sports/mens-basketball/roster/killyan-toure/13870",
        "https://cyclones.com/images/2025/7/10/Killyan_Toure_2025-26_Headshot.jpg",
        "official_roster_headshot",
        "high",
        "accepted",
        "Iowa State roster JSON-LD image for the matched player.",
    ),
    ImageCandidate(
        38,
        5652,
        "Braden Smith",
        "https://purduesports.com/sports/mens-basketball/roster/braden-smith/14425",
        "https://purduesports.com/imgproxy/kj3obyXMV_pCmjC2-SL1D8C5uA37GbG91M1PDDThDZU/rs:fit:1980:0:0:0/g:ce:0:0/q:90/aHR0cHM6Ly9zdG9yYWdlLmdvb2dsZWFwaXMuY29tL3B1cmR1ZXNwb3J0cy1jb20tcHJvZC8yMDI1LzA5LzEwL0xaUUJpUDlvVU9XaUVKR09TZDZuazdmUW81OHNkOW8zRHBBM1JCdVouanBn.jpg",
        "official_roster_image",
        "high",
        "accepted",
        "Replaces unrelated Wikimedia PDF URL with Purdue roster image.",
    ),
    ImageCandidate(
        51,
        6064,
        "Braden Huff",
        "https://gozags.com/sports/mens-basketball/roster/braden-huff/5748",
        "https://gozags.com/images/2025/10/6/HUFF_BRADEN.JPG",
        "official_roster_headshot",
        "high",
        "accepted",
        "Gonzaga roster headshot for current player page.",
    ),
    ImageCandidate(
        58,
        6065,
        "MoMo Faye",
        "https://parisbasketball.com/en/equipe/first-team/",
        "https://parisbasketball.com/wp-content/uploads/2025/09/Momo-1.png",
        "official_team_profile_image",
        "high",
        "accepted",
        "Paris Basketball first-team profile image for Mouhamed Faye.",
    ),
    ImageCandidate(
        59,
        6066,
        "Adam Atamna",
        "https://commons.wikimedia.org/wiki/File:Adam_Atamna_2_ASVEL_Basket_Euroleague_20251106_(1).jpg",
        "https://upload.wikimedia.org/wikipedia/commons/a/a9/Adam_Atamna_2_ASVEL_Basket_Euroleague_20251106_%281%29.jpg",
        "wikimedia_commons_identified_player",
        "high",
        "accepted",
        "Commons file title and description identify Adam Atamna with ASVEL.",
    ),
    ImageCandidate(
        64,
        6067,
        "Nolan Winter",
        "https://uwbadgers.com/sports/mens-basketball/roster/winter-nolan/14417",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fuwbadgers.com%2Fimages%2F2025%2F8%2F13%2F34788942-download.jpeg&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Wisconsin roster headshot alt text identifies Nolan Winter.",
    ),
    ImageCandidate(
        66,
        6068,
        "D’Shayne Montgomery",
        "https://daytonflyers.com/sports/mens-basketball/roster/de-shayne-montgomery/15464",
        "https://daytonflyers.com/images/2025/10/18/2_DeShayne-Montgomery_EGcWr.jpg",
        "official_roster_headshot",
        "high",
        "accepted",
        "Dayton roster image for De'Shayne Montgomery.",
    ),
    ImageCandidate(
        70,
        6070,
        "K.J. Lewis",
        "https://guhoyas.com/sports/mens-basketball/roster/kj-lewis/17403",
        "https://guhoyas.com/images/2025/10/20/LEWIS_KJ_-_2526_Jersey_No_Smile.jpg",
        "official_roster_headshot",
        "high",
        "accepted",
        "Georgetown roster image for KJ Lewis.",
    ),
    ImageCandidate(
        78,
        5798,
        "William Kyle",
        "https://cuse.com/sports/mens-basketball/roster/william-kyle-iii/24614",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fsuathletics.com%2Fimages%2F2025%2F9%2F18%2F42_-_William_Kyle-6A.jpg&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Replaces unrelated Wikimedia PDF URL with Syracuse roster headshot.",
    ),
    ImageCandidate(
        80,
        5500,
        "Emanuel Sharp",
        "https://uhcougars.com/sports/mens-basketball/roster/sharpemanuel/9540",
        "https://uhcougars.com/images/2025/8/30/Sharp_Emanuel_rX3OH.jpg",
        "official_roster_image",
        "high",
        "accepted",
        "Replaces unrelated Wikimedia PDF URL with Houston roster image.",
    ),
    ImageCandidate(
        81,
        6071,
        "Nick Boyd",
        "https://uwbadgers.com/sports/mens-basketball/roster/nick-boyd/14419",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fuwbadgers.com%2Fimages%2F2025%2F8%2F21%2F34788928_MBB-20250807-HEADSHOT-02-Nick_Boyd_JPG_Erik_Role_20250808_012726.JPG&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Wisconsin roster headshot alt text identifies Nick Boyd.",
    ),
    ImageCandidate(
        84,
        6072,
        "Trey Kaufman-Renn",
        "https://purduesports.com/sports/mens-basketball/roster/trey-kaufman-renn/14422",
        "https://purduesports.com/imgproxy/th6JKlVUb4Df2p712l-jGlgBOGjuVsu1QSmbtFohKgE/rs:fit:1980:0:0:0/g:ce:0:0/q:90/aHR0cHM6Ly9zdG9yYWdlLmdvb2dsZWFwaXMuY29tL3B1cmR1ZXNwb3J0cy1jb20tcHJvZC8yMDI1LzA5LzEwL1ZKZ1hETlkybjNGTDFSMExYYTk0eW5zWnRjSFR1ZW8xMWxxYWNGdVIuanBn.jpg",
        "official_roster_image",
        "high",
        "accepted",
        "Purdue roster image for Trey Kaufman-Renn.",
    ),
    ImageCandidate(
        86,
        6073,
        "Joshua Dix",
        "https://gocreighton.com/sports/mens-basketball/roster/josh-dix/8578",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fcreighton.sidearmsports.com%2Fimages%2F2025%2F6%2F20%2F06-04_JoshDixHeadshot-1.jpg&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Creighton roster headshot for Josh Dix.",
    ),
    ImageCandidate(
        87,
        5618,
        "John Blackwell",
        "https://uwbadgers.com/sports/mens-basketball/roster/john-blackwell/14412",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fuwbadgers.com%2Fimages%2F2025%2F8%2F21%2F34788941_MBB-20250807-HEADSHOT-25-John_Blackwell_JPG_Erik_Role_20250808_012753.JPG&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Replaces unrelated Wikimedia PDF URL with Wisconsin roster headshot.",
    ),
    ImageCandidate(
        89,
        6074,
        "Elijah Mahi",
        "https://santaclarabroncos.com/sports/mens-basketball/roster/elijah-mahi/9121",
        "https://santaclarabroncos.com/images/2025/9/25/mahiDJR13648.JPG",
        "official_roster_image",
        "high",
        "accepted",
        "Santa Clara roster image for Elijah Mahi.",
    ),
    ImageCandidate(
        90,
        6075,
        "Lamar Wilkerson",
        "https://iuhoosiers.com/sports/mens-basketball/roster/wilkerson-lamar/20211",
        "https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fiuhoosiers.com%2Fimages%2F2025%2F10%2F3%2F3_Lamar_Wilkerson_2klbn.jpg&width=180&height=270&type=webp",
        "official_roster_headshot",
        "high",
        "accepted",
        "Indiana roster headshot for Lamar Wilkerson.",
    ),
    ImageCandidate(
        98,
        6076,
        "Jordan Burks",
        "https://ucfknights.com/sports/mens-basketball/roster/player/jordan-burks",
        "https://ucfknights.com/imgproxy/3u8Wisf9u1OvoYPXmjwLQHyBRHLEzQc8k0lu-teUc1U/rs:fit:1980:0:0:0/g:ce:0:0/q:90/aHR0cHM6Ly9zdG9yYWdlLmdvb2dsZWFwaXMuY29tL3VjZmtuaWdodHMtY29tLXByb2QvMjAyNS8xMC8xMy9GUDFhRkpVR3A0RGFRV2d4MU9sb2x2eW9yY1kwOVB5aFI4RVdETGtWLmpwZw.jpg",
        "official_roster_image",
        "high",
        "accepted",
        "UCF roster image for Jordan Burks.",
    ),
    ImageCandidate(
        99,
        6077,
        "Fletcher Loyer",
        "https://purduesports.com/sports/mens-basketball/roster/fletcher-loyer/14424",
        "https://purduesports.com/imgproxy/2D4Dfn-YtTSa8hmyUEccbTEWVMb4IOgDOV72ps49gKw/rs:fit:1980:0:0:0/g:ce:0:0/q:90/aHR0cHM6Ly9zdG9yYWdlLmdvb2dsZWFwaXMuY29tL3B1cmR1ZXNwb3J0cy1jb20tcHJvZC8yMDI1LzA5LzEwL3QzR3RyV0hBZmNCMzRFdWY5Y2VJOEVFOG9aRTZHandndXdlc3NGTUMuanBn.jpg",
        "official_roster_image",
        "high",
        "accepted",
        "Purdue roster image for Fletcher Loyer.",
    ),
    # --- 2026-05-15 nbadraft.net first-round audit: replace incorrect refs ---
    ImageCandidate(
        19,
        5563,
        "Allen Graves",
        "https://santaclarabroncos.com/sports/mens-basketball/roster/allen--graves/9118",
        "https://dbukjj6eu5tsf.cloudfront.net/sidearm.sites/santaclara.sidearmsports.com/images/2025/9/25/gravesDJR13641.JPG",
        "official_roster_headshot",
        "high",
        "accepted",
        "Replaces unrelated 1839 Wikimedia evangelist PDF with Santa Clara 2025-26 roster headshot.",
    ),
    ImageCandidate(
        30,
        5679,
        "Tyler Tanner",
        "https://vucommodores.com/roster/tyler-tanner/",
        "https://vucommodores.com/wp-content/uploads/2025/07/3_Tanner_Tyler-420x640.jpg",
        "official_roster_headshot",
        "high",
        "accepted",
        "Replaces wrong-person Wikimedia 2018 'Evergreen' photo with Vanderbilt 2025-26 roster headshot (jersey #3).",
    ),
)


def write_candidates(output_date: date) -> Path:
    """Write reviewed image candidates to CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"image_candidates_{output_date.isoformat()}.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(IMAGE_CANDIDATES[0])))
        writer.writeheader()
        for candidate in IMAGE_CANDIDATES:
            writer.writerow(asdict(candidate))
    return path


async def apply_candidates(database_url: str) -> None:
    """Apply accepted candidates to players_master.reference_image_url."""
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with engine.begin() as conn:
            for candidate in IMAGE_CANDIDATES:
                if candidate.review_status != "accepted":
                    continue
                result = await conn.execute(
                    text(
                        """
                        UPDATE players_master
                        SET reference_image_url = :reference_image_url,
                            updated_at = :updated_at
                        WHERE id = :player_id
                        """
                    ),
                    {
                        "player_id": candidate.player_id,
                        "reference_image_url": candidate.image_url,
                        "updated_at": now,
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"Expected to update one row for player_id={candidate.player_id}, "
                        f"updated {result.rowcount}"
                    )
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    """Parse CLI args."""
    parser = argparse.ArgumentParser(
        description="Review/apply Top 100 image candidates"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Apply accepted candidates"
    )
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    load_dotenv()
    args = parse_args()
    path = write_candidates(args.date)
    if args.execute:
        database_url = args.database_url or os.getenv("DATABASE_URL")
        if not database_url:
            print("ERROR: DATABASE_URL not set", file=sys.stderr)
            sys.exit(1)
        asyncio.run(apply_candidates(database_url))
    print(path)


if __name__ == "__main__":
    main()
