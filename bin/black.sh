#!/bin/bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python="$root/env/bin/python"

"$python" -m isort "$root"
"$python" -m black "$root"
