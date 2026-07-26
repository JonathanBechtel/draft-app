#!/usr/bin/env python
r"""Phase 0 coverage audit for the Competition Context Explorer (issue #616).

Read-only, repeatable inventory of which Summer League years, competitions,
metrics, and field-composition attributes can be published *honestly* before
the Competition Context profile schema (#606) and aggregations (#617) are
designed. It answers, per year and per competition:

* normalized competitions and final/scheduled/in-progress/postponed/canceled
  games;
* final games with two complete team-box rows;
* final games with parsed shot-chart and play-by-play inputs;
* resolved and unresolved *appeared* player identities (positive minutes in a
  final game — DNP shells excluded, per the implementation contract §5);
* availability of event-time draft, age, position, and origin inputs;
* how many competition and all-competitions season profiles would *certify*
  each v1 metric from the implementation-contract registry (§4).

It never mutates raw or derived Summer League facts.

Contract source of truth:
``docs/plans/competition-context-explorer-implementation-contract.md``.

Usage::

    # Against dev (default DATABASE_URL)
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/audit_summer_league_environment_coverage.py \\
        --out-dir docs/plans --write-markdown

    # Against the prod-like read branch
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/audit_summer_league_environment_coverage.py \\
        --url-env EXPLAIN_DATABASE_URL --out-dir /tmp/audit
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from sqlalchemy import text

# ---------------------------------------------------------------------------
# Metric registry (mirrors implementation-contract §4). Each v1 metric names
# the *input source* whose per-game coverage certifies it. The audit does not
# recompute metric values; it reports whether every eligible final game carries
# the required input so #617 knows which profiles can publish which metric.
# ---------------------------------------------------------------------------

# Input-source kinds. Certifiability of a metric is derived from the coverage of
# its source across a scope's eligible (final) games.
SOURCE_BOX = "box"  # two complete team-box rows per final game
SOURCE_SHOT = "shot"  # parsed shot-chart input per final game
SOURCE_SCORE = "score"  # a known home/away final score per game
SOURCE_OT = "ot_state"  # a known overtime state (status_text present)
SOURCE_PBP = "pbp"  # parsed play-by-play input (informational in v1)


@dataclass(frozen=True)
class MetricSpec:
    """One v1 environment/landscape metric and the input it needs."""

    key: str
    source: str
    section: str
    note: str = ""


METRIC_REGISTRY: tuple[MetricSpec, ...] = (
    MetricSpec("points_per_team_game", SOURCE_BOX, "environment"),
    MetricSpec("estimated_possessions", SOURCE_BOX, "environment"),
    MetricSpec("pace_per_48", SOURCE_BOX, "environment"),
    MetricSpec("offensive_rating", SOURCE_BOX, "environment"),
    MetricSpec("three_attempt_share", SOURCE_BOX, "environment"),
    MetricSpec("three_fg_pct", SOURCE_BOX, "environment"),
    MetricSpec("free_throw_rate", SOURCE_BOX, "environment"),
    MetricSpec("offensive_rebound_rate", SOURCE_BOX, "environment"),
    MetricSpec("turnover_rate", SOURCE_BOX, "environment"),
    MetricSpec("assisted_fg_rate", SOURCE_BOX, "environment"),
    MetricSpec("rim_attempt_share", SOURCE_SHOT, "environment"),
    MetricSpec("rim_fg_pct", SOURCE_SHOT, "environment"),
    MetricSpec("average_score_margin", SOURCE_SCORE, "environment"),
    MetricSpec("close_game_share", SOURCE_SCORE, "environment"),
    MetricSpec(
        "overtime_share",
        SOURCE_OT,
        "environment",
        note="confidence: derived from raw status_text, not a normalized flag",
    ),
    MetricSpec("team_ortg_iqr", SOURCE_BOX, "landscape"),
    MetricSpec("top_decile_minutes_share", SOURCE_BOX, "landscape"),
    MetricSpec("top_decile_points_share", SOURCE_BOX, "landscape"),
)

# Field-composition attributes evaluated over resolved appeared players.
ATTRIBUTE_KEYS: tuple[str, ...] = ("draft", "age", "position", "origin")

COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_UNAVAILABLE = "unavailable"

# Summer League game statuses we count (mirrors SummerLeagueGameStatus values).
GAME_STATUSES: tuple[str, ...] = (
    "scheduled",
    "in_progress",
    "final",
    "postponed",
    "canceled",
    "unknown",
)

# The calculation version stamped into the report; bump when the audit logic
# changes so downstream tickets can tell reports apart.
AUDIT_VERSION = "1"


# ---------------------------------------------------------------------------
# Pure logic (no DB) — unit tested.
# ---------------------------------------------------------------------------


def is_appearance(minutes_seconds: Optional[int]) -> bool:
    """Whether a player-game log counts as an *appearance* (positive minutes).

    DNP shells (null or zero minutes) never count as appearances or feed field
    composition (implementation contract §5).
    """
    return minutes_seconds is not None and minutes_seconds > 0


def classify_coverage(final_games: int, covered_games: int) -> str:
    """Classify a metric input's coverage over a scope's eligible final games.

    * ``unavailable`` — no eligible final games, or none carry the input;
    * ``complete`` — every eligible final game carries the input;
    * ``partial`` — some but not all eligible final games carry the input.

    Partial is never coerced to zero and never certifies the metric.
    """
    if final_games <= 0 or covered_games <= 0:
        return COVERAGE_UNAVAILABLE
    if covered_games >= final_games:
        return COVERAGE_COMPLETE
    return COVERAGE_PARTIAL


@dataclass
class AttributeCoverage:
    """Known/unknown/total counts for one field-composition attribute."""

    known: int = 0
    total: int = 0

    @property
    def unknown(self) -> int:
        """Resolved appeared players missing this attribute."""
        return max(self.total - self.known, 0)

    def as_dict(self) -> dict[str, int]:
        """Serialize known/unknown/total."""
        return {"known": self.known, "unknown": self.unknown, "total": self.total}


@dataclass
class CoverageRecord:
    """Coverage inventory for one scope (a competition or a season rollup)."""

    scope_kind: str  # "competition" | "season"
    scope_key: str  # "competition:<id>" | "season:<year>"
    year: int
    competition_id: Optional[int] = None
    venue_slug: Optional[str] = None
    display_name: str = ""
    starts_on: Optional[str] = None
    ends_on: Optional[str] = None
    included_competitions: int = 1

    status_counts: dict[str, int] = field(
        default_factory=lambda: {s: 0 for s in GAME_STATUSES}
    )
    final_games: int = 0
    box_complete_games: int = 0
    shot_covered_games: int = 0
    pbp_covered_games: int = 0
    games_with_score: int = 0
    games_with_known_ot: int = 0
    overtime_games: int = 0

    appeared_canonical: int = 0
    appeared_unresolved: int = 0
    appeared_player_games: int = 0
    resolved_appeared: int = 0

    attributes: dict[str, AttributeCoverage] = field(
        default_factory=lambda: {k: AttributeCoverage() for k in ATTRIBUTE_KEYS}
    )

    def source_covered(self, source: str) -> int:
        """Games covered for a given metric input source."""
        return {
            SOURCE_BOX: self.box_complete_games,
            SOURCE_SHOT: self.shot_covered_games,
            SOURCE_PBP: self.pbp_covered_games,
            SOURCE_SCORE: self.games_with_score,
            SOURCE_OT: self.games_with_known_ot,
        }[source]

    def metric_certifiability(self) -> dict[str, str]:
        """Per-metric coverage verdict over this scope's eligible final games."""
        return {
            spec.key: classify_coverage(
                self.final_games, self.source_covered(spec.source)
            )
            for spec in METRIC_REGISTRY
        }

    def to_flat_dict(self) -> dict[str, Any]:
        """Flatten to a single JSON/CSV record."""
        out: dict[str, Any] = {
            "scope_kind": self.scope_kind,
            "scope_key": self.scope_key,
            "year": self.year,
            "competition_id": self.competition_id,
            "venue_slug": self.venue_slug,
            "display_name": self.display_name,
            "starts_on": self.starts_on,
            "ends_on": self.ends_on,
            "included_competitions": self.included_competitions,
            "final_games": self.final_games,
            "box_complete_games": self.box_complete_games,
            "shot_covered_games": self.shot_covered_games,
            "pbp_covered_games": self.pbp_covered_games,
            "games_with_score": self.games_with_score,
            "games_with_known_ot": self.games_with_known_ot,
            "overtime_games": self.overtime_games,
            "appeared_canonical": self.appeared_canonical,
            "appeared_unresolved": self.appeared_unresolved,
            "appeared_player_games": self.appeared_player_games,
            "resolved_appeared": self.resolved_appeared,
        }
        for status in GAME_STATUSES:
            out[f"games_{status}"] = self.status_counts.get(status, 0)
        for key in ATTRIBUTE_KEYS:
            cov = self.attributes[key]
            out[f"attr_{key}_known"] = cov.known
            out[f"attr_{key}_unknown"] = cov.unknown
            out[f"attr_{key}_total"] = cov.total
        for metric_key, verdict in self.metric_certifiability().items():
            out[f"metric_{metric_key}"] = verdict
        return out


