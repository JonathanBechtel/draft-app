#!/usr/bin/env python
"""Curate the Phase 0 archive manifest into a clean ingestion candidate list.

Read-only, no network. Reads ``docs/consensus_backfill_manifest.json`` and
applies a tighter NBA-draft-big-board filter than the audit's broad title
heuristic: a post is a candidate iff its title contains "big board" AND is
free AND does not match a noise token (college recruiting, JUCO, dynasty
fantasy ranks, NBA rookie/sophomore power rankings, podcasts/announcements).

Writes ``docs/consensus_backfill_candidates.json`` (the curated set the
ingestion harness will consume) and prints a per-source list plus a
month-by-month coverage grid so we can see what "weekly over the cycle"
actually resolves to.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass

MANIFEST = "docs/consensus_backfill_manifest.json"

_NOISE = re.compile(
    r"\b(recruit|recruitment|juco|dynasty (rank|basketball)|top\s*450|top\s*60|"
    r"rookie ranking|sophomore|college (basketball )?teams|power ranking|"
    r"podcast|partnership|announc|megadrop|points\)|categories\)|new chapter)\b",
    re.IGNORECASE,
)
# Titles that contain "big board" but describe methodology, not an actual board.
_NOT_A_BOARD = re.compile(r"from scratch", re.IGNORECASE)
# In May 2026 the archives also carry way-too-early 2027 (and stale 2025/2024)
# mocks. Exclude other-cycle titles so we only ingest 2026 boards; the
# post-bulk draft_year audit is the backstop for anything that slips through.
_OTHER_CYCLE = re.compile(r"\b(2024|2025|2027|2028)\b")


@dataclass(frozen=True)
class KindConfig:
    """Per-kind curation config (require term, output path, cycle guard)."""

    require: re.Pattern[str]
    out: str
    drop_other_cycle: bool


_KIND_CONFIG: dict[str, KindConfig] = {
    "big_board": KindConfig(
        require=re.compile(r"\bbig board\b", re.IGNORECASE),
        out="docs/consensus_backfill_candidates.json",
        drop_other_cycle=False,
    ),
    "mock_draft": KindConfig(
        require=re.compile(r"\bmock draft\b", re.IGNORECASE),
        out="docs/consensus_backfill_mock_candidates.json",
        drop_other_cycle=True,
    ),
}


def is_candidate(title: str, is_free: bool, cfg: KindConfig) -> bool:
    if not is_free:
        return False
    if not cfg.require.search(title):
        return False
    if _NOISE.search(title):
        return False
    if _NOT_A_BOARD.search(title):
        return False
    if cfg.drop_other_cycle and _OTHER_CYCLE.search(title):
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=sorted(_KIND_CONFIG), default="big_board")
    args = ap.parse_args()
    cfg = _KIND_CONFIG[args.kind]
    OUT = cfg.out
    m = json.load(open(MANIFEST))
    candidates: list[dict] = []
    flagged: list[dict] = []
    for s in m["sources"]:
        for p in s["posts"]:
            title = p["title"]
            if is_candidate(title, p["is_free"], cfg):
                candidates.append(p)
            elif (
                p["is_free"] and cfg.require.search(title) and not _NOISE.search(title)
            ):
                # Matches the kind term + free but dropped by a guard
                # (methodology / other-cycle) — surface for a human eyeball
                # rather than silently discarding.
                flagged.append(p)

    candidates.sort(key=lambda p: (p["source_name"].lower(), p["post_date"] or ""))
    json.dump(
        {"count": len(candidates), "candidates": candidates},
        open(OUT, "w"),
        indent=2,
    )

    by_source: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_source[c["source_name"]].append(c)

    print(f"=== curated NBA-draft {args.kind} candidates: {len(candidates)} ===\n")
    for name in sorted(by_source, key=str.lower):
        rows = by_source[name]
        print(f"### {name}  ({len(rows)})")
        for c in rows:
            print(f"  {(c['post_date'] or '')[:10]}  {c['title'][:70]}")
        print()

    # Month-by-month coverage: how many distinct sources have a board <= month-end.
    print("=== monthly coverage (distinct sources with a board published in month) ===")
    months: dict[str, set[str]] = defaultdict(set)
    for c in candidates:
        if c["post_date"]:
            months[c["post_date"][:7]].add(c["source_name"])
    for ym in sorted(months):
        srcs = sorted(months[ym])
        print(f"  {ym}: {len(srcs):>2}  {', '.join(srcs)}")

    if flagged:
        print(
            "\n=== FLAGGED for manual review (title has 'big board' but looks non-board) ==="
        )
        for f in flagged:
            print(
                f"  {f['source_name']}  {(f['post_date'] or '')[:10]}  {f['title'][:70]}"
            )

    print(f"\nwrote -> {OUT}")


if __name__ == "__main__":
    main()
