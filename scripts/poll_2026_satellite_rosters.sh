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
  # --force: always overwrite the on-disk snapshot with a fresh fetch. Without
  # it, an empty snapshot written before rosters were published is never
  # refreshed (the fetcher is idempotent), so the loader keeps reading stale
  # empty data and loads 0 players. The loader itself is idempotent (upsert +
  # roster diff), so re-fetching every run is safe.
  summary=$(run scripts/fetch_summer_league_rosters.py --year 2026 --league-id "$lid" --force 2>&1 | grep -E "teams=|players=" | tail -1)
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

  # C3 — bio: scrape bbref bios for the cohort's bbref-having players, then
  # ingest the freshly-written (timestamped) CSV. bbref pages are cached, so
  # repeated runs only fetch newly-added players.
  echo "L$lid: C3 bio scrape + ingest"
  # Capture the scraper's output + exit status so we ingest exactly the CSV
  # THIS scrape emitted (it prints "... -> <path>.csv"). Globbing the output
  # dir instead would re-ingest a stale/other-league CSV whenever this scrape
  # fails or writes nothing — and the filename is date-stamped, so both leagues
  # share it within a day.
  bio_out=$(run scripts/bbref_bio_scraper.py --summer-league-year 2026 --summer-league-league-id "$lid" 2>&1)
  bio_status=$?
  echo "$bio_out" | grep -iE "wrote|scraped|manual.review|error" | tail -2
  bio_csv=$(echo "$bio_out" | sed -nE 's/.*-> (.*\.csv).*/\1/p' | tail -1)
  if [[ $bio_status -eq 0 && -n "$bio_csv" && -f "$bio_csv" ]]; then
    run scripts/ingest_player_bios.py --file "$bio_csv" 2>&1 | grep -iE "updated|ingest|matched|error" | tail -2
  else
    echo "  (bio scrape wrote no CSV for L$lid — skipping ingest)"
  fi

  # C4 — college stats: cohort players with school + bbref id; --only-missing
  # skips players already enriched, so this is light on repeat runs.
  echo "L$lid: C4 college stats (only-missing)"
  run scripts/scrape_college_stats.py --sl-cohort --sl-year 2026 --sl-league-id "$lid" --only-missing 2>&1 | grep -iE "attempted|no.source|no source|failed|error" | tail -2
done

echo "===== poll run complete $(date '+%H:%M:%S') ====="
