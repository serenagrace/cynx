#!/bin/bash
# Create the gadget after boot; USB peripheral mode must already be enabled.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run sudo bin/usb_setup.sh." >&2
    exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
modprobe libcomposite
cd "$root"
exec "$root/env/bin/python" -m cynx.nx.gadget
