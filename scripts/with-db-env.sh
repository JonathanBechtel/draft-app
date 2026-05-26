#!/usr/bin/env bash
# Load .env into the environment, then exec the given command.
#
# Usage: scripts/with-db-env.sh <command> [args...]
# Example: scripts/with-db-env.sh conda run -n draftguru alembic upgrade head
#
# Loads variables from the repo's .env (relative to this script's directory),
# so commands like alembic/psql can see DATABASE_URL without inlining it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/../.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "with-db-env.sh: env file not found at $ENV_FILE" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "with-db-env.sh: no command given" >&2
  echo "usage: $0 <command> [args...]" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

exec "$@"
