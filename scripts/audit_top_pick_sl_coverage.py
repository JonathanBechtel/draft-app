#!/usr/bin/env python
r"""Audit Summer League log coverage for the top draft picks of each class.

For every first-round pick in a draft-year range this reports whether the player
has aggregated SL data and, when they don't, *why* — distinguishing a genuine
entity-resolution gap (orphaned/misresolved logs we can recover) from a
sub-threshold season, a roster-only appearance, or the player simply never
having played Summer League. Built for issue #495 so top-pick coverage
regressions stay visible.

Read-only (SELECT only). Runs against whatever ``--url-env`` points at.

Usage::

    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/audit_top_pick_sl_coverage.py --years 2015-2025 --top 14

    # audit all of round 1 against the prod read branch
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/audit_top_pick_sl_coverage.py --top 30 --url-env EXPLAIN_DATABASE_URL
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.player_mention_service import _normalized_name_key
from app.utils.db_async import _prepare_asyncpg_connection

load_dotenv()

# Summers with no NBA Summer League — context only (a rookie's first SL is
# normally the summer of their draft year; 2020 draftees debuted in the 2021 SL).
CANCELLED_SL_YEARS = {2011: "lockout", 2020: "COVID"}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--years", default="2015-2025", help="inclusive range, e.g. 2015-2025"
    )
    ap.add_argument("--top", type=int, default=14, help="round-1 picks with pick<=N")
    ap.add_argument("--url-env", default="DATABASE_URL")
    args = ap.parse_args()

    y0, y1 = (int(x) for x in args.years.split("-"))
    top_n = args.top
    raw_url = os.environ.get(args.url_env)
    if not raw_url:
        print(f"ERROR: {args.url_env} not set", file=sys.stderr)
        sys.exit(1)

    # Repo helper strips libpq-only query args (sslmode, channel_binding) that
    # asyncpg rejects and normalizes the scheme.
    normalized_url, connect_args = _prepare_asyncpg_connection(raw_url)
    engine = create_async_engine(
        normalized_url, connect_args=connect_args, pool_pre_ping=True
    )
    async with engine.connect() as conn:
        picks = (
            await conn.execute(
                text(
                    """
                    SELECT id, display_name, draft_year, draft_pick
                    FROM players_master
                    WHERE draft_round = 1
                      AND draft_pick BETWEEN 1 AND :n
                      AND draft_year BETWEEN :y0 AND :y1
                    ORDER BY draft_year, draft_pick
                    """
                ),
                {"n": top_n, "y0": y0, "y1": y1},
            )
        ).all()
        if not picks:
            print("No picks matched the filter.")
            await engine.dispose()
            return
        ids = [p.id for p in picks]

        seasons: dict[int, int] = {
            int(r[0]): int(r[1])
            for r in (
                await conn.execute(
                    text(
                        "SELECT player_id, COUNT(*) FROM summer_league_player_seasons "
                        "WHERE player_id = ANY(:ids) GROUP BY player_id"
                    ),
                    {"ids": ids},
                )
            ).all()
        }
        logs: dict[int, int] = {
            int(r[0]): int(r[1])
            for r in (
                await conn.execute(
                    text(
                        "SELECT player_id, COUNT(*) FROM summer_league_player_game_logs "
                        "WHERE player_id = ANY(:ids) GROUP BY player_id"
                    ),
                    {"ids": ids},
                )
            ).all()
        }

        # True gaps (no season AND no resolved logs) get a name-based candidate
        # lookup to tell recoverable orphans from genuine non-participation.
        name_to_pick: dict[str, list] = {}
        for p in picks:
            if seasons.get(p.id) or logs.get(p.id):
                continue
            key = _normalized_name_key(p.display_name or "")
            if key:
                name_to_pick.setdefault(key, []).append(p)

        candidates: dict[str, list] = {}
        if name_to_pick:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT sp.normalized_name,
                               sp.raw_player_name,
                               sp.canonical_player_id,
                               COUNT(pgl.id) AS logs
                        FROM summer_league_source_players sp
                        LEFT JOIN summer_league_player_game_logs pgl
                               ON pgl.source_player_id = sp.id
                        WHERE sp.normalized_name = ANY(:names)
                        GROUP BY sp.id, sp.normalized_name, sp.raw_player_name,
                                 sp.canonical_player_id
                        """
                    ),
                    {"names": list(name_to_pick.keys())},
                )
            ).all()
            for r in rows:
                candidates.setdefault(r.normalized_name, []).append(r)

    await engine.dispose()

    counts = {
        k: 0 for k in ("OK", "THIN", "RECOVERABLE", "MISRESOLVED", "ROSTER", "ABSENT")
    }
    lines = []
    for p in picks:
        yr, pick = p.draft_year, p.draft_pick
        note = (
            f"  [{CANCELLED_SL_YEARS[yr]} draft yr]" if yr in CANCELLED_SL_YEARS else ""
        )
        if seasons.get(p.id):
            counts["OK"] += 1
            verdict = f"OK           seasons={seasons[p.id]} logs={logs.get(p.id, 0)}"
        elif logs.get(p.id):
            counts["THIN"] += 1
            verdict = f"THIN         resolved logs={logs[p.id]}, no qualifying season"
        else:
            key = _normalized_name_key(p.display_name or "")
            cands = candidates.get(key, [])
            orphan = [c for c in cands if c.canonical_player_id is None and c.logs]
            misres = [
                c for c in cands if c.canonical_player_id not in (None, p.id) and c.logs
            ]
            roster = [c for c in cands if not c.logs]
            if orphan:
                counts["RECOVERABLE"] += 1
                verdict = (
                    f"RECOVERABLE  unresolved source(s)={len(orphan)} "
                    f"orphan_logs={sum(c.logs for c in orphan)} [{orphan[0].raw_player_name!r}]"
                )
            elif misres:
                counts["MISRESOLVED"] += 1
                verdict = (
                    f"MISRESOLVED  logs={misres[0].logs} under canonical_id="
                    f"{misres[0].canonical_player_id} [{misres[0].raw_player_name!r}]"
                )
            elif roster:
                counts["ROSTER"] += 1
                verdict = f"ROSTER-ONLY  source exists, 0 box logs [{roster[0].raw_player_name!r}]"
            else:
                counts["ABSENT"] += 1
                verdict = (
                    "ABSENT       no source by exact name (variant, or never played SL)"
                )
        lines.append(
            f"  {yr} R1.{pick:<2} {(p.display_name or '?'):<24} {verdict}{note}"
        )

    total = len(picks)
    host = raw_url.split("@")[-1].split("/")[0]
    print(f"\nTop-{top_n} R1 picks, {y0}-{y1}  ({args.url_env}: {host})")
    print(f"  players audited        : {total}")
    print(f"  OK (aggregated season) : {counts['OK']}  ({counts['OK'] / total:.0%})")
    print(f"  THIN (resolved, <floor): {counts['THIN']}")
    print(f"  RECOVERABLE (orphaned) : {counts['RECOVERABLE']}  <- resolution wins")
    print(f"  MISRESOLVED (dup/other): {counts['MISRESOLVED']}")
    print(f"  ROSTER-ONLY (0 logs)   : {counts['ROSTER']}")
    print(
        f"  ABSENT (no source row) : {counts['ABSENT']}  <- name variant OR never played"
    )
    print()
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
