#!/usr/bin/env bash
set -euo pipefail
task_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/tides-coverage-uv-cache}"
export TIDES_WORKBENCH_DATA_ROOT="$task_root"
export PYTHONDONTWRITEBYTECODE=1
exec uv run --offline --no-sync --project "$task_root" python "$task_root/tools/merge_review/workbench.py" "$@"