def roll_up_season(
    year: int,
    competitions: list[CoverageRecord],
    *,
    appeared_canonical: int,
    appeared_unresolved: int,
    resolved_appeared: int,
    attributes: dict[str, AttributeCoverage],
) -> CoverageRecord:
    """Pool competition records into one all-competitions season record.

    Additive facts (game/status counts, coverage counts, player-game counts)
    are summed; distinct-identity facts (canonical/unresolved appeared players
    and per-attribute known/total) are supplied pre-deduplicated at year grain
    so a player at two venues counts once.
    """
    season = CoverageRecord(
        scope_kind="season",
        scope_key=f"season:{year}",
        year=year,
        display_name=f"{year} Summer League (all competitions)",
        included_competitions=len(competitions),
    )
    for comp in competitions:
        for status in GAME_STATUSES:
            season.status_counts[status] += comp.status_counts.get(status, 0)
        season.final_games += comp.final_games
        season.box_complete_games += comp.box_complete_games
        season.shot_covered_games += comp.shot_covered_games
        season.pbp_covered_games += comp.pbp_covered_games
        season.games_with_score += comp.games_with_score
        season.games_with_known_ot += comp.games_with_known_ot
        season.overtime_games += comp.overtime_games
        season.appeared_player_games += comp.appeared_player_games
    season.appeared_canonical = appeared_canonical
    season.appeared_unresolved = appeared_unresolved
    season.resolved_appeared = resolved_appeared
    season.attributes = attributes
    return season


