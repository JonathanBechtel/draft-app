"""Run Summer League backbone QA validators and write a Markdown report.

Run:

    conda run -n draftguru python scripts/qa_summer_league_backbone.py \
      --year 2024 --league-id 15 \
      --raw-root data/raw/nba_stats/summer_league \
      --report-path docs/qa/summer-league-backbone-qa-YYYY-MM-DD.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.services.summer_league.qa import (  # noqa: E402
    SummerLeagueQAFinding,
    SummerLeagueQAReport,
    SummerLeagueQASeverity,
    SummerLeagueSlice,
    run_summer_league_backbone_qa,
)

DEFAULT_RAW_ROOT = Path("data/raw/nba_stats/summer_league")
DEFAULT_REPORT_DIR = Path("docs/qa")
DEFAULT_EXAMPLE_LIMIT = 5


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, help="Summer League year for --league-id")
    parser.add_argument(
        "--league-id",
        action="append",
        help="NBA.com LeagueID. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--slice",
        dest="slices",
        action="append",
        help="Summer League slice as YEAR/LEAGUE_ID. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_RAW_ROOT,
        help="Local Summer League raw snapshot root to include in the report.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Markdown report path. Defaults under docs/qa/ with today's date.",
    )
    return parser


def parse_slice(value: str) -> SummerLeagueSlice:
    """Parse one YEAR/LEAGUE_ID slice value."""
    pieces = value.strip().split("/", maxsplit=1)
    if len(pieces) != 2 or not pieces[0] or not pieces[1]:
        raise ValueError(f"Expected slice in YEAR/LEAGUE_ID format, got {value!r}")
    try:
        year = int(pieces[0])
    except ValueError as exc:
        raise ValueError(f"Expected numeric slice year, got {pieces[0]!r}") from exc
    return SummerLeagueSlice(year=year, league_id=pieces[1].strip())


def split_comma_values(values: Sequence[str] | None) -> list[str]:
    """Expand repeated and comma-separated CLI values into stripped tokens."""
    expanded: list[str] = []
    for value in values or ():
        for piece in value.split(","):
            stripped = piece.strip()
            if stripped:
                expanded.append(stripped)
    return expanded


def build_slices(
    *,
    year: int | None,
    league_ids: Sequence[str] | None,
    slice_values: Sequence[str] | None,
) -> list[SummerLeagueSlice]:
    """Build deduplicated QA slices from parser output."""
    slices: list[SummerLeagueSlice] = []
    for value in split_comma_values(slice_values):
        slices.append(parse_slice(value))

    expanded_league_ids = split_comma_values(league_ids)
    if year is not None or expanded_league_ids:
        if year is None or not expanded_league_ids:
            raise ValueError("--year and --league-id must be provided together")
        slices.extend(
            SummerLeagueSlice(year=year, league_id=league_id)
            for league_id in expanded_league_ids
        )

    deduped: list[SummerLeagueSlice] = []
    seen: set[tuple[int, str]] = set()
    for slice_ in slices:
        key = (slice_.year, slice_.league_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(slice_)
    if not deduped:
        raise ValueError("Provide at least one --slice or --year/--league-id pair")
    return deduped


def default_report_path(generated_at: datetime) -> Path:
    """Return the default dated Markdown report path."""
    return (
        DEFAULT_REPORT_DIR
        / f"summer-league-backbone-qa-{generated_at.date().isoformat()}.md"
    )


def render_markdown_report(
    report: SummerLeagueQAReport,
    *,
    raw_root: Path,
    generated_at: datetime,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> str:
    """Render a human-readable Markdown QA report."""
    finding_counts = Counter(finding.code for finding in report.findings)
    lines = [
        "# Summer League Backbone QA Report",
        "",
        f"- Generated: `{generated_at.isoformat()}`",
        f"- Raw root: `{raw_root.as_posix()}`",
        f"- Status: `{'FAIL' if report.has_errors else 'PASS'}`",
        f"- Slices: `{', '.join(format_slice(slice_) for slice_ in report.slices)}`",
        f"- Total findings: `{len(report.findings)}`",
        "",
        "## Severity Counts",
        "",
        "| Severity | Count |",
        "| --- | ---: |",
    ]
    severity_counts = report.severity_counts
    for severity in SummerLeagueQASeverity:
        lines.append(f"| {severity.value} | {severity_counts[severity]} |")

    lines.extend(["", "## Finding Codes", ""])
    if finding_counts:
        lines.extend(["| Code | Count |", "| --- | ---: |"])
        for code, count in sorted(finding_counts.items()):
            lines.append(f"| `{code}` | {count} |")
    else:
        lines.append("No findings.")

    lines.extend(["", "## Failure Summary", ""])
    error_findings = [
        finding
        for finding in report.findings
        if finding.severity == SummerLeagueQASeverity.ERROR
    ]
    if error_findings:
        error_counts = Counter(finding.code for finding in error_findings)
        lines.extend(["| Code | Count | Example |", "| --- | ---: | --- |"])
        for code, count in sorted(error_counts.items()):
            example = next(
                finding for finding in error_findings if finding.code == code
            )
            lines.append(f"| `{code}` | {count} | {example.message} |")
    else:
        lines.append("No blocking/error findings.")

    lines.extend(["", "## Examples", ""])
    if report.findings:
        for finding in report.findings[:example_limit]:
            lines.extend(render_finding_example(finding))
    else:
        lines.append("No finding examples.")
    if len(report.findings) > example_limit:
        lines.append("")
        lines.append(f"_Showing {example_limit} of {len(report.findings)} findings._")

    return "\n".join(lines) + "\n"


def render_finding_example(finding: SummerLeagueQAFinding) -> list[str]:
    """Render one finding example block."""
    lines = [
        f"### `{finding.code}`",
        "",
        f"- Severity: `{finding.severity.value}`",
        f"- Message: {finding.message}",
    ]
    if finding.evidence:
        lines.append(f"- Evidence: `{format_evidence(finding.evidence)}`")
    lines.append("")
    return lines


def format_evidence(evidence: dict[str, object]) -> str:
    """Return compact stable evidence text for Markdown examples."""
    return json.dumps(evidence, sort_keys=True, default=str)


def format_slice(slice_: SummerLeagueSlice) -> str:
    """Return the stable YEAR/LEAGUE_ID display form."""
    return f"{slice_.year}/{slice_.league_id}"


def write_markdown_report(markdown: str, report_path: Path) -> None:
    """Write a Markdown report file, creating parent directories as needed."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")


