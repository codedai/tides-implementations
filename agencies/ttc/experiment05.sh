#!/usr/bin/env bash
set -euo pipefail
task_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/tides-coverage-uv-cache}"
export PYTHONDONTWRITEBYTECODE=1
cd "$task_root"
exec uv run --offline --no-sync --project "$task_root" python -m tools.experiment05 "$@"
