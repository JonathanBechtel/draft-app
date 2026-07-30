"""Unit tests for Summer League player-resolution helper behavior."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeaguePlayerResolutionReview,
    SummerLeagueResolutionStatus,
    SummerLeagueReviewStatus,
    SummerLeagueSourcePlayer,
)
from app.services.summer_league import player_resolution as service
from app.services.summer_league.player_resolution import (
    SummerLeagueResolutionCandidate,
    SummerLeagueResolutionResult,
    SummerLeagueCandidateSearchError,
    build_resolution_report,
    _candidate_payloads,
    _collapse_whitespace,
    _create_stub_player,
    _ensure_nba_stats_external_id,
    _find_external_id_player,
    _has_serious_candidate,
    _load_source_players,
    _serialize_search_candidates,
    _backfill_player_game_logs,
    ensure_pending_resolution_review,
    normalize_player_name,
    record_resolution_review_decision,
    resolve_source_player,
    resolve_summer_league_players,
)


class _FakeResult:
    def __init__(
        self,
        *,
        scalar: Any = None,
        rows: list[tuple[Any, ...]] | None = None,
        scalars: list[Any] | None = None,
        rowcount: int | None = None,
    ) -> None:
        self._scalar = scalar
        self._rows = rows or []
        self._scalars = scalars or []
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def scalars(self) -> "_FakeScalarResult":
        return _FakeScalarResult(self._scalars)


class _FakeScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _FakeDb:
    def __init__(
        self,
        results: list[_FakeResult] | None = None,
        *,
        get_result: Any = None,
    ) -> None:
        self.results = results or []
        self.get_result = get_result
        self.added: list[Any] = []
        self.flushed = 0
        self.executed = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed += 1
        if not self.results:
            return _FakeResult()
        return self.results.pop(0)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def get(self, model: Any, object_id: Any) -> Any:
        return self.get_result

    async def flush(self) -> None:
        self.flushed += 1
        for obj in self.added:
            if isinstance(obj, PlayerMaster) and obj.id is None:
                obj.id = 99


class _SearchHit:
    def __init__(self, player_id: int, display_name: str | None, score: float) -> None:
        self.player_id = player_id
        self.display_name = display_name
        self.score = score


def _source(
    *,
    person_id: str = "1640001",
    name: str = "Source Prospect",
    canonical_player_id: int | None = None,
    status: SummerLeagueResolutionStatus = SummerLeagueResolutionStatus.UNRESOLVED,
    resolved_at: datetime | None = None,
    resolved_by: str | None = None,
) -> SummerLeagueSourcePlayer:
    return SummerLeagueSourcePlayer(
        id=5,
        nba_stats_person_id=person_id,
        raw_player_name=name,
        normalized_name=normalize_player_name(name),
        canonical_player_id=canonical_player_id,
        resolution_status=status,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
    )


def test_collapse_whitespace_trims_repeated_spacing() -> None:
    """Stub display names are normalized without altering source spelling."""
    assert _collapse_whitespace("  Two   Way\tProspect  ") == "Two Way Prospect"


def test_normalize_player_name_folds_diacritics_and_suffixes() -> None:
    """Identity matching folds suffix, diacritic, and punctuation variants."""
    assert normalize_player_name(" José   García Jr. ") == "jose garcia"
    assert normalize_player_name("P.J. Washington") == "pj washington"
    assert normalize_player_name("Jean-Luc O’Neal III") == "jean luc oneal"


@pytest.mark.parametrize(
    ("source_name", "candidate_name", "expected"),
    [
        ("Gary Payton II", "Gary Payton", True),
        ("Gary Payton II", "Gary Payton II", False),
        ("José García", "Jose Garcia", False),
        ("Gary Payton Jr.", "Gary Payton Sr.", True),
    ],
)
def test_suffix_variant_planning_distinguishes_namesakes(
    source_name: str,
    candidate_name: str,
    expected: bool,
) -> None:
    """Suffix differences are ambiguous while punctuation and matching suffixes are safe."""
    assert service._suffixes_differ(source_name, candidate_name) is expected


def test_suffix_mismatch_variant_match_becomes_review_candidate() -> None:
    """A unique suffix mismatch produces a review plan instead of an exact link."""
    plan = service._plan_from_variant_matches(
        source_player_id=5,
        source_player_name="Gary Payton II",
        matches=service.IdentityVariantMatches(
            display_names={7: "Gary Payton"},
            alias_names={},
        ),
    )

    assert plan is not None
    assert plan.kind == "VECTOR_CANDIDATE"
    assert plan.player_id is None
    assert plan.candidates[0].player_id == 7
    assert plan.candidates[0].method == "NORMALIZED_SUFFIX_MISMATCH"


def test_candidate_payloads_round_scores_for_json_storage() -> None:
    """Candidate DTOs serialize to compact JSONB-safe dictionaries."""
    candidates = [
        SummerLeagueResolutionCandidate(
            player_id=7,
            display_name="Candidate Player",
            score=0.87654321,
        )
    ]

    assert _candidate_payloads(candidates) == [
        {
            "player_id": 7,
            "display_name": "Candidate Player",
            "score": 0.876543,
            "method": "HYBRID",
        }
    ]


def test_serious_candidate_threshold_blocks_speculative_stubs() -> None:
    """Only candidates at or above the service threshold block stub creation."""
    assert (
        _has_serious_candidate(
            [
                SummerLeagueResolutionCandidate(
                    player_id=1,
                    display_name="Weak Candidate",
                    score=0.299,
                )
            ]
        )
        is False
    )
    assert (
        _has_serious_candidate(
            [
                SummerLeagueResolutionCandidate(
                    player_id=2,
                    display_name="Serious Candidate",
                    score=0.3,
                )
            ]
        )
        is True
    )


@pytest.mark.asyncio
async def test_ensure_pending_resolution_review_creates_and_updates() -> None:
    """Pending review upsert creates one active row and refreshes candidates."""
    source = _source()
    candidate = SummerLeagueResolutionCandidate(
        player_id=7,
        display_name="Candidate",
        score=0.72,
    )
    create_db = _FakeDb([_FakeResult(scalar=None)])

    created = await ensure_pending_resolution_review(  # type: ignore[arg-type]
        create_db,
        source,
        [candidate],
    )

    assert created.source_player_id == source.id
    assert created.status == SummerLeagueReviewStatus.PENDING
    assert created.candidate_players == [
        {
            "player_id": 7,
            "display_name": "Candidate",
            "score": 0.72,
            "method": "HYBRID",
        }
    ]
    assert create_db.flushed == 1

    existing = SummerLeaguePlayerResolutionReview(
        source_player_id=source.id,  # type: ignore[arg-type]
        raw_player_name="Old Name",
        nba_stats_person_id="old",
        candidate_players=[],
        status=SummerLeagueReviewStatus.PENDING,
        selected_player_id=99,
        review_note="stale",
        reviewed_at=datetime(2024, 7, 1, 12, 0, 0),
    )
    update_db = _FakeDb([_FakeResult(scalar=existing)])
    updated = await ensure_pending_resolution_review(  # type: ignore[arg-type]
        update_db,
        source,
        [],
    )

    assert updated is existing
    assert updated.raw_player_name == source.raw_player_name
    assert updated.nba_stats_person_id == source.nba_stats_person_id
    assert updated.candidate_players is None
    assert updated.selected_player_id is None
    assert updated.review_note is None
    assert updated.reviewed_at is None


@pytest.mark.asyncio
async def test_record_resolution_review_decision_updates_status_note_and_time() -> None:
    """Review decisions persist lifecycle status and selected player metadata."""
    review = SummerLeaguePlayerResolutionReview(
        id=3,
        source_player_id=5,
        raw_player_name="Review Me",
        nba_stats_person_id="1640001",
        status=SummerLeagueReviewStatus.PENDING,
    )
    decided_at = datetime(2026, 6, 9, 15, 30, 0)
    db = _FakeDb(get_result=review)

    updated = await record_resolution_review_decision(  # type: ignore[arg-type]
        db,
        review_id=3,
        status=SummerLeagueReviewStatus.APPROVED,
        selected_player_id=11,
        review_note="Matches profile",
        reviewed_at=decided_at,
    )

    assert updated is review
    assert review.status == SummerLeagueReviewStatus.APPROVED
    assert review.selected_player_id == 11
    assert review.review_note == "Matches profile"
    assert review.reviewed_at == decided_at
    assert db.flushed == 1

    missing = await record_resolution_review_decision(  # type: ignore[arg-type]
        _FakeDb(get_result=None),
        review_id=999,
        status=SummerLeagueReviewStatus.REJECTED,
    )
    assert missing is None


def test_report_counts_methods_candidates_stubs_and_backfills() -> None:
    """Batch reports aggregate resolution outcomes from individual results."""
    results = [
        SummerLeagueResolutionResult(
            source_player_id=1,
            nba_stats_person_id="100",
            raw_player_name="External Player",
            player_id=10,
            status=SummerLeagueResolutionStatus.EXTERNAL_ID,
            method="EXTERNAL_ID",
            logs_backfilled=2,
        ),
        SummerLeagueResolutionResult(
            source_player_id=2,
            nba_stats_person_id="200",
            raw_player_name="Candidate Player",
            player_id=None,
            status=SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
            method="VECTOR_CANDIDATE",
            candidates=[
                SummerLeagueResolutionCandidate(
                    player_id=20,
                    display_name="Candidate Player",
                    score=0.72,
                )
            ],
        ),
        SummerLeagueResolutionResult(
            source_player_id=3,
            nba_stats_person_id="300",
            raw_player_name="Stub Player",
            player_id=30,
            status=SummerLeagueResolutionStatus.STUB,
            method="STUB",
            stub_created=True,
            logs_backfilled=1,
        ),
    ]

    report = build_resolution_report(year=2024, league_id="15", results=results)

    assert report.total_source_players == 3
    assert report.resolved_source_players == 2
    assert report.unresolved_source_players == 1
    assert report.external_id_resolutions == 1
    assert report.candidate_source_players == 1
    assert report.stubs_created == 1
    assert report.player_game_logs_backfilled == 3


def test_serialize_search_candidates_clamps_scores() -> None:
    """Search candidates are clamped before persistence."""
    payload = _serialize_search_candidates(
        [
            _SearchHit(1, "Low", -0.2),
            _SearchHit(2, "High", 1.2),
        ]
    )

    assert [candidate.score for candidate in payload] == [0.0, 1.0]


@pytest.mark.asyncio
async def test_find_external_id_player_reads_scalar_result() -> None:
    """External-ID lookup returns an integer player id when present."""
    db = _FakeDb([_FakeResult(scalar=42)])

    assert await _find_external_id_player(db, "1640001") == 42  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ensure_external_id_handles_existing_conflict_and_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External-ID ensure respects uniqueness and inserts only when missing."""
    db = _FakeDb()

    async def same_player(db_arg: Any, person_id: str) -> int:
        return 7

    monkeypatch.setattr(service, "_find_external_id_player", same_player)
    assert (
        await _ensure_nba_stats_external_id(  # type: ignore[arg-type]
            db, player_id=7, nba_stats_person_id="1640001"
        )
        is False
    )

    async def other_player(db_arg: Any, person_id: str) -> int:
        return 8

    monkeypatch.setattr(service, "_find_external_id_player", other_player)
    with pytest.raises(ValueError):
        await _ensure_nba_stats_external_id(  # type: ignore[arg-type]
            db, player_id=7, nba_stats_person_id="1640001"
        )

    async def no_player(db_arg: Any, person_id: str) -> None:
        return None

    monkeypatch.setattr(service, "_find_external_id_player", no_player)
    assert (
        await _ensure_nba_stats_external_id(  # type: ignore[arg-type]
            db, player_id=7, nba_stats_person_id="1640001"
        )
        is True
    )
    assert db.flushed == 1