def summarize_qa_report(report: SummerLeagueQAReport, report_path: Path) -> str:
    """Return a compact CLI summary."""
    counts = report.severity_counts
    return (
        f"slices={len(report.slices)} findings={len(report.findings)} "
        f"errors={counts[SummerLeagueQASeverity.ERROR]} "
        f"warnings={counts[SummerLeagueQASeverity.WARNING]} "
        f"report={report_path.as_posix()}"
    )


def load_database_url() -> str | None:
    """Resolve the database URL without breaking --help in unconfigured shells."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url
    try:
        from app.config import settings  # noqa: PLC0415
    except Exception:
        return None
    return settings.database_url


async def run_qa(args: argparse.Namespace) -> int:
    """Run Summer League QA and return a process exit code."""
    generated_at = datetime.now(UTC)
    report_path = args.report_path or default_report_path(generated_at)
    slices = build_slices(
        year=args.year,
        league_ids=args.league_id,
        slice_values=args.slices,
    )
    database_url = load_database_url()
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    try:
        async with session_factory() as db:
            report = await run_summer_league_backbone_qa(db, slices=slices)
    finally:
        await engine.dispose()

    markdown = render_markdown_report(
        report,
        raw_root=args.raw_root,
        generated_at=generated_at,
    )
    write_markdown_report(markdown, report_path)
    print(summarize_qa_report(report, report_path), flush=True)
    return 1 if report.has_errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the async QA command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_qa(args))
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
