"""Unit tests for the `ingest()` wrapper: CSV read, transaction shape, reports.

The row-level behaviour is covered against a real database in
``tests/integration/test_player_bio_ingest.py``. What is pinned here is the
part that has no database in it: that a real run commits, that a dry run leaves
the session uncommitted, and that the unresolved-row reports land beside the
CSV.

This matters because the commit/rollback pair the ingest used to call directly
was replaced with an ``async with db.begin()`` block (``app/services/`` must
leave transaction boundaries to the caller — see
``scripts/check_request_transaction_policy.py``). These tests are what makes
that swap verifiable rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.player_bio import ingest as ingest_module
from app.services.player_bio.ingest import IngestReport, ingest

_CSV_HEADER = "slug,full_name,source_url,draft_year\n"
_CSV_ROW = "balllo01,Lonzo Ball,https://example.invalid/balllo01.html,2017\n"


class _FakeTransaction:
    """Stands in for the object ``AsyncSession.begin()`` returns."""

    def __init__(self, session: "_FakeSession") -> None:
        self._session = session

    async def __aenter__(self) -> "_FakeTransaction":
        self._session.began = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._session.committed = True
        else:
            self._session.rolled_back = True
        return False


class _FakeSession:
    """Minimal async session recording whether a transaction was opened."""

    def __init__(self) -> None:
        self.began = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self.closed = True
        return False


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    """Install a fake ``SessionLocal`` and return the single session it yields."""
    fake = _FakeSession()
    monkeypatch.setattr(ingest_module, "SessionLocal", lambda: fake)
    return fake


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    """A one-row scraped-bio CSV."""
    path = tmp_path / "bbio_b_20260727.csv"
    path.write_text(_CSV_HEADER + _CSV_ROW, encoding="utf-8")
    return path


def _stub_rows(monkeypatch: pytest.MonkeyPatch, report: IngestReport) -> list[Any]:
    """Replace ``_ingest_rows`` with a recorder returning ``report``."""
    calls: list[Any] = []

    async def _fake(db: Any, rows: Any, options: Any) -> IngestReport:
        calls.append((db, rows, options))
        return report

    monkeypatch.setattr(ingest_module, "_ingest_rows", _fake)
    return calls


@pytest.mark.asyncio
async def test_a_real_run_commits_once(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dry run wraps the staging in a transaction that commits."""
    calls = _stub_rows(monkeypatch, IngestReport())

    await ingest(
        csv_path=csv_path,
        cache_dir=csv_path.parent,
        dry_run=False,
        verbose=False,
        overwrite_master=False,
        fix_ambiguities_path=None,
        create_missing=False,
    )

    assert session.began is True
    assert session.committed is True
    assert session.rolled_back is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_dry_run_never_opens_a_transaction(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run stages the same work but lets the session close uncommitted."""
    calls = _stub_rows(monkeypatch, IngestReport())

    await ingest(
        csv_path=csv_path,
        cache_dir=csv_path.parent,
        dry_run=True,
        verbose=False,
        overwrite_master=False,
        fix_ambiguities_path=None,
        create_missing=False,
    )

    assert session.began is False
    assert session.committed is False
    assert session.closed is True
    assert len(calls) == 1  # the work was staged, just not made durable


@pytest.mark.asyncio
async def test_a_failure_rolls_back_and_writes_no_reports(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error inside the transaction propagates; no partial report is emitted."""

    async def _boom(db: Any, rows: Any, options: Any) -> IngestReport:
        raise RuntimeError("conflicting row")

    monkeypatch.setattr(ingest_module, "_ingest_rows", _boom)

    with pytest.raises(RuntimeError, match="conflicting row"):
        await ingest(
            csv_path=csv_path,
            cache_dir=csv_path.parent,
            dry_run=False,
            verbose=False,
            overwrite_master=False,
            fix_ambiguities_path=None,
            create_missing=False,
        )

    assert session.rolled_back is True
    assert session.committed is False
    assert not (csv_path.parent / "bbio_unmatched.json").exists()


@pytest.mark.asyncio
async def test_reports_are_written_beside_the_csv(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unresolved slugs and manual-review ids land next to the ingested file."""
    _stub_rows(
        monkeypatch,
        IngestReport(
            unmatched=["nobodyxx01"], ambiguous=["harpede01"], manual_review=[7, 9]
        ),
    )

    await ingest(
        csv_path=csv_path,
        cache_dir=csv_path.parent,
        dry_run=False,
        verbose=False,
        overwrite_master=False,
        fix_ambiguities_path=None,
        create_missing=False,
    )

    out = csv_path.parent
    assert json.loads((out / "bbio_unmatched.json").read_text()) == ["nobodyxx01"]
    assert json.loads((out / "bbio_ambiguous.json").read_text()) == ["harpede01"]
    assert json.loads((out / "bbio_manual_review.json").read_text()) == [7, 9]


@pytest.mark.asyncio
async def test_an_empty_report_writes_no_files(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run leaves no stale report files behind."""
    _stub_rows(monkeypatch, IngestReport())

    await ingest(
        csv_path=csv_path,
        cache_dir=csv_path.parent,
        dry_run=False,
        verbose=False,
        overwrite_master=False,
        fix_ambiguities_path=None,
        create_missing=False,
    )

    out = csv_path.parent
    assert not (out / "bbio_unmatched.json").exists()
    assert not (out / "bbio_ambiguous.json").exists()
    assert not (out / "bbio_manual_review.json").exists()


@pytest.mark.asyncio
async def test_the_csv_and_the_fix_map_reach_the_staging_step(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CSV rows are parsed once, and manual resolutions are read off disk."""
    calls = _stub_rows(monkeypatch, IngestReport())
    fix_path = csv_path.parent / "fixes.json"
    fix_path.write_text(json.dumps({"balllo01": 42}), encoding="utf-8")

    await ingest(
        csv_path=csv_path,
        cache_dir=csv_path.parent,
        dry_run=False,
        verbose=True,
        overwrite_master=True,
        fix_ambiguities_path=fix_path,
        create_missing=True,
        summer_league_year=2026,
        summer_league_league_id="15",
    )

    _db, rows, options = calls[0]
    assert [row.slug for row in rows] == ["balllo01"]
    assert rows[0].draft_year == 2017
    assert options.fixed_map == {"balllo01": 42}
    assert options.overwrite_master is True
    assert options.create_missing is True
    assert options.cohort_scoped is True


@pytest.mark.asyncio
async def test_a_missing_fix_map_is_simply_absent(
    session: _FakeSession, csv_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing at a nonexistent fixes file is not an error — it means no fixes."""
    calls = _stub_rows(monkeypatch, IngestReport())

    await ingest(
        csv_path=csv_path,
        cache_dir=csv_path.parent,
        dry_run=False,
        verbose=False,
        overwrite_master=False,
        fix_ambiguities_path=csv_path.parent / "does-not-exist.json",
        create_missing=False,
    )

    _db, _rows, options = calls[0]
    assert options.fixed_map == {}
    assert options.cohort_scoped is False
