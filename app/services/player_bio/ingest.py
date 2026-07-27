"""Ingest a scraped Basketball-Reference bio CSV into the database.

One run resolves every CSV row to a canonical player (see
:mod:`app.services.player_bio.matching`), stages the external ids, alias,
master fields, status row and raw-meta snapshot for each, and commits once.
Rows that cannot be resolved are reported to ``bbio_unmatched.json`` /
``bbio_ambiguous.json`` beside the CSV rather than guessed at.

A dry run stages exactly the same work and then simply lets the session close
without a transaction block, so nothing is durable -- which is why no explicit
``commit()``/``rollback()`` appears here (see
``scripts/check_request_transaction_policy.py``).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.players_master import PlayerMaster
from app.services.player_bio.matching import (
    SYSTEM_BBR,
    SYSTEM_INSTAGRAM,
    SYSTEM_X,
    _deterministic_match,
    _load_lookup,
    _name_parts,
    _norm,
)
from app.services.player_bio.persistence import (
    _ensure_alias,
    _load_raw_meta_html,
    _update_master,
    _upsert_external,
    _upsert_status,
)
from app.services.player_bio.rows import BioRow, load_bio_rows
from app.services.summer_league.bio_enrichment_targets import (
    select_bio_enrichment_targets,
)
from app.utils.db_async import SessionLocal


@dataclass
class IngestReport:
    """Slugs and player ids one ingest run could not resolve on its own."""

    unmatched: List[str] = field(default_factory=list)
    ambiguous: List[str] = field(default_factory=list)
    manual_review: List[int] = field(default_factory=list)


@dataclass(frozen=True)
class IngestOptions:
    """Everything one ingest run needs beyond the session and the rows.

    Attributes:
        cache_dir: Directory of cached player HTML for raw-meta snapshots.
        overwrite_master: Allow overwriting already-populated master fields.
        create_missing: Create a ``players_master`` row for unmatched records.
        fixed_map: Manual ``slug -> player_id`` resolutions that win outright.
        verbose: Print per-row diagnostics.
        summer_league_year: Restrict to this Summer League cohort year.
        summer_league_league_id: Restrict to this NBA.com LeagueID.
        summer_league_venue_slug: Restrict to this venue slug.
    """

    cache_dir: Path
    overwrite_master: bool
    create_missing: bool
    fixed_map: Dict[str, int] = field(default_factory=dict)
    verbose: bool = False
    summer_league_year: Optional[int] = None
    summer_league_league_id: Optional[str] = None
    summer_league_venue_slug: Optional[str] = None

    @property
    def cohort_scoped(self) -> bool:
        """True when the run is restricted to a Summer League cohort."""
        return (
            self.summer_league_year is not None
            or self.summer_league_league_id is not None
            or self.summer_league_venue_slug is not None
        )


async def _ingest_rows(
    db: AsyncSession, rows: List[BioRow], options: IngestOptions
) -> IngestReport:
    """Stage every row's writes on ``db`` without committing.

    Args:
        db: Session to stage on; the caller owns the transaction boundary.
        rows: Bio rows read from the scraped CSV.
        options: Resolution and write behaviour for this run.

    Returns:
        The unresolved slugs and manual-review player ids for this run.
    """
    verbose = options.verbose
    fixed_map = options.fixed_map
    report = IngestReport()
    ext_map, alias_map, last_idx, pm_by_id = await _load_lookup(db)

    if options.cohort_scoped:
        targets = await select_bio_enrichment_targets(
            db,
            year=options.summer_league_year,
            league_id=options.summer_league_league_id,
            venue_slug=options.summer_league_venue_slug,
        )
        report.manual_review = sorted(targets.manual_review_player_ids)
        rows = [r for r in rows if r.slug in targets.slugs]
        if verbose:
            print(
                f"[info] SL-cohort scope: {len(targets.slugs)} bbref-having"
                f" target slug(s), {len(report.manual_review)} flagged for manual review"
            )

    for r in rows:
        player_id: Optional[int] = None
        # Fixed mapping overrides
        if r.slug in fixed_map:
            player_id = fixed_map[r.slug]
        # External ID (bbr)
        if player_id is None:
            player_id = ext_map.get(r.slug)
        # Exact alias
        if player_id is None:
            pids = alias_map.get(_norm(r.full_name), [])
            if len(pids) == 1:
                player_id = pids[0]
            elif len(pids) > 1:
                report.ambiguous.append(r.slug)
        # Deterministic match by last/first
        if player_id is None:
            pid = _deterministic_match(r.full_name, last_idx, pm_by_id)
            if pid is not None:
                player_id = pid

        if player_id is None:
            if options.create_missing:
                # Create a new PlayerMaster for this record (canonical row)
                first, middle, last = _name_parts(r.full_name)
                pm = PlayerMaster(
                    prefix=None,
                    first_name=first,
                    middle_name=middle,
                    last_name=last,
                    suffix=None,
                    display_name=r.full_name,
                )
                db.add(pm)
                await db.flush()  # get pm.id
                player_pk = pm.id
                if player_pk is None:
                    raise RuntimeError("PlayerMaster missing id after flush")
                player_id = player_pk
                pm_by_id[player_pk] = pm
                # Update lookups for subsequent rows
                alias_map.setdefault(_norm(r.full_name), []).append(player_pk)
                if last:
                    last_idx.setdefault(_norm(last), []).append(player_pk)
            else:
                report.unmatched.append(r.slug)
                if verbose:
                    print(f"[warn] unmatched slug={r.slug} name={r.full_name}")
                continue

        assert player_id is not None
        # Load player
        player = pm_by_id.get(player_id)
        if not player:
            # Should not happen
            report.unmatched.append(r.slug)
            continue

        # External IDs: bbr + socials
        await _upsert_external(db, player_id, SYSTEM_BBR, r.slug, r.source_url)
        if r.social_x_handle:
            await _upsert_external(
                db, player_id, SYSTEM_X, r.social_x_handle, r.social_x_url
            )
        if r.social_instagram_handle:
            await _upsert_external(
                db,
                player_id,
                SYSTEM_INSTAGRAM,
                r.social_instagram_handle,
                r.social_instagram_url,
            )

        await _ensure_alias(db, player_id, r.full_name)

        # Update master (immutable fields)
        await _update_master(db, player, r, options.overwrite_master)

        # Upsert status (ephemeral)
        await _upsert_status(db, player_id, r)

        # Snapshot raw meta HTML if present in cache
        raw_meta = _load_raw_meta_html(options.cache_dir, r.slug)
        if raw_meta:
            db.add(
                PlayerBioSnapshot(
                    player_id=player_id,
                    source="bbr",
                    source_url=r.source_url,
                    raw_meta_html=raw_meta,
                )
            )

    return report


def _write_reports(out_dir: Path, report: IngestReport) -> None:
    """Write the run's unresolved-row reports beside the ingested CSV."""
    if report.unmatched:
        (out_dir / "bbio_unmatched.json").write_text(
            json.dumps(report.unmatched, indent=2), encoding="utf-8"
        )
    if report.ambiguous:
        (out_dir / "bbio_ambiguous.json").write_text(
            json.dumps(report.ambiguous, indent=2), encoding="utf-8"
        )
    if report.manual_review:
        (out_dir / "bbio_manual_review.json").write_text(
            json.dumps(report.manual_review, indent=2), encoding="utf-8"
        )


