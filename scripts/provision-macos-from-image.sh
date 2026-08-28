#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
exec python3 scripts/macos_deploy.py --mode provision "$@"
