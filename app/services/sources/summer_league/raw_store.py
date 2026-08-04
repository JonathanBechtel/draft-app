"""Local raw snapshot storage for Summer League NBA Stats payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.services.sources.summer_league.endpoints import (
    normalize_league_id,
    normalize_season,
)
from app.services.sources.summer_league.manifest import SummerLeagueRawManifest


@dataclass(frozen=True)
class SnapshotWriteResult:
    """Result of writing or skipping one raw snapshot."""

    path: Path
    relative_path: str
    written: bool
    skipped: bool


class SummerLeagueRawStore:
    """Deterministic local storage for raw NBA Stats JSON snapshots."""

    def __init__(
        self, root: Path | str = Path("data/raw/nba_stats/summer_league")
    ) -> None:
        """Initialize the store.

        Args:
            root: Root directory for Summer League raw snapshots.
        """
        self.root = Path(root)

    def season_dir(self, *, year: int | str, league_id: str) -> Path:
        """Return the directory for one year and LeagueID."""
        return self.root / normalize_season(year) / normalize_league_id(league_id)

    def season_file(self, *, year: int | str, league_id: str, name: str) -> Path:
        """Return a season-level JSON file path."""
        filename = name if name.endswith(".json") else f"{name}.json"
        return self.season_dir(year=year, league_id=league_id) / filename

    def game_file(
        self,
        *,
        year: int | str,
        league_id: str,
        game_id: str,
        endpoint: str,
    ) -> Path:
        """Return a game-level endpoint JSON file path."""
        filename = endpoint if endpoint.endswith(".json") else f"{endpoint}.json"
        return (
            self.season_dir(year=year, league_id=league_id)
            / "games"
            / game_id.strip()
            / filename
        )

    def relative_path(self, path: Path) -> str:
        """Return a stable path relative to the raw root when possible."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def write_json(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> SnapshotWriteResult:
        """Write a JSON payload unless an existing file should be reused.

        Args:
            path: Destination path.
            payload: JSON-compatible object payload.
            force: Overwrite an existing file when true.

        Returns:
            Structured write result.
        """
        relative_path = self.relative_path(path)
        if path.exists() and not force:
            return SnapshotWriteResult(
                path=path,
                relative_path=relative_path,
                written=False,
                skipped=True,
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return SnapshotWriteResult(
            path=path,
            relative_path=relative_path,
            written=True,
            skipped=False,
        )

    def read_json(self, path: Path) -> dict[str, Any]:
        """Read a JSON snapshot from disk."""
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return payload

    def write_manifest(
        self,
        manifest: SummerLeagueRawManifest,
        *,
        force: bool = True,
    ) -> SnapshotWriteResult:
        """Write a run manifest to its canonical season path."""
        path = self.season_file(
            year=manifest.year,
            league_id=manifest.league_id,
            name="manifest.json",
        )
        relative_path = self.relative_path(path)
        if path.exists() and not force:
            return SnapshotWriteResult(
                path=path,
                relative_path=relative_path,
                written=False,
                skipped=True,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.to_json())
        return SnapshotWriteResult(
            path=path,
            relative_path=relative_path,
            written=True,
            skipped=False,
        )