async def ingest(
    csv_path: Path,
    cache_dir: Path,
    dry_run: bool,
    verbose: bool,
    overwrite_master: bool,
    fix_ambiguities_path: Optional[Path],
    create_missing: bool,
    summer_league_year: Optional[int] = None,
    summer_league_league_id: Optional[str] = None,
    summer_league_venue_slug: Optional[str] = None,
) -> None:
    """Ingest a scraped bio CSV, committing once at the end.

    Args:
        csv_path: Scraped ``bbio_*.csv`` to ingest; reports land beside it.
        cache_dir: Directory of cached player HTML for raw-meta snapshots.
        dry_run: Stage the work but leave nothing durable.
        verbose: Print per-row diagnostics.
        overwrite_master: Allow overwriting already-populated master fields.
        fix_ambiguities_path: Optional JSON of manual ``slug -> player_id``
            resolutions that win over every automatic match.
        create_missing: Create a ``players_master`` row for unmatched records.
        summer_league_year: Restrict to this Summer League cohort year.
        summer_league_league_id: Restrict to this NBA.com LeagueID.
        summer_league_venue_slug: Restrict to this venue slug.
    """
    rows = load_bio_rows(csv_path)

    # Ambiguities fixes: mapping slug -> player_id
    fixed_map: Dict[str, int] = {}
    if fix_ambiguities_path and fix_ambiguities_path.exists():
        fixed_map = json.loads(fix_ambiguities_path.read_text(encoding="utf-8"))

    options = IngestOptions(
        cache_dir=cache_dir,
        overwrite_master=overwrite_master,
        create_missing=create_missing,
        fixed_map=fixed_map,
        verbose=verbose,
        summer_league_year=summer_league_year,
        summer_league_league_id=summer_league_league_id,
        summer_league_venue_slug=summer_league_venue_slug,
    )

    async with SessionLocal() as db:
        if dry_run:
            # No transaction block: the session closes without committing, so
            # everything staged above is discarded. That is the dry-run contract.
            report = await _ingest_rows(db, rows, options)
        else:
            # `begin()` commits on clean exit and rolls back on any exception,
            # including the IntegrityError a conflicting row would raise.
            async with db.begin():
                report = await _ingest_rows(db, rows, options)

        _write_reports(csv_path.parent, report)