@pytest.mark.asyncio
async def test_backfill_player_game_logs_returns_update_rowcount() -> None:
    """Backfill reports the number of updated Summer League logs."""
    db = _FakeDb([_FakeResult(rowcount=3)])

    assert (
        await _backfill_player_game_logs(  # type: ignore[arg-type]
            db, source_player_id=5, player_id=7
        )
        == 3
    )
    assert (
        await _backfill_player_game_logs(  # type: ignore[arg-type]
            db, source_player_id=None, player_id=7
        )
        == 0
    )


@pytest.mark.asyncio
async def test_create_stub_player_populates_stub_fields() -> None:
    """Stub creation writes the minimal PlayerMaster fields expected by ingest."""
    db = _FakeDb()

    player_id = await _create_stub_player(db, _source(name="New Stub Jr."))  # type: ignore[arg-type]

    assert player_id == 99
    stub = db.added[0]
    assert isinstance(stub, PlayerMaster)
    assert stub.display_name == "New Stub Jr."
    assert stub.first_name == "New"
    assert stub.last_name == "Stub"
    assert stub.suffix == "Jr."
    assert stub.is_stub is True
    assert stub.bio_source == service.STUB_BIO_SOURCE


@pytest.mark.asyncio
async def test_stub_creation_rechecks_variant_matches_at_write_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale no-match plan cannot create a duplicate after a variant appears."""
    source = _source(name="PJ Washington")
    plan = service.SummerLeagueResolutionPlan(
        source_player_id=source.id,  # type: ignore[arg-type]
        kind="UNRESOLVED",
    )

    async def variant_match(
        db: Any, source_name: str
    ) -> service.IdentityVariantMatches:
        return service.IdentityVariantMatches(
            display_names={77: "P.J. Washington Jr."},
            alias_names={},
        )

    async def create_stub(db: Any, source_player: SummerLeagueSourcePlayer) -> int:
        raise AssertionError("late variant match must block stub creation")

    monkeypatch.setattr(service, "find_variant_identity_matches", variant_match)
    monkeypatch.setattr(service, "_create_stub_player", create_stub)

    result = await service.apply_source_player_resolution_plan(
        _FakeDb(),  # type: ignore[arg-type]
        source,
        plan,
        create_stub=True,
    )

    assert result.player_id is None
    assert result.status == SummerLeagueResolutionStatus.VECTOR_CANDIDATE
    assert result.candidates[0].player_id == 77
    assert result.candidates[0].method == "NORMALIZED_SUFFIX_MISMATCH"
    assert result.stub_created is False


@pytest.mark.asyncio
async def test_resolve_source_player_external_id_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External-ID resolution updates source state and backfills logs."""

    async def external(db: Any, person_id: str) -> int:
        return 10

    async def ensure(db: Any, player_id: int, nba_stats_person_id: str) -> bool:
        return False

    async def backfill(db: Any, source_player_id: int | None, player_id: int) -> int:
        return 2

    monkeypatch.setattr(service, "_find_external_id_player", external)
    monkeypatch.setattr(service, "_ensure_nba_stats_external_id", ensure)
    monkeypatch.setattr(service, "_backfill_player_game_logs", backfill)

    source = _source()
    result = await resolve_source_player(_FakeDb(), source)  # type: ignore[arg-type]

    assert result.player_id == 10
    assert result.status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert result.logs_backfilled == 2
    assert source.canonical_player_id == 10


