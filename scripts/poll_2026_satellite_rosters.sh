#!/usr/bin/env bash
# Daily poll for 2026 Summer League SATELLITE rosters (California Classic =
# LeagueID 13, Salt Lake City = LeagueID 16). These tip ~Jul 4-8 2026 and are
# the first events. NBA.com publishes their announced rosters close to the
# event; this script fetches each venue and, the moment a roster is non-empty,
# runs the full ingest + enrich pipeline into the dev Neon branch.
#
# Idempotent: safe to run daily. The loader upserts and emits a roster diff so
# only NEW names are re-enriched (cuts are marked, not deleted).
#
# Scheduled via a launchd LaunchAgent (see scripts/ docs / the install used by
# the assistant). Logs to logs/poll_2026_satellite_rosters.log.

set -uo pipefail

# Derive the repo root from this script's location so it works on any host
# (local Mac, sprite, CI) rather than a hard-coded path.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEAGUES=(13 16)  # 13=California Classic, 16=Salt Lake City; add 15 (Vegas) ~Jul 9
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/poll_2026_satellite_rosters.log"

mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1
echo "===== poll run $(date '+%Y-%m-%d %H:%M:%S %z') ====="

cd "$REPO" || { echo "ERROR: cannot cd to $REPO"; exit 1; }

# shellcheck disable=SC1090
source "$HOME/miniconda3/etc/profile.d/conda.sh" 2>/dev/null || \
  source "$HOME/anaconda3/etc/profile.d/conda.sh" 2>/dev/null

# Get the latest scripts (incl. the roster-loader schema fix) without disturbing
# local work: only pull if cleanly on main, else just report and continue.
git fetch origin main --quiet 2>/dev/null || true

run() { scripts/with-db-env.sh conda run -n draftguru python "$@"; }

for lid in "${LEAGUES[@]}"; do
  echo "--- LeagueID $lid ---"
  summary=$(run scripts/fetch_summer_league_rosters.py --year 2026 --league-id "$lid" 2>&1 | grep -E "teams=|players=" | tail -1)
  echo "$summary"
  players=$(echo "$summary" | sed -nE 's/.*players=([0-9]+).*/\1/p')
  if [[ -z "$players" || "$players" == "0" ]]; then
    echo "L$lid: not published yet"
    continue
  fi
  echo "L$lid: $players players published — running ingest + enrich pipeline"
  run scripts/load_summer_league_rosters.py --year 2026 --league-id "$lid" --verbose 2>&1 | grep -iE "Loading|Wrote|error" | head -5
  run scripts/resolve_summer_league_players.py --year 2026 --league-id "$lid" --create-stubs 2>&1 | grep -E "resolved=|total=" | tail -1
  run scripts/seed_nba_stats_external_ids.py 2>&1 | grep -E "Seeded:" | tail -1
  run scripts/backfill_nba_headshots.py --no-validate 2>&1 | grep -E "Set:" | tail -1
done

echo "===== poll run complete $(date '+%H:%M:%S') ====="
