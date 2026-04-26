"""Generate review artifacts for the 2026 Top 100 prospect refresh.

This script intentionally does not mutate application data. It freezes the
selected source board, canonicalizes affiliations for review, and optionally
checks a configured database for existing player records.

Usage:
    conda run -n draftguru python scripts/top100_refresh.py
    conda run -n draftguru python scripts/top100_refresh.py --date 2026-04-26
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import ssl
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


SOURCE_NAME = "The Athletic 2026 NBA Draft Top 100 via NBA.com"
SOURCE_URL = "https://www.nba.com/news/the-athletic-2026-nba-draft-top-100-prospects"
SOURCE_PUBLICATION_DATE = "2026-01-13"
SECONDARY_SOURCES = (
    "ESPN 2026 NBA draft big board rankings: Top 100 prospects, published April 2026; "
    "used only as secondary context because the full table is not available in this repo. "
    "Basketball Reference is the preferred downstream player-data source whenever a "
    "prospect has an available BBRef player page."
)
OUTPUT_DIR = Path("scraper/output")
SCHOOL_MAPPING_PATH = Path("scripts/data/school_mapping.json")
COLLEGE_SCHOOLS_PATH = Path("scripts/data/college_schools.json")


@dataclass(frozen=True)
class SourceRow:
    """One source-board prospect row."""

    source_rank: int
    source_name: str
    source_position: str
    source_affiliation: str
    source_age: str
    source_height: str


TOP100_ROWS: tuple[SourceRow, ...] = (
    SourceRow(1, "AJ Dybantsa", "Wing", "BYU", "19", "6-9"),
    SourceRow(2, "Darryn Peterson", "Guard", "Kansas", "19", "6-5"),
    SourceRow(3, "Cameron Boozer", "Forward", "Duke", "18", "6-9"),
    SourceRow(4, "Caleb Wilson", "Wing", "North Carolina", "19", "6-8"),
    SourceRow(5, "Kingston Flemings", "Guard", "Houston", "19", "6-4"),
    SourceRow(6, "Jayden Quaintance", "Big", "Kentucky", "19", "6-10"),
    SourceRow(7, "Koa Peat", "Forward", "Arizona", "19", "6-8"),
    SourceRow(8, "Yaxel Lendeborg", "Forward", "Michigan", "23", "6-8.5"),
    SourceRow(9, "Hannes Steinbach", "Big", "Washington", "20", "6-9.5"),
    SourceRow(10, "Mikel Brown Jr.", "Guard", "Louisville", "20", "6-4"),
    SourceRow(11, "Keaton Wagler", "Wing", "Illinois", "19", "6-6"),
    SourceRow(12, "Thomas Haugh", "Forward", "Florida", "23", "6-9"),
    SourceRow(13, "Labaron Philon", "Guard", "Alabama", "20", "6-2.75"),
    SourceRow(14, "Patrick Ngongba II", "Big", "Duke", "20", "6-11"),
    SourceRow(15, "Cameron Carr", "Wing", "Baylor", "21", "6-6"),
    SourceRow(16, "Darius Acuff Jr.", "Guard", "Arkansas", "19", "6-1"),
    SourceRow(17, "Braylon Mullins", "Wing", "Connecticut", "20", "6-5"),
    SourceRow(18, "Nate Ament", "Wing/Forward", "Tennessee", "19", "6-9"),
    SourceRow(19, "Joshua Jefferson", "Wing", "Iowa State", "22", "6-9"),
    SourceRow(20, "Christian Anderson", "Guard", "Texas Tech", "20", "6-2"),
    SourceRow(21, "Benett Stirtz", "Guard", "Iowa", "22", "6-4"),
    SourceRow(22, "Amari Allen", "Wing", "Alabama", "20", "6-8"),
    SourceRow(23, "Chris Cenac Jr.", "Big", "Houston", "19", "6-9.5"),
    SourceRow(24, "Neoklis Avdalas", "Wing", "Virginia Tech", "20", "6-7.5"),
    SourceRow(25, "Tounde Yessoufou", "Wing", "Baylor", "20", "6-4"),
    SourceRow(26, "Karim Lopez", "Forward", "NZ Breakers", "19", "6-8"),
    SourceRow(27, "Tyler Tanner", "Guard", "Vanderbilt", "20", "5-11"),
    SourceRow(28, "Henri Veesaar", "Big", "North Carolina", "22", "7-0"),
    SourceRow(29, "Dash Daniels", "Wing", "Melbourne United", "18", "6-6"),
    SourceRow(30, "Killyan Toure", "Guard", "Iowa State", "20", "6-3"),
    SourceRow(31, "Dailyn Swain", "Wing", "Texas", "20", "6-8"),
    SourceRow(32, "Aday Mara", "Big", "Michigan", "21", "7-3"),
    SourceRow(33, "Brayden Burries", "Wing", "Arizona", "20", "6-4"),
    SourceRow(34, "Alex Karaban", "Wing", "Connecticut", "23", "6-6.5"),
    SourceRow(35, "Richie Saunders", "Wing", "BYU", "24", "6-5"),
    SourceRow(36, "Isaiah Evans", "Wing", "Duke", "20", "6-6"),
    SourceRow(37, "Meleek Thomas", "Wing", "Arkansas", "19", "6-5"),
    SourceRow(38, "Braden Smith", "Guard", "Purdue", "22", "6-0"),
    SourceRow(39, "Ryan Conwell", "Guard", "Louisville", "22", "6-4"),
    SourceRow(40, "JoJo Tugler", "Big", "Houston", "21", "6-8"),
    SourceRow(41, "Sergio De Larrea", "Guard", "Valencia", "20", "6-5"),
    SourceRow(42, "Dame Sarr", "Wing", "Duke", "20", "6-6.5"),
    SourceRow(43, "Tarris Reed Jr.", "Big", "Connecticut", "22", "6-11"),
    SourceRow(44, "Alex Condon", "Big", "Florida", "21", "6-11.25"),
    SourceRow(45, "Tamin Lipsey", "Guard", "Iowa State", "23", "6-1"),
    SourceRow(46, "Milan Momcilovic", "Wing", "Iowa State", "21", "6-8"),
    SourceRow(47, "Milos Uzan", "Guard", "Houston", "23", "6-3.25"),
    SourceRow(48, "Johann Grunloh", "Big", "Virginia", "20", "6-11"),
    SourceRow(49, "Nate Bittle", "Big", "Oregon", "23", "6-11.25"),
    SourceRow(50, "Flory Bidunga", "Big", "Kansas", "21", "6-7"),
    SourceRow(51, "Braden Huff", "Big", "Gonzaga", "22", "6-10"),
    SourceRow(52, "Morez Johnson Jr.", "Big", "Michigan", "20", "6-9"),
    SourceRow(53, "Bruce Thornton", "Guard", "Ohio State", "22", "6-2"),
    SourceRow(54, "Juke Harris", "Wing", "Wake Forest", "20", "6-7"),
    SourceRow(55, "Jaden Bradley", "Guard", "Arizona", "22", "6-3"),
    SourceRow(56, "Zuby Ejiofor", "Big", "St. John’s", "22", "6-8"),
    SourceRow(57, "Silas DeMary Jr.", "Guard", "Connecticut", "22", "6-4"),
    SourceRow(58, "MoMo Faye", "Big", "Paris Basketball", "21", "6-9"),
    SourceRow(59, "Adam Atamna", "Guard", "ASVEL", "18", "6-5"),
    SourceRow(60, "Tomislav Ivisic", "Big", "Illinois", "22", "7-1"),
    SourceRow(61, "Kwame Evans Jr.", "Wing", "Oregon", "21", "6-9"),
    SourceRow(62, "Paul McNeil", "Wing", "NC State", "20", "6-5"),
    SourceRow(63, "Pryce Sandfort", "Wing", "Nebraska", "22", "6-6"),
    SourceRow(64, "Nolan Winter", "Big", "Wisconsin", "21", "7-0"),
    SourceRow(65, "JT Toppin", "Big", "Texas Tech", "21", "6-7"),
    SourceRow(66, "D’Shayne Montgomery", "Wing", "Dayton", "22", "6-4"),
    SourceRow(67, "Blue Cain", "Guard", "Georgia", "21", "6-5"),
    SourceRow(68, "Andrej Stojakovic", "Wing", "Illinois", "21", "6-7"),
    SourceRow(69, "Tahaad Pettiford", "Guard", "Auburn", "20", "6-0.25"),
    SourceRow(70, "K.J. Lewis", "Guard", "Georgetown", "21", "6-4"),
    SourceRow(71, "Baba Miller", "Wing", "Cincinnati", "22", "6-11"),
    SourceRow(72, "Ja’Kobi Gillespie", "Guard", "Tennessee", "22", "6-1"),
    SourceRow(73, "Motiejus Krivas", "Big", "Arizona", "21", "7-2"),
    SourceRow(74, "Tyler Bilodeau", "Forward", "UCLA", "22", "6-9"),
    SourceRow(75, "Darrion Williams", "Forward", "NC State", "23", "6-4.5"),
    SourceRow(76, "Nick Martinelli", "Wing", "Northwestern", "22", "6-6"),
    SourceRow(77, "Cade Tyson", "Wing", "Minnesota", "22", "6-7"),
    SourceRow(78, "William Kyle", "Big", "Syracuse", "22", "6-9"),
    SourceRow(79, "Keyshawn Hall", "Forward", "Auburn", "23", "6-7"),
    SourceRow(80, "Emanuel Sharp", "Guard", "Houston", "22", "6-3"),
    SourceRow(81, "Nick Boyd", "Guard", "Wisconsin", "25", "6-3"),
    SourceRow(82, "Donovan Dent", "Guard", "UCLA", "22", "6-2"),
    SourceRow(83, "Maliq Brown", "Big", "Duke", "22", "6-9"),
    SourceRow(84, "Trey Kaufman-Renn", "Big", "Purdue", "23", "6-9"),
    SourceRow(85, "Tucker DeVries", "Wing", "Indiana", "23", "6-7"),
    SourceRow(86, "Joshua Dix", "Wing", "Creighton", "22", "6-6"),
    SourceRow(87, "John Blackwell", "Guard", "Wisconsin", "21", "6-4"),
    SourceRow(88, "Otega Oweh", "Wing", "Kentucky", "23", "6-4.25"),
    SourceRow(89, "Elijah Mahi", "Forward", "Santa Clara", "22", "6-7"),
    SourceRow(90, "Lamar Wilkerson", "Guard", "Indiana", "24", "6-4"),
    SourceRow(91, "Kylan Boswell", "Guard", "Illinois", "21", "6-2"),
    SourceRow(92, "Dillon Mitchell", "Wing", "St. John’s", "22", "6-8"),
    SourceRow(93, "Ben Henshall", "Guard", "Perth Wildcats", "22", "6-5.5"),
    SourceRow(94, "Trevon Brazile", "Big", "Arkansas", "23", "6-9"),
    SourceRow(95, "Michael Ruzic", "Big", "Joventut", "19", "6-11"),
    SourceRow(96, "Michael Ajayi", "Wing", "Butler", "23", "6-7"),
    SourceRow(97, "Oscar Cluff", "Big", "Purdue", "24", "6-11"),
    SourceRow(98, "Jordan Burks", "Forward", "UCF", "21", "6-9"),
    SourceRow(99, "Fletcher Loyer", "Guard", "Purdue", "22", "6-4"),
    SourceRow(100, "Bryce Hopkins", "Forward", "St. John’s", "23", "6-7"),
)


def normalize_name(name: str) -> str:
    """Return a comparison key that handles punctuation and suffix variants."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace("’", "'").replace(".", " ")
    tokens = re.findall(r"[a-z0-9]+", ascii_name.lower())
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    return " ".join(token for token in tokens if token not in suffixes)


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def load_school_mapping() -> dict[str, str | None]:
    """Load the reviewed school/club mapping."""
    mapping = _load_json(SCHOOL_MAPPING_PATH)
    if not isinstance(mapping, dict):
        raise TypeError(f"{SCHOOL_MAPPING_PATH} must contain a JSON object")
    return {
        str(raw): None if canonical is None else str(canonical)
        for raw, canonical in mapping.items()
    }


