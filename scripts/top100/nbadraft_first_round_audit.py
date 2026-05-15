"""Audit nbadraft.net's 2026 first round against the DraftGuru player database.

Reads scripts/top100/output/nbadraft_2026_first_round.csv (produced from a
Playwright scrape of nbadraft.net's 2026 mock), then for each pick looks up
matching rows in players_master + player_aliases, checks reference image and
stylized image asset status, and counts combine rows.

Output: scripts/top100/output/nbadraft_2026_audit.md

This script is intentionally read-only. It does not write to the database.
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import ssl
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.canonical_resolution_service import normalize_player_name  # noqa: E402

INPUT_CSV = REPO_ROOT / "scripts/top100/output/nbadraft_2026_first_round.csv"
OUTPUT_MD = REPO_ROOT / "scripts/top100/output/nbadraft_2026_audit.md"


def _load_dotenv(env_path: Path) -> None:
    """Lightweight .env reader. Sets only vars that aren't already set."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set; check .env")
    return url


async def _lookup_player(conn: Any, raw_name: str) -> list[dict[str, Any]]:
    """Find all players_master rows whose canonical or alias name matches."""
    key = normalize_player_name(raw_name)
    if not key:
        return []
    # We compare against a SQL-side normalized form of display_name and any
    # alias.full_name. We mirror normalize_player_name's behavior approximately:
    # lower, strip non-alphanumeric, collapse whitespace. It is intentionally
    # loose; we will visually verify matches in the report.
    sql = text(
        """
        WITH norm AS (
            SELECT
                pm.id AS player_id,
                pm.display_name,
                pm.slug,
                pm.draft_year,
                pm.school,
                pm.is_stub,
                pm.reference_image_url,
                pm.reference_image_s3_key,
                regexp_replace(lower(pm.display_name), '[^a-z0-9 ]', '', 'g') AS norm_display
            FROM players_master pm
        ),
        alias_norm AS (
            SELECT
                pa.player_id,
                regexp_replace(lower(pa.full_name), '[^a-z0-9 ]', '', 'g') AS norm_alias,
                pa.full_name AS alias_full_name
            FROM player_aliases pa
        )
        SELECT DISTINCT
            n.player_id,
            n.display_name,
            n.slug,
            n.draft_year,
            n.school,
            n.is_stub,
            n.reference_image_url,
            n.reference_image_s3_key,
            STRING_AGG(DISTINCT a.alias_full_name, ' || ') AS aliases
        FROM norm n
        LEFT JOIN alias_norm a ON a.player_id = n.player_id
        WHERE
            regexp_replace(n.norm_display, '\\s+', ' ', 'g') = :key
            OR EXISTS (
                SELECT 1 FROM alias_norm a2
                WHERE a2.player_id = n.player_id
                  AND regexp_replace(a2.norm_alias, '\\s+', ' ', 'g') = :key
            )
        GROUP BY n.player_id, n.display_name, n.slug, n.draft_year, n.school,
                 n.is_stub, n.reference_image_url, n.reference_image_s3_key
        ORDER BY n.player_id
        """
    )
    res = await conn.execute(sql, {"key": key})
    rows = res.mappings().all()
    return [dict(r) for r in rows]


