#!/bin/bash
# Refresh installed package metadata without changing dependencies.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python="$root/env/bin/python"

if [[ ! -x "$python" ]]; then
    echo "Missing $python; create the env virtualenv first." >&2
    exit 1
fi

expected=$(
    "$python" - "$root/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as file:
    print(tomllib.load(file)["project"]["version"])
PY
)

"$python" -m pip install --no-deps -e "$root"
installed=$("$python" -c "from importlib.metadata import version; print(version('cynx'))")

if [[ "$installed" != "$expected" ]]; then
    echo "Installed cynx $installed, but pyproject.toml declares $expected." >&2
    exit 1
fi

echo "Installed cynx $installed."