def load_college_school_names() -> set[str]:
    """Load canonical college school names."""
    rows = _load_json(COLLEGE_SCHOOLS_PATH)
    if not isinstance(rows, list):
        raise TypeError(f"{COLLEGE_SCHOOLS_PATH} must contain a JSON array")
    return {str(row["name"]) for row in rows if isinstance(row, dict) and "name" in row}


def resolve_affiliation(
    raw_affiliation: str,
    mapping: dict[str, str | None],
    college_school_names: set[str],
) -> tuple[str, str, str, str]:
    """Resolve a raw source affiliation into review fields."""
    normalized_raw = raw_affiliation.replace("’", "'")
    if raw_affiliation in mapping:
        canonical = mapping[raw_affiliation]
        if canonical is None:
            return (
                "",
                "professional_or_international",
                "mapped_intentional_non_college",
                "",
            )
        return canonical, "college", "mapped", ""
    if normalized_raw in mapping:
        canonical = mapping[normalized_raw]
        if canonical is None:
            return (
                "",
                "professional_or_international",
                "mapped_intentional_non_college",
                "",
            )
        return canonical, "college", "mapped_punctuation_normalized", ""
    if raw_affiliation in college_school_names:
        return raw_affiliation, "college", "canonical_school_name", ""
    if normalized_raw in college_school_names:
        return (
            normalized_raw,
            "college",
            "canonical_school_name_punctuation_normalized",
            "",
        )
    return "", "unknown", "needs_review", "Add raw affiliation to school_mapping.json"