async def _find_potential_duplicates(
    conn: Any,
    raw_name: str,
    matched_ids: set[int],
) -> list[dict[str, Any]]:
    """Find rows sharing the prospect's last name that look like duplicates.

    Heuristic: same normalized last name AND (draft_year between 2024 and 2028
    OR is_stub is true), excluding the rows already matched by exact lookup.
    This catches near-misses like 'Nate Ament' vs 'Nathaniel Ament' and stub
    rows like 'Peat' / 'Wagler' / 'Lendeborg'.
    """
    tokens = [t for t in re.sub(r"[^A-Za-z\s]", " ", raw_name).split() if t]
    while tokens and tokens[-1].lower() in {"jr", "sr", "ii", "iii", "iv"}:
        tokens.pop()
    if not tokens:
        return []
    last = tokens[-1].lower()

    sql = text(
        """
        SELECT pm.id, pm.display_name, pm.first_name, pm.last_name, pm.slug,
               pm.draft_year, pm.school, pm.is_stub,
               (SELECT STRING_AGG(full_name, ' || ') FROM player_aliases pa
                WHERE pa.player_id = pm.id) AS aliases
        FROM players_master pm
        WHERE lower(coalesce(pm.last_name, '')) = :ln
           OR lower(coalesce(pm.first_name, '')) = :ln
           OR lower(pm.display_name) = :ln
           OR lower(pm.display_name) LIKE :likepat
        """
    )
    res = await conn.execute(sql, {"ln": last, "likepat": f"% {last}"})
    candidates = [dict(r) for r in res.mappings().all()]
    out: list[dict[str, Any]] = []
    for c in candidates:
        if c["id"] in matched_ids:
            continue
        # Re-confirm normalized last name from display_name (or first_name when
        # display_name is just the surname, which happens for some stub rows).
        disp_tokens = [
            t for t in re.sub(r"[^A-Za-z\s]", " ", c["display_name"] or "").split() if t
        ]
        while disp_tokens and disp_tokens[-1].lower() in {
            "jr",
            "sr",
            "ii",
            "iii",
            "iv",
        }:
            disp_tokens.pop()
        disp_last = disp_tokens[-1].lower() if disp_tokens else ""
        # Stub rows sometimes store the surname in first_name with last_name NULL
        # and display_name == surname. Treat that as a match.
        stub_first_last = (c["first_name"] or "").lower()
        if disp_last != last and stub_first_last != last:
            continue
        year = c["draft_year"]
        if c["is_stub"] or (year is not None and 2024 <= year <= 2028):
            out.append(c)
    return out


async def _count_assets_and_combine(
    conn: Any, player_id: int
) -> dict[str, int | list[str]]:
    sql = text(
        """
        SELECT
            (SELECT COUNT(*) FROM player_image_assets
             WHERE player_id = :pid AND error_message IS NULL) AS image_asset_count,
            (SELECT STRING_AGG(DISTINCT s.style, ',')
             FROM player_image_assets a
             JOIN player_image_snapshots s ON s.id = a.snapshot_id
             WHERE a.player_id = :pid AND a.error_message IS NULL) AS styles,
            (SELECT COUNT(*) FROM combine_anthro WHERE player_id = :pid) AS anthro_rows,
            (SELECT COUNT(*) FROM combine_agility WHERE player_id = :pid) AS agility_rows,
            (SELECT COUNT(*) FROM combine_shooting_results WHERE player_id = :pid) AS shooting_rows
        """
    )
    res = await conn.execute(sql, {"pid": player_id})
    row = res.mappings().one()
    return dict(row)


def _summarize_match(
    matches: list[dict[str, Any]],
    csv_row: dict[str, str],
    extras_by_pid: dict[int, dict[str, Any]],
    potential_dupes: list[dict[str, Any]],
) -> str:
    """Render the per-prospect markdown section."""
    pick = csv_row["pick"]
    name = csv_row["player_name"]
    school = csv_row["school"]
    pos = csv_row["position"]
    klass = csv_row["class"]
    team = csv_row["team"]
    header = f"### {pick}. {name} — {school} ({pos}, {klass}) → {team}"

    if not matches:
        return (
            f"{header}\n\n"
            f"- ⚠️  **NO MATCH** in `players_master` (or via `player_aliases`). "
            f"Likely a missing prospect record.\n"
        )

    dupe_flag = (
        " ⚠️  **POSSIBLE DUPLICATE** (multiple matches)" if len(matches) > 1 else ""
    )
    lines = [f"{header}{dupe_flag}", ""]
    for m in matches:
        pid = m["player_id"]
        extras = extras_by_pid.get(pid, {})
        ref_url = m["reference_image_url"]
        ref_s3 = m["reference_image_s3_key"]
        ref_status = "✅" if (ref_url or ref_s3) else "❌"
        asset_count = extras.get("image_asset_count") or 0
        styles = extras.get("styles") or ""
        stylized_status = (
            f"✅ ({asset_count} asset(s), styles: {styles})"
            if asset_count
            else "❌ none"
        )
        combine_bits = []
        if extras.get("anthro_rows"):
            combine_bits.append(f"anthro={extras['anthro_rows']}")
        if extras.get("agility_rows"):
            combine_bits.append(f"agility={extras['agility_rows']}")
        if extras.get("shooting_rows"):
            combine_bits.append(f"shooting={extras['shooting_rows']}")
        combine_status = ", ".join(combine_bits) if combine_bits else "❌ none"

        school_match = ""
        db_school = (m.get("school") or "").strip()
        if db_school and db_school.lower() != school.strip().lower():
            school_match = f"  ⚠️ school mismatch (DB: `{db_school}`)"

        stub_flag = "  🔸stub" if m.get("is_stub") else ""

        lines.append(
            f"- **player_id={pid}** `{m['slug']}` — display: `{m['display_name']}`"
            f" (draft_year={m['draft_year']}){stub_flag}{school_match}"
        )
        if m.get("aliases"):
            lines.append(f"    - aliases: {m['aliases']}")
        lines.append(
            f"    - reference image: {ref_status} "
            f"{'(s3:' + ref_s3 + ')' if ref_s3 else ''}"
            f"{' (url:' + ref_url + ')' if ref_url else ''}"
        )
        lines.append(f"    - stylized images: {stylized_status}")
        lines.append(f"    - combine: {combine_status}")

    if potential_dupes:
        lines.append("")
        lines.append("  ⚠️ **Potential duplicate row(s) in DB** — review for merge:")
        for d in potential_dupes:
            stub_tag = " (stub)" if d["is_stub"] else ""
            lines.append(
                f"    - player_id={d['id']} `{d['slug']}` — `{d['display_name']}`"
                f" draft_year={d['draft_year']} school={d['school']!r}{stub_tag}"
            )
    lines.append("")
    return "\n".join(lines)


