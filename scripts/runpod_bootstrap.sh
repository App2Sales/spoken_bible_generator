#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/runpod_install.sh"
"${SCRIPT_DIR}/runpod_start_api.sh" "$@"
