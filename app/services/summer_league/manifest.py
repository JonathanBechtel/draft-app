"""Manifest primitives for Summer League raw ingestion runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.summer_league.endpoints import (
    SUPPORTED_SUMMER_LEAGUES,
    normalize_league_id,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SummerLeagueRawError:
    """One recoverable raw-ingestion error."""

    endpoint: str
    message: str
    game_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serialize this error for manifest JSON."""
        return {
            "endpoint": self.endpoint,
            "game_id": self.game_id,
            "message": self.message,
        }


@dataclass
class SummerLeagueRawManifest:
    """Run summary for one Summer League year and LeagueID."""

    year: int
    league_id: str
    venue: str
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime | None = None
    team_gamelog_rows: int = 0
    player_gamelog_rows: int = 0
    game_ids: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    errors: list[SummerLeagueRawError] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        *,
        year: int,
        league_id: str,
        venue: str | None = None,
        started_at: datetime | None = None,
    ) -> "SummerLeagueRawManifest":
        """Create a manifest with venue inferred from ``league_id`` by default."""
        normalized_league_id = normalize_league_id(league_id)
        resolved_venue = venue or SUPPORTED_SUMMER_LEAGUES[normalized_league_id].slug
        return cls(
            year=year,
            league_id=normalized_league_id,
            venue=resolved_venue,
            started_at=started_at or _utc_now(),
        )

    @property
    def game_count(self) -> int:
        """Return the number of discovered unique game IDs."""
        return len(self.game_ids)

    def finish(self, finished_at: datetime | None = None) -> None:
        """Mark the manifest as complete."""
        self.finished_at = finished_at or _utc_now()

    def record_file(self, relative_path: str, *, written: bool) -> None:
        """Record a written or skipped raw snapshot file."""
        if written:
            self.files_written.append(relative_path)
        else:
            self.files_skipped.append(relative_path)

    def add_error(
        self, *, endpoint: str, message: str, game_id: str | None = None
    ) -> None:
        """Record a recoverable endpoint or game-level error."""
        self.errors.append(
            SummerLeagueRawError(endpoint=endpoint, game_id=game_id, message=message)
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this manifest to a JSON-compatible dictionary."""
        return {
            "year": self.year,
            "league_id": self.league_id,
            "venue": self.venue,
            "started_at": _isoformat_z(self.started_at),
            "finished_at": _isoformat_z(self.finished_at),
            "team_gamelog_rows": self.team_gamelog_rows,
            "player_gamelog_rows": self.player_gamelog_rows,
            "game_ids": list(self.game_ids),
            "game_count": self.game_count,
            "files_written": list(self.files_written),
            "files_skipped": list(self.files_skipped),
            "errors": [error.to_dict() for error in self.errors],
        }

    def to_json(self) -> str:
        """Serialize this manifest as stable pretty-printed JSON."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
