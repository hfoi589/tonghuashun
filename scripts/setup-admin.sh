#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON_BIN:-python3}" "$script_dir/setup-admin.py" "$@"