@dataclass
class AuditReport:
    """Full audit output: per-competition and per-season records plus summary."""

    generated_at: str
    database_host: str
    audit_version: str
    competitions: list[CoverageRecord]
    seasons: list[CoverageRecord]

    def all_records(self) -> list[CoverageRecord]:
        """Season rollups first, then their component competitions."""
        return [*self.seasons, *self.competitions]


# ---------------------------------------------------------------------------
# DB collection — integration tested against a disposable database.
# ---------------------------------------------------------------------------


class _Executor(Protocol):
    """Minimal async execute interface (AsyncSession or AsyncConnection)."""

    async def execute(self, statement: Any, parameters: Any = ...) -> Any: ...


async def _fetch_all(executor: _Executor, sql: str) -> list[Any]:
    """Run a read-only statement and return all rows."""
    result = await executor.execute(text(sql))
    return list(result.all())


async def collect_coverage(
    executor: _Executor, *, database_host: str = "local"
) -> AuditReport:
    """Build the full coverage report from read-only aggregate queries.

    Set-based: a fixed number of GROUP BY queries, never a per-competition or
    per-player loop. Accepts any object exposing ``async execute`` (the app's
    ``AsyncSession`` in tests, an ``AsyncConnection`` in the CLI).
    """
    comps = await _fetch_all(
        executor,
        """
        SELECT id, year, venue_slug, display_name, starts_on, ends_on
        FROM summer_league_competitions
        ORDER BY year, venue_slug
        """,
    )

    records: dict[int, CoverageRecord] = {}
    for row in comps:
        records[int(row.id)] = CoverageRecord(
            scope_kind="competition",
            scope_key=f"competition:{int(row.id)}",
            year=int(row.year),
            competition_id=int(row.id),
            venue_slug=row.venue_slug,
            display_name=row.display_name or f"{row.year} {row.venue_slug}",
            starts_on=row.starts_on.isoformat() if row.starts_on else None,
            ends_on=row.ends_on.isoformat() if row.ends_on else None,
        )

    # Status counts per competition.
    for row in await _fetch_all(
        executor,
        """
        SELECT competition_id, status, COUNT(*) AS n
        FROM summer_league_games
        GROUP BY competition_id, status
        """,
    ):
        rec = records.get(int(row.competition_id))
        if rec is not None:
            rec.status_counts[str(row.status)] = int(row.n)

    # Final-game coverage: box completeness, score/OT state per competition.
    for row in await _fetch_all(
        executor,
        """
        WITH final_games AS (
            SELECT g.id, g.competition_id, g.home_score, g.away_score,
                   g.status_text
            FROM summer_league_games g
            WHERE g.status = 'final'
        ),
        box AS (
            SELECT t.game_id,
                   COUNT(*) FILTER (
                       WHERE t.fga IS NOT NULL AND t.fgm IS NOT NULL
                         AND t.fg3a IS NOT NULL AND t.fta IS NOT NULL
                         AND t.oreb IS NOT NULL AND t.dreb IS NOT NULL
                         AND t.tov IS NOT NULL AND t.pts IS NOT NULL
                         AND t.minutes IS NOT NULL
                   ) AS complete_rows
            FROM summer_league_team_game_logs t
            GROUP BY t.game_id
        )
        SELECT fg.competition_id,
               COUNT(*) AS final_games,
               COUNT(*) FILTER (WHERE COALESCE(b.complete_rows, 0) = 2)
                   AS box_complete_games,
               COUNT(*) FILTER (
                   WHERE fg.home_score IS NOT NULL AND fg.away_score IS NOT NULL
               ) AS games_with_score,
               COUNT(*) FILTER (WHERE fg.status_text IS NOT NULL)
                   AS games_with_known_ot,
               COUNT(*) FILTER (WHERE fg.status_text ILIKE '%OT%')
                   AS overtime_games
        FROM final_games fg
        LEFT JOIN box b ON b.game_id = fg.id
        GROUP BY fg.competition_id
        """,
    ):
        rec = records.get(int(row.competition_id))
        if rec is not None:
            rec.final_games = int(row.final_games)
            rec.box_complete_games = int(row.box_complete_games)
            rec.games_with_score = int(row.games_with_score)
            rec.games_with_known_ot = int(row.games_with_known_ot)
            rec.overtime_games = int(row.overtime_games)

    # Shot-chart coverage: final games with at least one parsed shot event.
    for row in await _fetch_all(
        executor,
        """
        SELECT g.competition_id, COUNT(DISTINCT s.game_id) AS n
        FROM summer_league_games g
        JOIN summer_league_shot_events s ON s.game_id = g.id
        WHERE g.status = 'final'
        GROUP BY g.competition_id
        """,
    ):
        rec = records.get(int(row.competition_id))
        if rec is not None:
            rec.shot_covered_games = int(row.n)

    # PBP coverage: final games with at least one parsed play-by-play event.
    for row in await _fetch_all(
        executor,
        """
        SELECT g.competition_id, COUNT(DISTINCT p.game_id) AS n
        FROM summer_league_games g
        JOIN summer_league_play_by_play_events p ON p.game_id = g.id
        WHERE g.status = 'final'
        GROUP BY g.competition_id
        """,
    ):
        rec = records.get(int(row.competition_id))
        if rec is not None:
            rec.pbp_covered_games = int(row.n)

    # Appeared players (positive minutes in final games) per competition.
    for row in await _fetch_all(
        executor,
        """
        SELECT pgl.competition_id,
               COUNT(*) FILTER (WHERE pgl.minutes_seconds > 0)
                   AS appeared_player_games,
               COUNT(DISTINCT pgl.player_id) FILTER (
                   WHERE pgl.minutes_seconds > 0 AND pgl.player_id IS NOT NULL
               ) AS appeared_canonical,
               COUNT(DISTINCT pgl.source_player_id) FILTER (
                   WHERE pgl.minutes_seconds > 0 AND pgl.player_id IS NULL
               ) AS appeared_unresolved
        FROM summer_league_player_game_logs pgl
        JOIN summer_league_games g ON g.id = pgl.game_id AND g.status = 'final'
        GROUP BY pgl.competition_id
        """,
    ):
        rec = records.get(int(row.competition_id))
        if rec is not None:
            rec.appeared_player_games = int(row.appeared_player_games)
            rec.appeared_canonical = int(row.appeared_canonical)
            rec.appeared_unresolved = int(row.appeared_unresolved)

    # Attribute coverage over distinct resolved appeared players per competition.
    for row in await _fetch_all(
        executor,
        _attr_sql("pgl.competition_id", "competition_id"),
    ):
        rec = records.get(int(row.competition_id))
        if rec is not None:
            rec.resolved_appeared = int(row.resolved_appeared)
            rec.attributes["draft"] = AttributeCoverage(
                int(row.draft_known), int(row.resolved_appeared)
            )
            rec.attributes["age"] = AttributeCoverage(
                int(row.age_known), int(row.resolved_appeared)
            )
            rec.attributes["position"] = AttributeCoverage(
                int(row.position_known), int(row.resolved_appeared)
            )
            rec.attributes["origin"] = AttributeCoverage(
                int(row.origin_known), int(row.resolved_appeared)
            )

    competitions_by_year: dict[int, list[CoverageRecord]] = defaultdict(list)
    for rec in records.values():
        competitions_by_year[rec.year].append(rec)

    # Year-grain distinct appeared players (dedup canonical across venues).
    year_appeared: dict[int, tuple[int, int]] = {}
    for row in await _fetch_all(
        executor,
        """
        SELECT c.year AS year,
               COUNT(DISTINCT pgl.player_id) FILTER (
                   WHERE pgl.minutes_seconds > 0 AND pgl.player_id IS NOT NULL
               ) AS appeared_canonical,
               COUNT(DISTINCT pgl.source_player_id) FILTER (
                   WHERE pgl.minutes_seconds > 0 AND pgl.player_id IS NULL
               ) AS appeared_unresolved
        FROM summer_league_player_game_logs pgl
        JOIN summer_league_games g ON g.id = pgl.game_id AND g.status = 'final'
        JOIN summer_league_competitions c ON c.id = pgl.competition_id
        GROUP BY c.year
        """,
    ):
        year_appeared[int(row.year)] = (
            int(row.appeared_canonical),
            int(row.appeared_unresolved),
        )

    # Year-grain attribute coverage over distinct resolved appeared players.
    year_attr: dict[int, dict[str, AttributeCoverage]] = {}
    year_resolved: dict[int, int] = {}
    for row in await _fetch_all(
        executor,
        _attr_sql("c.year", "year", year_join=True),
    ):
        year_resolved[int(row.year)] = int(row.resolved_appeared)
        year_attr[int(row.year)] = {
            "draft": AttributeCoverage(
                int(row.draft_known), int(row.resolved_appeared)
            ),
            "age": AttributeCoverage(int(row.age_known), int(row.resolved_appeared)),
            "position": AttributeCoverage(
                int(row.position_known), int(row.resolved_appeared)
            ),
            "origin": AttributeCoverage(
                int(row.origin_known), int(row.resolved_appeared)
            ),
        }

    seasons: list[CoverageRecord] = []
    for year in sorted(competitions_by_year):
        canon, unresolved = year_appeared.get(year, (0, 0))
        seasons.append(
            roll_up_season(
                year,
                sorted(
                    competitions_by_year[year],
                    key=lambda r: r.venue_slug or "",
                ),
                appeared_canonical=canon,
                appeared_unresolved=unresolved,
                resolved_appeared=year_resolved.get(year, 0),
                attributes=year_attr.get(
                    year, {k: AttributeCoverage() for k in ATTRIBUTE_KEYS}
                ),
            )
        )

    return AuditReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        database_host=database_host,
        audit_version=AUDIT_VERSION,
        competitions=sorted(
            records.values(), key=lambda r: (r.year, r.venue_slug or "")
        ),
        seasons=seasons,
    )


