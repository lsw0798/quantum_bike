#!/bin/bash
# macOS 실행 도우미. config.toml에 지정한 QPU 또는 EMU_FRESNEL에 제출합니다.
set -euo pipefail
cd "$(dirname "$0")"
export MPLCONFIGDIR="$PWD/.venv/matplotlib"
if [ ! -x .venv/bin/python ]; then
  python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 이상이 필요합니다."'
  python3 -m venv .venv
fi
requirement_file="requirements.txt"
if [ -f requirements.lock.txt ]; then
  requirement_file="requirements.lock.txt"
fi
requirement_hash=$(.venv/bin/python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$requirement_file")
installed_hash=""
if [ -f .venv/.truck-mis-installed ]; then
  installed_hash=$(cat .venv/.truck-mis-installed)
fi
if [ "$installed_hash" != "$requirement_hash" ]; then
  .venv/bin/python -m pip install -r "$requirement_file"
  echo "$requirement_hash" > .venv/.truck-mis-installed
fi
exec .venv/bin/python run.py run "$@"