@pytest.mark.asyncio
async def test_resolve_source_player_existing_exact_alias_candidate_and_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cascade branches reuse links, resolve exact/alias, store candidates, and stub."""

    async def no_external(db: Any, person_id: str) -> None:
        return None

    async def ensure(db: Any, player_id: int, nba_stats_person_id: str) -> bool:
        return True

    async def backfill(db: Any, source_player_id: int | None, player_id: int) -> int:
        return 1

    monkeypatch.setattr(service, "_find_external_id_player", no_external)
    monkeypatch.setattr(service, "_ensure_nba_stats_external_id", ensure)
    monkeypatch.setattr(service, "_backfill_player_game_logs", backfill)

    async def no_variant_match(
        db: Any, source_name: str
    ) -> service.IdentityVariantMatches:
        return service.IdentityVariantMatches(display_names={}, alias_names={})

    monkeypatch.setattr(service, "find_variant_identity_matches", no_variant_match)

    manual_resolved_at = datetime(2024, 7, 1, 12, 0, 0)
    existing_source = _source(
        canonical_player_id=11,
        status=SummerLeagueResolutionStatus.MANUAL,
        resolved_at=manual_resolved_at,
        resolved_by="admin@example.test",
    )
    existing = await resolve_source_player(_FakeDb(), existing_source)  # type: ignore[arg-type]
    assert existing.method == "EXISTING_SOURCE"
    assert existing.status == SummerLeagueResolutionStatus.MANUAL
    assert existing_source.resolved_at == manual_resolved_at
    assert existing_source.resolved_by == "admin@example.test"

    async def exact(db: Any, source_name: str) -> service.IdentityVariantMatches:
        return service.IdentityVariantMatches(
            display_names={12: "Exact Prospect"},
            alias_names={},
        )

    monkeypatch.setattr(service, "find_variant_identity_matches", exact)
    exact_result = await resolve_source_player(_FakeDb(), _source())  # type: ignore[arg-type]
    assert exact_result.status == SummerLeagueResolutionStatus.EXACT
    assert exact_result.player_id == 12

    async def alias(db: Any, source_name: str) -> service.IdentityVariantMatches:
        return service.IdentityVariantMatches(
            display_names={},
            alias_names={13: "Alias Prospect"},
        )

    monkeypatch.setattr(service, "find_variant_identity_matches", alias)
    alias_result = await resolve_source_player(_FakeDb(), _source())  # type: ignore[arg-type]
    assert alias_result.status == SummerLeagueResolutionStatus.ALIAS
    assert alias_result.player_id == 13

    async def candidates(
        db: Any, source_player: SummerLeagueSourcePlayer
    ) -> list[SummerLeagueResolutionCandidate]:
        return [
            SummerLeagueResolutionCandidate(
                player_id=14, display_name="Candidate", score=0.7
            )
        ]

    monkeypatch.setattr(service, "find_variant_identity_matches", no_variant_match)
    monkeypatch.setattr(service, "_collect_candidates", candidates)
    candidate_source = _source()
    candidate_result = await resolve_source_player(  # type: ignore[arg-type]
        _FakeDb(), candidate_source
    )
    assert candidate_result.status == SummerLeagueResolutionStatus.VECTOR_CANDIDATE
    assert candidate_source.resolution_candidates == [
        {
            "player_id": 14,
            "display_name": "Candidate",
            "score": 0.7,
            "method": "HYBRID",
        }
    ]

    async def weak_candidates(
        db: Any, source_player: SummerLeagueSourcePlayer
    ) -> list[SummerLeagueResolutionCandidate]:
        return [
            SummerLeagueResolutionCandidate(
                player_id=15, display_name="Weak", score=0.1
            )
        ]

    async def stub(db: Any, source_player: SummerLeagueSourcePlayer) -> int:
        return 16

    monkeypatch.setattr(service, "_collect_candidates", weak_candidates)
    monkeypatch.setattr(service, "_create_stub_player", stub)
    stub_result = await resolve_source_player(  # type: ignore[arg-type]
        _FakeDb(), _source(), create_stub=True
    )
    assert stub_result.status == SummerLeagueResolutionStatus.STUB
    assert stub_result.stub_created is True
    assert stub_result.player_id == 16


@pytest.mark.asyncio
async def test_resolve_source_player_unresolved_stores_weak_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Weak candidates remain unresolved when stub mode is disabled."""

    async def no_external(db: Any, person_id: str) -> None:
        return None

    async def no_variant_match(
        db: Any, source_name: str
    ) -> service.IdentityVariantMatches:
        return service.IdentityVariantMatches(display_names={}, alias_names={})

    async def weak_candidates(
        db: Any, source_player: SummerLeagueSourcePlayer
    ) -> list[SummerLeagueResolutionCandidate]:
        return [
            SummerLeagueResolutionCandidate(
                player_id=15, display_name="Weak", score=0.1
            )
        ]

    monkeypatch.setattr(service, "_find_external_id_player", no_external)
    monkeypatch.setattr(service, "find_variant_identity_matches", no_variant_match)
    monkeypatch.setattr(service, "_collect_candidates", weak_candidates)

    source = _source()
    result = await resolve_source_player(_FakeDb(), source)  # type: ignore[arg-type]

    assert result.status == SummerLeagueResolutionStatus.UNRESOLVED
    assert result.player_id is None
    assert source.resolution_confidence == 0.1
    assert source.resolution_candidates is not None