# Shared attribute-coverage SQL. ``{scope}`` groups by competition or year;
# ``{alias}`` names the grouping column. Draft/age/position/origin "known" is a
# non-null event-time-capable input on the resolved canonical player.
_ATTRIBUTE_SQL = """
    WITH appeared AS (
        SELECT DISTINCT {scope} AS grp, pgl.player_id
        FROM summer_league_player_game_logs pgl
        JOIN summer_league_games g
            ON g.id = pgl.game_id AND g.status = 'final'
        {year_join_clause}
        WHERE pgl.minutes_seconds > 0 AND pgl.player_id IS NOT NULL
    )
    SELECT a.grp AS {alias},
           COUNT(*) AS resolved_appeared,
           COUNT(*) FILTER (WHERE pm.draft_year IS NOT NULL) AS draft_known,
           COUNT(*) FILTER (WHERE pm.birthdate IS NOT NULL) AS age_known,
           COUNT(*) FILTER (
               WHERE ps.position_id IS NOT NULL OR pm.position IS NOT NULL
           ) AS position_known,
           COUNT(*) FILTER (
               WHERE pm.birth_country IS NOT NULL OR pm.school IS NOT NULL
           ) AS origin_known
    FROM appeared a
    JOIN players_master pm ON pm.id = a.player_id
    LEFT JOIN player_status ps ON ps.player_id = pm.id
    GROUP BY a.grp
"""


