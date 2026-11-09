#!/usr/bin/env bash
set -euo pipefail

# Resolve the project directory so the script can be called from anywhere.
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_DIR"

if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    exec "$PROJECT_DIR/.venv/bin/python" -m console "$@"
fi

exec python3 -m console "$@"
