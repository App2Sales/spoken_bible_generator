#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends git curl ffmpeg libsndfile1 sqlite3
fi

"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install -r requirements.txt

mkdir -p /workspace/assets /workspace/outputs /data/voices

"${PYTHON_BIN}" - <<'PY'
import torch
import omnivoice

print(f"torch {torch.__version__}")
print(f"cuda {torch.cuda.is_available()}")
print("omnivoice import ok")
PY

echo "RunPod dependencies installed."