def _attr_sql(scope: str, alias: str, year_join: bool = False) -> str:
    """Render the attribute SQL, wiring the competition-year join when needed."""
    return _ATTRIBUTE_SQL.format(
        scope=scope,
        alias=alias,
        year_join_clause=(
            "JOIN summer_league_competitions c ON c.id = pgl.competition_id"
            if year_join
            else ""
        ),
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def report_to_json(report: AuditReport) -> str:
    """Serialize the full report to pretty JSON."""
    payload = {
        "generated_at": report.generated_at,
        "database_host": report.database_host,
        "audit_version": report.audit_version,
        "metric_registry": [asdict(spec) for spec in METRIC_REGISTRY],
        "records": [rec.to_flat_dict() for rec in report.all_records()],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def report_to_csv(report: AuditReport) -> str:
    """Serialize the flattened records to CSV (one row per scope)."""
    rows = [rec.to_flat_dict() for rec in report.all_records()]
    buf = io.StringIO()
    if not rows:
        return ""
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _fmt_pct(known: int, total: int) -> str:
    """Format a known/total share as a percentage, or ``n/a``."""
    if total <= 0:
        return "n/a"
    return f"{100 * known / total:.0f}%"


def _season_metric_summary(report: AuditReport) -> dict[str, dict[str, int]]:
    """Count how many season profiles certify each metric at each level."""
    summary: dict[str, dict[str, int]] = {
        spec.key: {
            COVERAGE_COMPLETE: 0,
            COVERAGE_PARTIAL: 0,
            COVERAGE_UNAVAILABLE: 0,
        }
        for spec in METRIC_REGISTRY
    }
    for season in report.seasons:
        for key, verdict in season.metric_certifiability().items():
            summary[key][verdict] += 1
    return summary


def report_to_markdown(report: AuditReport) -> str:
    """Render the human-readable findings document."""
    lines: list[str] = []
    lines.append("# Competition Context Explorer — Coverage Audit")
    lines.append("")
    lines.append(
        "**Status:** Phase 0 audit (issue #616) · "
        f"**Generated:** {report.generated_at} · "
        f"**Source DB:** `{report.database_host}` · "
        f"**Audit version:** {report.audit_version}"
    )
    lines.append("")
    lines.append(
        "Read-only, reproducible inventory of what the Competition Context "
        "profiles (#606/#617) can publish honestly. Regenerate with "
        "`scripts/audit_summer_league_environment_coverage.py`. This report "
        "mutates no raw or derived Summer League fact."
    )
    lines.append("")
    lines.append("## Season rollups (all competitions per year)")
    lines.append("")
    lines.append(
        "| Year | Comps | Final | Box✓ | Shot✓ | PBP✓ | Appeared (canon) | "
        "Unresolved | Draft known | Age known | Pos known | Origin known |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for s in report.seasons:
        a = s.attributes
        lines.append(
            f"| {s.year} | {s.included_competitions} | {s.final_games} | "
            f"{s.box_complete_games} | {s.shot_covered_games} | "
            f"{s.pbp_covered_games} | {s.appeared_canonical} | "
            f"{s.appeared_unresolved} | "
            f"{_fmt_pct(a['draft'].known, a['draft'].total)} | "
            f"{_fmt_pct(a['age'].known, a['age'].total)} | "
            f"{_fmt_pct(a['position'].known, a['position'].total)} | "
            f"{_fmt_pct(a['origin'].known, a['origin'].total)} |"
        )
    lines.append("")

    lines.append("## Per-metric season certifiability")
    lines.append("")
    lines.append(
        "How many of the "
        f"{len(report.seasons)} season profiles certify each v1 metric "
        "(`complete` = every eligible final game carries the input)."
    )
    lines.append("")
    lines.append("| Metric | Source | Complete | Partial | Unavailable |")
    lines.append("| --- | --- | --- | --- | --- |")
    summary = _season_metric_summary(report)
    src = {spec.key: spec.source for spec in METRIC_REGISTRY}
    for spec in METRIC_REGISTRY:
        counts = summary[spec.key]
        lines.append(
            f"| `{spec.key}` | {src[spec.key]} | {counts[COVERAGE_COMPLETE]} | "
            f"{counts[COVERAGE_PARTIAL]} | {counts[COVERAGE_UNAVAILABLE]} |"
        )
    lines.append("")

    lines.append("## Individual competitions")
    lines.append("")
    lines.append(
        "| Year | Venue | Final | Box✓ | Shot✓ | PBP✓ | Score✓ | OT state✓ | "
        "Appeared | Unresolved |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for c in report.competitions:
        lines.append(
            f"| {c.year} | {c.venue_slug} | {c.final_games} | "
            f"{c.box_complete_games} | {c.shot_covered_games} | "
            f"{c.pbp_covered_games} | {c.games_with_score} | "
            f"{c.games_with_known_ot} | {c.appeared_canonical} | "
            f"{c.appeared_unresolved} |"
        )
    lines.append("")

    lines.append("## Schema / index notes for #606 and #617")
    lines.append("")
    lines.append(_SCHEMA_NOTES)
    lines.append("")
    lines.append("## Method and honesty rules")
    lines.append("")
    lines.append(_METHOD_NOTES)
    lines.append("")
    return "\n".join(lines)


_SCHEMA_NOTES = """\
- **No stored profile table exists yet.** `#606` must add the `scope_key`
  (`season:<year>` / `competition:<competition_id>`) projection with a partial
  unique index on the current row. This audit reads only raw spokes.
- **Overtime is not a normalized fact.** OT is inferred from
  `SummerLeagueGame.status_text ILIKE '%OT%'`; games with a null `status_text`
  have unknown OT state. `overtime_share` is a confidence-badged metric, not a
  certified count, until a normalized OT flag exists.
- **Shot/PBP coverage is proxied by parsed event rows**, not by raw-file parse
  status. A game with zero shot events is treated as shot-uncovered; #617
  should reconcile against `summer_league_raw_files.parse_status` for the
  `shotchartdetail`/`playbyplay` endpoints before certifying `shot_complete`.
- **Origin has no event-time affiliation source in the box spoke.** This audit
  approximates origin from current `players_master.birth_country`/`school`,
  which the contract (§5) forbids as the published source. #617 must resolve a
  pre-event affiliation/participation origin; treat the origin distribution as
  provisional until then.
- **Position** is read from the canonical `player_status.position_id` taxonomy
  (falling back to the near-empty `players_master.position`). Per §5 the
  *event-time* participation/roster position is preferred; that lives in
  `summer_league_participation.roster_position` and
  `summer_league_player_game_logs.starter_position` and is only ~24% populated,
  so #617 must decide the event-time-vs-canonical precedence explicitly rather
  than assuming the canonical value is the answer.
- Aggregation reads should key competition scope through the existing
  `ix_summer_league_team_game_logs_competition_team` and
  `ix_summer_league_player_game_logs_competition_player` indexes; a rebuild
  keyed by `game_id` uses `ix_summer_league_shot_events_game_id`.
"""

_METHOD_NOTES = """\
- **Eligible games:** only `status = 'final'` games contribute metric inputs
  and appeared players. Scheduled/in-progress/postponed/canceled/unknown games
  are reported as status counts but never as numerators/denominators.
- **Appeared player:** a player-game log with `minutes_seconds > 0` in an
  eligible final game. DNP shells are excluded. Distinct people use canonical
  `player_id`; unresolved appearances are counted separately by
  `source_player_id` and never silently dropped.
- **Box complete (per game):** exactly two team-box rows with all of
  fga/fgm/fg3a/fta/oreb/dreb/tov/pts/minutes non-null.
- **Coverage verdict (per metric, per scope):** `complete` when every eligible
  final game carries the input, `partial` when some do, `unavailable` when
  none do or there are no eligible games. `partial` is never shown as zero.
- **Season dedup:** canonical appeared players and attribute known/total are
  recomputed at year grain so a player at multiple venues counts once;
  additive game/coverage counts are summed across competitions.
"""


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _host_of(url: str) -> str:
    """Extract a printable host from a database URL."""
    return url.split("@")[-1].split("/")[0] if "@" in url else "local"


async def _run_cli(args: argparse.Namespace) -> int:
    """Connect, collect, and write the audit artifacts."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.utils.db_async import _prepare_asyncpg_connection

    raw_url = os.environ.get(args.url_env)
    if not raw_url:
        print(f"ERROR: {args.url_env} not set", file=sys.stderr)
        return 1

    normalized_url, connect_args = _prepare_asyncpg_connection(raw_url)
    engine = create_async_engine(
        normalized_url, connect_args=connect_args, pool_pre_ping=True
    )
    try:
        async with engine.connect() as conn:
            report = await collect_coverage(conn, database_host=_host_of(raw_url))
    finally:
        await engine.dispose()

    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "competition-context-coverage-audit.json")
    csv_path = os.path.join(args.out_dir, "competition-context-coverage-audit.csv")
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(report_to_json(report))
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write(report_to_csv(report))
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")

    if args.write_markdown:
        md_path = os.path.join(
            args.md_dir, "competition-context-explorer-coverage-audit.md"
        )
        os.makedirs(args.md_dir, exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(report_to_markdown(report))
        print(f"wrote {md_path}")

    print(
        f"\nseasons={len(report.seasons)} competitions={len(report.competitions)} "
        "audited"
    )
    return 0


def main() -> None:
    """Parse arguments and run the audit."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url-env",
        default="DATABASE_URL",
        help="env var holding the database URL (default DATABASE_URL)",
    )
    ap.add_argument(
        "--out-dir",
        default="/tmp",
        help="directory for the JSON/CSV machine-readable artifacts",
    )
    ap.add_argument(
        "--md-dir",
        default="docs/plans",
        help="directory for the human-readable markdown report",
    )
    ap.add_argument(
        "--write-markdown",
        action="store_true",
        help="also write the human-readable coverage-audit markdown",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    main()