@pytest.mark.asyncio
async def test_collect_candidates_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate collection serializes hits and degrades to empty on failure."""

    async def fake_search(db: Any, query: str, k: int = 5) -> list[_SearchHit]:
        return [_SearchHit(1, "Candidate", 0.44)]

    monkeypatch.setattr(service, "find_candidate_players", fake_search)
    hits = await service._collect_candidates(_FakeDb(), _source())  # type: ignore[arg-type]
    assert hits == [
        SummerLeagueResolutionCandidate(
            player_id=1,
            display_name="Candidate",
            score=0.44,
        )
    ]

    async def broken_search(db: Any, query: str, k: int = 5) -> list[_SearchHit]:
        raise RuntimeError("offline")

    monkeypatch.setattr(service, "find_candidate_players", broken_search)
    with pytest.raises(SummerLeagueCandidateSearchError):
        await service._collect_candidates(_FakeDb(), _source())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resolve_source_player_search_failure_does_not_create_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub mode treats candidate-search outages as unresolved, not no-match."""

    async def no_external(db: Any, person_id: str) -> None:
        return None

    async def broken_candidates(
        db: Any, source_player: SummerLeagueSourcePlayer
    ) -> list[SummerLeagueResolutionCandidate]:
        raise SummerLeagueCandidateSearchError("offline")

    async def create_stub(db: Any, source_player: SummerLeagueSourcePlayer) -> int:
        raise AssertionError("stub creation should not run after search failure")

    monkeypatch.setattr(service, "_find_external_id_player", no_external)

    async def no_variant_match(
        db: Any, source_name: str
    ) -> service.IdentityVariantMatches:
        return service.IdentityVariantMatches(display_names={}, alias_names={})

    monkeypatch.setattr(service, "find_variant_identity_matches", no_variant_match)
    monkeypatch.setattr(service, "_collect_candidates", broken_candidates)
    monkeypatch.setattr(service, "_create_stub_player", create_stub)

    source = _source()
    result = await resolve_source_player(  # type: ignore[arg-type]
        _FakeDb(), source, create_stub=True
    )

    assert result.player_id is None
    assert result.status == SummerLeagueResolutionStatus.UNRESOLVED
    assert result.method == service.CANDIDATE_SEARCH_FAILED_METHOD
    assert source.canonical_player_id is None