async def main() -> int:
    _load_dotenv(REPO_ROOT / ".env")
    db_url = _get_database_url()

    if not INPUT_CSV.exists():
        raise SystemExit(f"missing {INPUT_CSV}; run the scrape step first")

    with INPUT_CSV.open() as f:
        csv_rows = list(csv.DictReader(f))

    # Neon requires SSL.
    ssl_ctx = ssl.create_default_context()
    engine = create_async_engine(db_url, connect_args={"ssl": ssl_ctx})

    sections: list[str] = []
    summary_rows: list[
        tuple[str, str, str, str, str, str, str]
    ] = []  # pick, name, match_count, ref_image, stylized, combine, dupes

    async with engine.connect() as conn:
        for row in csv_rows:
            matches = await _lookup_player(conn, row["player_name"])
            extras_by_pid: dict[int, dict[str, Any]] = {}
            for m in matches:
                extras_by_pid[m["player_id"]] = await _count_assets_and_combine(
                    conn, m["player_id"]
                )

            matched_ids = {m["player_id"] for m in matches}
            potential_dupes = await _find_potential_duplicates(
                conn, row["player_name"], matched_ids
            )

            sections.append(
                _summarize_match(matches, row, extras_by_pid, potential_dupes)
            )

            # Build summary table row.
            dupe_cell = f"⚠️ {len(potential_dupes)}" if potential_dupes else "—"
            if not matches:
                summary_rows.append(
                    (row["pick"], row["player_name"], "0", "—", "—", "—", dupe_cell)
                )
            elif len(matches) == 1:
                m = matches[0]
                ex = extras_by_pid[m["player_id"]]
                ref_ok = bool(m["reference_image_url"] or m["reference_image_s3_key"])
                styl_ok = (ex.get("image_asset_count") or 0) > 0
                comb_ok = any(
                    ex.get(k) for k in ("anthro_rows", "agility_rows", "shooting_rows")
                )
                summary_rows.append(
                    (
                        row["pick"],
                        row["player_name"],
                        "1",
                        "✅" if ref_ok else "❌",
                        "✅" if styl_ok else "❌",
                        "✅" if comb_ok else "❌",
                        dupe_cell,
                    )
                )
            else:
                summary_rows.append(
                    (
                        row["pick"],
                        row["player_name"],
                        f"{len(matches)} ⚠️",
                        "?",
                        "?",
                        "?",
                        dupe_cell,
                    )
                )

    # Write report.
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_MD.open("w") as f:
        f.write("# 2026 First Round Audit — nbadraft.net\n\n")
        f.write(f"Source CSV: `{INPUT_CSV.relative_to(REPO_ROOT)}`  \n")
        f.write("Database: Neon dev branch (from `.env`)\n\n")
        f.write("## Summary\n\n")
        f.write(
            "| Pick | Player | DB matches | Ref image | Stylized | Combine | Dupes |\n"
        )
        f.write("|---:|---|---:|:---:|:---:|:---:|:---:|\n")
        for sr in summary_rows:
            f.write("| " + " | ".join(sr) + " |\n")
        f.write("\n## Per-prospect detail\n\n")
        for s in sections:
            f.write(s)
            f.write("\n")

    await engine.dispose()
    print(f"Wrote {OUTPUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
