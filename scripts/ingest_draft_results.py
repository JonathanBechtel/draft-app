#!/usr/bin/env python
r"""Ingest actual draft-night picks into ``draft_results``.

Reads lines of the form ``<overall_pick> <player name> <TEAM_ABBR>`` — the
shape you can paste straight from a draft tracker as the picks come in::

    1  Cooper Flagg    DAL
    2  Dylan Harper    SAS
    3  VJ Edgecombe    PHI

The trailing all-caps token (2–4 letters) is read as the team abbreviation; the
pick number is the leading integer; everything between is the player name. Lines
without a trailing team token still ingest (team left blank for later fixup).

Players are resolved against existing ``PlayerMaster`` records via the shared
matcher (no stubs created) so a typo or genuinely new name is reported for
manual review rather than minting a junk player. Re-running is idempotent: each
``(draft_year, overall_pick)`` row is upserted, so you can paste the growing
list repeatedly through the night.

Usage::

    # from a file
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/ingest_draft_results.py --file picks.txt

    # or pipe a pasted block on stdin
    pbpaste | scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/ingest_draft_results.py

    # preview without writing
    python scripts/ingest_draft_results.py --file picks.txt --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.schemas.draft_results import DraftResult
from app.schemas.nba_teams import NbaTeam
from app.services.player_mention_service import (
    build_player_name_lookup,
    find_existing_player,
)

load_dotenv()

_TEAM_TOKEN = re.compile(r"^[A-Z]{2,4}$")
_LINE = re.compile(r"^\s*#?\s*(\d+)[.):\s]\s*(.+?)\s*$")


@dataclass(slots=True)
class ParsedPick:
    """One parsed input line before DB resolution."""

    overall_pick: int
    player_name: str
    team_abbr: Optional[str]


def parse_lines(text: str) -> list[ParsedPick]:
    """Parse pasted ``pick name TEAM`` lines into ``ParsedPick`` rows.

    Blank lines and lines that do not start with a pick number are skipped, so
    headers or stray commentary in a pasted block are tolerated.
    """
    picks: list[ParsedPick] = []
    for raw in text.splitlines():
        line = raw.strip()
        # Skip blanks and comment lines. "#1" is still pick numbering, so a
        # line only counts as a comment when "#" is not followed by a digit.
        if not line or (line.startswith("#") and not line[1:2].isdigit()):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        overall = int(m.group(1))
        rest = m.group(2).strip()
        tokens = rest.split()
        team: Optional[str] = None
        if len(tokens) >= 2 and _TEAM_TOKEN.match(tokens[-1]):
            team = tokens[-1]
            tokens = tokens[:-1]
        name = " ".join(tokens).strip()
        if not name:
            continue
        picks.append(ParsedPick(overall_pick=overall, player_name=name, team_abbr=team))
    return picks


async def _upsert_pick(
    session: AsyncSession,
    *,
    draft_year: int,
    parsed: ParsedPick,
    player_id: Optional[int],
    team_id: Optional[int],
    resolution_method: str,
    source: str,
) -> bool:
    """Insert or update one ``draft_results`` row. Returns True if newly inserted."""
    round_no = 1 if parsed.overall_pick <= 30 else 2
    round_pick = parsed.overall_pick if round_no == 1 else parsed.overall_pick - 30

    existing = await session.scalar(
        select(DraftResult)  # type: ignore[call-overload]
        .where(DraftResult.draft_year == draft_year)  # type: ignore[arg-type]
        .where(DraftResult.overall_pick == parsed.overall_pick)  # type: ignore[arg-type]
    )
    if existing is not None:
        existing.round = round_no
        existing.round_pick = round_pick
        existing.player_id = player_id
        existing.team_id = team_id
        existing.raw_player_name = parsed.player_name
        existing.raw_team = parsed.team_abbr
        existing.resolution_method = resolution_method
        existing.source = source
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        return False

    session.add(
        DraftResult(
            draft_year=draft_year,
            overall_pick=parsed.overall_pick,
            round=round_no,
            round_pick=round_pick,
            player_id=player_id,
            team_id=team_id,
            raw_player_name=parsed.player_name,
            raw_team=parsed.team_abbr,
            resolution_method=resolution_method,
            source=source,
            picked_at=datetime.utcnow(),
        )
    )
    return True


async def ingest(text: str, *, draft_year: int, source: str, dry_run: bool) -> None:
    """Resolve and persist the parsed picks, reporting unresolved rows."""
    parsed = parse_lines(text)
    if not parsed:
        print("No pick lines found in input.", file=sys.stderr)
        sys.exit(1)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    inserted = updated = 0
    unresolved_players: list[tuple[int, str]] = []
    ambiguous_players: list[tuple[int, str]] = []
    unresolved_teams: list[tuple[int, str]] = []

    async with session_factory() as session:
        teams = (await session.execute(select(NbaTeam))).scalars().all()
        team_id_by_abbr = {t.abbreviation: t.id for t in teams if t.id is not None}
        lookup = await build_player_name_lookup(session)

        for p in parsed:
            match, ambiguous = await find_existing_player(
                session, p.player_name, lookup=lookup
            )
            if match is not None:
                player_id: Optional[int] = match.player_id
                method = "matched"
            else:
                player_id = None
                method = "unresolved"
                if ambiguous:
                    ambiguous_players.append((p.overall_pick, p.player_name))
                else:
                    unresolved_players.append((p.overall_pick, p.player_name))

            team_id = team_id_by_abbr.get(p.team_abbr) if p.team_abbr else None
            if p.team_abbr and team_id is None:
                unresolved_teams.append((p.overall_pick, p.team_abbr))

            was_new = await _upsert_pick(
                session,
                draft_year=draft_year,
                parsed=p,
                player_id=player_id,
                team_id=team_id,
                resolution_method=method,
                source=source,
            )
            inserted += int(was_new)
            updated += int(not was_new)

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    await engine.dispose()

    verb = "Would ingest" if dry_run else "Ingested"
    print(f"{verb} {len(parsed)} picks ({inserted} new, {updated} updated).")
    if unresolved_players:
        print("\nUNRESOLVED players (no match — fix name or add player):")
        for pick, name in unresolved_players:
            print(f"  #{pick:>2}  {name}")
    if ambiguous_players:
        print("\nAMBIGUOUS players (matched multiple — resolve manually):")
        for pick, name in ambiguous_players:
            print(f"  #{pick:>2}  {name}")
    if unresolved_teams:
        print("\nUNKNOWN team abbreviations:")
        for pick, abbr in unresolved_teams:
            print(f"  #{pick:>2}  {abbr}")
    if not (unresolved_players or ambiguous_players or unresolved_teams):
        print("All picks resolved cleanly.")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        help="Path to a file of pick lines; reads stdin when omitted.",
    )
    parser.add_argument("--draft-year", type=int, default=2026)
    parser.add_argument(
        "--source",
        default="manual",
        help="Provenance label stored on each row (e.g. 'manual', 'espn').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and resolve but roll back without writing.",
    )
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    asyncio.run(
        ingest(
            text,
            draft_year=args.draft_year,
            source=args.source,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