@pytest.mark.asyncio
async def test_load_source_players_and_batch_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch loading and resolution report wiring use the selected scope."""
    source = _source()
    db = _FakeDb([_FakeResult(scalars=[source])])

    assert await _load_source_players(db, year=None, league_id=None) == [source]  # type: ignore[arg-type]
    filtered_db = _FakeDb([_FakeResult(scalars=[source])])
    assert await _load_source_players(filtered_db, year=2024, league_id="15") == [  # type: ignore[arg-type]
        source
    ]

    async def fake_load(
        db_arg: Any, year: int | None, league_id: str | None
    ) -> list[SummerLeagueSourcePlayer]:
        return [source]

    async def fake_resolve(
        db_arg: Any, source_player: SummerLeagueSourcePlayer, create_stub: bool = False
    ) -> SummerLeagueResolutionResult:
        return SummerLeagueResolutionResult(
            source_player_id=source_player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=source_player.raw_player_name,
            player_id=7,
            status=SummerLeagueResolutionStatus.EXACT,
            method="EXACT",
            logs_backfilled=1,
        )

    monkeypatch.setattr(service, "_load_source_players", fake_load)
    monkeypatch.setattr(service, "resolve_source_player", fake_resolve)

    report = await resolve_summer_league_players(  # type: ignore[arg-type]
        _FakeDb(), year=2024, league_id="15"
    )

    assert report.total_source_players == 1
    assert report.resolved_source_players == 1
    assert report.exact_resolutions == 1
    assert report.player_game_logs_backfilled == 1
