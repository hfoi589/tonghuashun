#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

if [ -z "${PYTHON_BIN+x}" ]; then
    if [ -x "$project_root/.venv/bin/python" ]; then
        PYTHON_BIN="$project_root/.venv/bin/python"
    else
        PYTHON_BIN=python3
    fi
fi
export PYTHON_BIN

check_dependencies=1
for argument in "$@"; do
    case "$argument" in
        --help|-h)
            check_dependencies=0
            break
            ;;
    esac
done

if [ "$check_dependencies" -eq 1 ] && ! "$PYTHON_BIN" -c 'import argon2' >/dev/null 2>/dev/null; then
    printf '%s\n' 'PYTHON_DEPENDENCY_MISSING' >&2
    exit 1
fi

exec "$PYTHON_BIN" "$script_dir/macos_deploy.py" "$@"