def write_source_snapshot(output_date: date) -> Path:
    """Write the immutable source snapshot CSV."""
    path = OUTPUT_DIR / f"top100_source_snapshot_{output_date.isoformat()}.csv"
    fields = [
        "source_rank",
        "source_name",
        "source_affiliation",
        "source_position",
        "source_height",
        "source_age",
        "source_url",
        "source_publication_date",
        "retrieval_date",
        "source_name_full",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in TOP100_ROWS:
            writer.writerow(
                {
                    "source_rank": row.source_rank,
                    "source_name": row.source_name,
                    "source_affiliation": row.source_affiliation,
                    "source_position": row.source_position,
                    "source_height": row.source_height,
                    "source_age": row.source_age,
                    "source_url": SOURCE_URL,
                    "source_publication_date": SOURCE_PUBLICATION_DATE,
                    "retrieval_date": output_date.isoformat(),
                    "source_name_full": SOURCE_NAME,
                }
            )
    return path


def write_school_review(output_date: date) -> Path:
    """Write affiliation resolution review CSV."""
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    path = OUTPUT_DIR / f"school_resolution_review_{output_date.isoformat()}.csv"
    fields = [
        "source_rank",
        "source_name",
        "raw_affiliation",
        "canonical_affiliation",
        "affiliation_type",
        "resolution_status",
        "review_note",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in TOP100_ROWS:
            canonical, affiliation_type, status, note = resolve_affiliation(
                row.source_affiliation, mapping, college_school_names
            )
            writer.writerow(
                {
                    "source_rank": row.source_rank,
                    "source_name": row.source_name,
                    "raw_affiliation": row.source_affiliation,
                    "canonical_affiliation": canonical,
                    "affiliation_type": affiliation_type,
                    "resolution_status": status,
                    "review_note": note,
                }
            )
    return path


def _prepare_connection(url: str) -> tuple[str, dict[str, ssl.SSLContext]]:
    """Strip Neon query params that asyncpg does not accept."""
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]

    split = urlsplit(url)
    pairs = parse_qsl(split.query, keep_blank_values=True)
    sslmode = None
    filtered = []
    for key, value in pairs:
        if key == "sslmode":
            sslmode = value
        elif key != "channel_binding":
            filtered.append((key, value))

    cleaned = urlunsplit(split._replace(query=urlencode(filtered))).rstrip("?")
    connect_args: dict[str, ssl.SSLContext] = {}
    if sslmode and sslmode.lower() == "require":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = context
    return cleaned, connect_args


async def fetch_player_candidates(
    database_url: str | None,
) -> tuple[dict[str, list[dict[str, str | int | None]]], str]:
    """Fetch players and aliases grouped by normalized name."""
    if not database_url:
        return {}, "DATABASE_URL not set; player decisions require DB-backed review."

    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    candidates: dict[str, list[dict[str, str | int | None]]] = {}
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        pm.id,
                        pm.display_name,
                        pm.school,
                        pm.school_raw,
                        pm.draft_year,
                        pm.is_stub,
                        NULL::text AS alias_name
                    FROM players_master pm
                    WHERE pm.draft_year = 2026
                       OR pm.display_name = ANY(:source_names)
                    UNION ALL
                    SELECT
                        pm.id,
                        pm.display_name,
                        pm.school,
                        pm.school_raw,
                        pm.draft_year,
                        pm.is_stub,
                        pa.full_name AS alias_name
                    FROM player_aliases pa
                    JOIN players_master pm ON pm.id = pa.player_id
                    WHERE pm.draft_year = 2026
                       OR pa.full_name = ANY(:source_names)
                    """
                ),
                {"source_names": [row.source_name for row in TOP100_ROWS]},
            )
            for row in result.mappings():
                candidate = {
                    "player_id": row["id"],
                    "display_name": row["display_name"],
                    "school": row["school"],
                    "school_raw": row["school_raw"],
                    "draft_year": row["draft_year"],
                    "is_stub": row["is_stub"],
                    "matched_name": row["alias_name"] or row["display_name"],
                }
                key = normalize_name(str(candidate["matched_name"] or ""))
                candidates.setdefault(key, []).append(candidate)
    except Exception as exc:
        return {}, f"Database candidate lookup failed: {exc}"
    finally:
        await engine.dispose()
    return candidates, ""


async def write_player_plan(output_date: date, database_url: str | None) -> Path:
    """Write source-row player resolution plan CSV."""
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    candidates_by_name, db_note = await fetch_player_candidates(database_url)
    path = OUTPUT_DIR / f"player_resolution_plan_{output_date.isoformat()}.csv"
    fields = [
        "source_rank",
        "source_name",
        "normalized_source_name",
        "raw_affiliation",
        "canonical_affiliation",
        "resolution_action",
        "confidence",
        "canonical_player_id",
        "candidate_player_ids",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in TOP100_ROWS:
            canonical, _, affiliation_status, _ = resolve_affiliation(
                row.source_affiliation, mapping, college_school_names
            )
            normalized_name = normalize_name(row.source_name)
            candidates = candidates_by_name.get(normalized_name, [])
            unique_candidates = {
                player_id: candidate
                for candidate in candidates
                if isinstance(player_id := candidate["player_id"], int)
            }
            player_ids = [str(player_id) for player_id in sorted(unique_candidates)]

            if db_note:
                action = "needs_manual_review"
                confidence = "low"
                player_id = ""
                reason = db_note
            elif len(unique_candidates) == 1:
                candidate = next(iter(unique_candidates.values()))
                school_values = {candidate["school"], candidate["school_raw"]}
                school_match = canonical and canonical in school_values
                action = "matched"
                confidence = "high" if school_match or not canonical else "medium"
                player_id = str(candidate["player_id"])
                reason = "Normalized name matched one draft-year player"
                if school_match:
                    reason += " with matching canonical affiliation"
            elif len(unique_candidates) > 1:
                action = "merge_required"
                confidence = "medium"
                player_id = ""
                reason = (
                    "Multiple normalized-name candidates; review duplicate/alias merge"
                )
            elif affiliation_status == "needs_review":
                action = "needs_manual_review"
                confidence = "low"
                player_id = ""
                reason = "Affiliation is unmapped; resolve school/club before creating player"
            else:
                action = "create_stub"
                confidence = "medium"
                player_id = ""
                reason = "No existing draft-year player or alias candidate found"

            writer.writerow(
                {
                    "source_rank": row.source_rank,
                    "source_name": row.source_name,
                    "normalized_source_name": normalized_name,
                    "raw_affiliation": row.source_affiliation,
                    "canonical_affiliation": canonical,
                    "resolution_action": action,
                    "confidence": confidence,
                    "canonical_player_id": player_id,
                    "candidate_player_ids": "|".join(player_ids),
                    "reason": reason,
                }
            )
    return path


def write_run_note(output_date: date, paths: list[Path]) -> Path:
    """Write a short run note for the frozen source snapshot."""
    path = OUTPUT_DIR / f"top100_refresh_run_note_{output_date.isoformat()}.md"
    content = f"""# Top 100 Refresh Run Note - {output_date.isoformat()}

## Primary Source

- Source: {SOURCE_NAME}
- URL: {SOURCE_URL}
- Publication date: {SOURCE_PUBLICATION_DATE}
- Retrieval date: {output_date.isoformat()}

## Secondary Context

- {SECONDARY_SOURCES}

## Generated Artifacts

{chr(10).join(f"- `{artifact}`" for artifact in paths)}

## Known Limitations

- The primary source is an accessible NBA.com republication of The Athletic's first
  2026 draft-cycle Top 100 board. A newer ESPN board was visible in search snippets
  but was not used as the frozen source because the complete structured table was
  not available for review in this repo.
- Ages are source-provided as whole years on 2026 draft day.
- Heights and affiliations preserve source spelling in the immutable snapshot.
- Professional and international clubs are intentionally mapped as non-college
  affiliations with blank canonical college names in the school review artifact.
- Player resolution is DB-backed when `DATABASE_URL` is set. Without DB access,
  the generated plan marks rows for manual review instead of creating duplicate
  stubs.
- Use Basketball Reference as the preferred identity, bio, and statistical data
  source whenever a prospect has an available BBRef page. For prospects without a
  BBRef page, use reviewed official school/team roster pages before other sources.
"""
    path.write_text(content, encoding="utf-8")
    return path


async def generate_artifacts(output_date: date, database_url: str | None) -> list[Path]:
    """Generate all Session 1 Top 100 artifacts."""
    if len(TOP100_ROWS) != 100:
        raise ValueError(f"Expected 100 source rows, found {len(TOP100_ROWS)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = write_source_snapshot(output_date)
    school_path = write_school_review(output_date)
    player_path = await write_player_plan(output_date, database_url)
    run_note_path = write_run_note(
        output_date, [snapshot_path, school_path, player_path]
    )
    return [snapshot_path, school_path, player_path, run_note_path]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Generate Top 100 refresh artifacts")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Artifact date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Optional DB URL for player resolution checks",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    args = parse_args()
    try:
        paths = asyncio.run(generate_artifacts(args.date, args.database_url))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
