"""Raw Summer League NBA Stats ingestion orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from app.services.summer_league.endpoints import (
    build_boxscore_params,
    build_leaguegamelog_params,
    build_playbyplay_params,
    build_shotchart_params,
    normalize_league_id,
)
from app.services.summer_league.manifest import SummerLeagueRawManifest
from app.services.summer_league.nba_stats_client import extract_result_sets
from app.services.summer_league.raw_store import SummerLeagueRawStore

Payload = Mapping[str, object]

GAME_ENDPOINTS = (
    "boxscoretraditionalv2",
    "boxscoreadvancedv2",
    "boxscorescoringv2",
    "playbyplayv2",
    "shotchartdetail",
)


class SummerLeagueRequiredGamelogError(RuntimeError):
    """Raised when a required season gamelog cannot be collected."""


class NBAStatsJSONClient(Protocol):
    """Protocol for the NBA Stats client behavior used by raw ingestion."""

    def fetch_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, object]:
        """Fetch one NBA Stats endpoint as JSON."""
        ...


@dataclass(frozen=True)
class RawIngestionOptions:
    """Options for one Summer League year/LeagueID raw-ingestion run.

    Attributes:
        year: Summer League season year.
        league_id: NBA.com Summer League LeagueID.
        limit_games: Optional cap on how many discovered game IDs get
            per-game endpoint calls. Ignored when ``game_ids`` is set.
        dry_run: Plan file writes without performing them.
        force: Overwrite existing raw snapshots instead of reusing them.
        delay_seconds: Sleep applied after each NBA Stats call.
        skip_endpoints: Game endpoints to omit from ``GAME_ENDPOINTS``.
        game_ids: Explicit game IDs to fetch endpoints for, overriding the
            IDs discovered from the season team gamelog. ``None`` (the
            default) preserves prior behavior -- game IDs come from the
            season gamelog, optionally narrowed by ``limit_games``. An
            empty tuple selects zero games, so no per-game endpoint calls
            are made at all. The required season team/player gamelog
            fetches still run either way -- this field only scopes the
            per-game endpoint loop (boxscore/pbp/shotchart), which is what
            targeted live refreshes (see
            ``app.services.summer_league.live_ingestion``) need to force
            without redownloading every game in the season.
    """

    year: int
    league_id: str
    limit_games: int | None = None
    dry_run: bool = False
    force: bool = False
    delay_seconds: float = 0.0
    skip_endpoints: tuple[str, ...] = ()
    game_ids: tuple[str, ...] | None = None


class SummerLeagueRawIngestor:
    """Fetch raw NBA Stats payloads and store deterministic snapshots."""

    def __init__(
        self,
        *,
        client: NBAStatsJSONClient,
        store: SummerLeagueRawStore,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize the ingestor.

        Args:
            client: NBA Stats client.
            store: Raw snapshot store.
            sleep: Injectable sleep function for tests.
            progress: Optional callback for observable ingestion progress.
        """
        self.client = client
        self.store = store
        self.sleep = sleep
        self.progress = progress

    def fetch_year_league(
        self, options: RawIngestionOptions
    ) -> SummerLeagueRawManifest:
        """Fetch one Summer League year and LeagueID into raw storage."""
        league_id = normalize_league_id(options.league_id)
        manifest = SummerLeagueRawManifest.start(year=options.year, league_id=league_id)
        self._progress(f"start {options.year}/{league_id}")

        team_payload = self._fetch_season_gamelog(
            options=options,
            manifest=manifest,
            league_id=league_id,
            player_or_team="T",
            filename="leaguegamelog_team",
        )
        if team_payload is None:
            self._finish_manifest(manifest)
            raise SummerLeagueRequiredGamelogError(
                f"Required team leaguegamelog failed for {options.year}/{league_id}"
            )
        manifest.team_gamelog_rows = _first_result_set_row_count(team_payload)
        manifest.game_ids = extract_game_ids(team_payload)

        player_payload = self._fetch_season_gamelog(
            options=options,
            manifest=manifest,
            league_id=league_id,
            player_or_team="P",
            filename="leaguegamelog_player",
        )
        if player_payload is None:
            self._finish_manifest(manifest)
            raise SummerLeagueRequiredGamelogError(
                f"Required player leaguegamelog failed for {options.year}/{league_id}"
            )
        manifest.player_gamelog_rows = _first_result_set_row_count(player_payload)

        if options.game_ids is not None:
            # Explicit selection wins outright: limit_games is a discovery-list
            # narrowing tool and doesn't apply once the caller has named exact
            # IDs (an empty tuple here deliberately means "fetch none").
            game_ids_to_fetch = list(options.game_ids)
        else:
            game_ids_to_fetch = manifest.game_ids
            if options.limit_games is not None:
                game_ids_to_fetch = game_ids_to_fetch[: options.limit_games]

        skipped_endpoints = set(options.skip_endpoints)
        endpoints_to_fetch = [
            endpoint for endpoint in GAME_ENDPOINTS if endpoint not in skipped_endpoints
        ]
        total_games = len(game_ids_to_fetch)
        for index, game_id in enumerate(game_ids_to_fetch, start=1):
            self._progress(
                f"game {index}/{total_games} {options.year}/{league_id}/{game_id}"
            )
            for endpoint in endpoints_to_fetch:
                self._fetch_game_endpoint(
                    options=options,
                    manifest=manifest,
                    league_id=league_id,
                    game_id=game_id,
                    endpoint=endpoint,
                )

        self._finish_manifest(manifest)
        return manifest

    def _fetch_season_gamelog(
        self,
        *,
        options: RawIngestionOptions,
        manifest: SummerLeagueRawManifest,
        league_id: str,
        player_or_team: str,
        filename: str,
    ) -> Payload | None:
        path = self.store.season_file(
            year=options.year,
            league_id=league_id,
            name=filename,
        )
        endpoint = "leaguegamelog"
        params = build_leaguegamelog_params(
            league_id=league_id,
            season=options.year,
            player_or_team=player_or_team,  # type: ignore[arg-type]
        )
        if path.exists() and not options.force:
            manifest.record_file(self.store.relative_path(path), written=False)
            self._progress(f"reuse {self.store.relative_path(path)}")
            return self.store.read_json(path)

        if options.dry_run:
            manifest.record_file(self.store.relative_path(path), written=False)
            self._progress(f"plan {self.store.relative_path(path)}")
            return self._safe_fetch(
                endpoint=endpoint,
                params=params,
                manifest=manifest,
                delay_seconds=options.delay_seconds,
            )

        payload = self._safe_fetch(
            endpoint=endpoint,
            params=params,
            manifest=manifest,
            delay_seconds=options.delay_seconds,
        )
        if payload is None:
            return None
        result = self.store.write_json(path, payload, force=options.force)
        manifest.record_file(result.relative_path, written=result.written)
        self._progress(
            ("wrote" if result.written else "reuse") + f" {result.relative_path}"
        )
        return payload

    def _fetch_game_endpoint(
        self,
        *,
        options: RawIngestionOptions,
        manifest: SummerLeagueRawManifest,
        league_id: str,
        game_id: str,
        endpoint: str,
    ) -> None:
        path = self.store.game_file(
            year=options.year,
            league_id=league_id,
            game_id=game_id,
            endpoint=endpoint,
        )
        if options.dry_run:
            manifest.record_file(self.store.relative_path(path), written=False)
            self._progress(f"plan {self.store.relative_path(path)}")
            return
        if path.exists() and not options.force:
            manifest.record_file(self.store.relative_path(path), written=False)
            self._progress(f"reuse {self.store.relative_path(path)}")
            return

        params = _game_endpoint_params(
            endpoint=endpoint,
            league_id=league_id,
            year=options.year,
            game_id=game_id,
        )
        payload = self._safe_fetch(
            endpoint=endpoint,
            params=params,
            manifest=manifest,
            game_id=game_id,
            delay_seconds=options.delay_seconds,
        )
        if payload is None:
            return
        result = self.store.write_json(path, payload, force=options.force)
        manifest.record_file(result.relative_path, written=result.written)
        self._progress(
            ("wrote" if result.written else "reuse") + f" {result.relative_path}"
        )

    def _safe_fetch(
        self,
        *,
        endpoint: str,
        params: Mapping[str, str],
        manifest: SummerLeagueRawManifest,
        game_id: str | None = None,
        delay_seconds: float = 0.0,
    ) -> Payload | None:
        try:
            return self.client.fetch_json(endpoint, params)
        except Exception as exc:
            manifest.add_error(
                endpoint=endpoint,
                game_id=game_id,
                message=f"{type(exc).__name__}: {exc}",
            )
            location = f" game={game_id}" if game_id else ""
            self._progress(f"error {endpoint}{location}: {type(exc).__name__}: {exc}")
            return None
        finally:
            if delay_seconds > 0:
                self.sleep(delay_seconds)

    def _progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def _finish_manifest(self, manifest: SummerLeagueRawManifest) -> None:
        manifest.finish()
        self.store.write_manifest(manifest, force=True)
        self._progress(
            f"finish {manifest.year}/{manifest.league_id}: games={manifest.game_count} "
            f"written={len(manifest.files_written)} "
            f"skipped={len(manifest.files_skipped)} errors={len(manifest.errors)}"
        )


def extract_game_ids(payload: Payload) -> list[str]:
    """Return unique game IDs from an NBA Stats gamelog payload."""
    result_sets = extract_result_sets(payload)
    if not result_sets:
        return []
    headers = result_sets[0].headers
    try:
        game_id_index = headers.index("GAME_ID")
    except ValueError:
        return []

    game_ids: list[str] = []
    seen: set[str] = set()
    for row in result_sets[0].rows:
        if game_id_index >= len(row) or row[game_id_index] is None:
            continue
        game_id = str(row[game_id_index])
        if game_id in seen:
            continue
        seen.add(game_id)
        game_ids.append(game_id)
    return game_ids


def _first_result_set_row_count(payload: Payload) -> int:
    result_sets = extract_result_sets(payload)
    return len(result_sets[0].rows) if result_sets else 0


def _game_endpoint_params(
    *,
    endpoint: str,
    league_id: str,
    year: int,
    game_id: str,
) -> dict[str, str]:
    if endpoint == "playbyplayv2":
        return build_playbyplay_params(game_id)
    if endpoint == "shotchartdetail":
        return build_shotchart_params(game_id=game_id)
    return build_boxscore_params(game_id)
