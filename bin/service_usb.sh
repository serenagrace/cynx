#!/bin/bash
# Root-only systemd hooks; the network listener never runs as root.
# Usage: service_usb.sh prepare USER | unbind
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo "USB preparation requires root." >&2
    exit 1
fi
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
gadget="$here/gadget.py"
if [[ ! -f "$gadget" ]]; then
    gadget="$here/../cynx/nx/gadget.py"
fi
cd /
case "${1:-}" in
    prepare)
        modprobe libcomposite || echo "libcomposite unavailable; USB may remain idle." >&2
        user=${2:?Usage: $0 prepare USER}
        exec /usr/bin/python3 -I "$gadget" --prepare --user "$user"
        ;;
    unbind)
        exec /usr/bin/python3 -I "$gadget" --unbind
        ;;
    *)
        echo "Usage: $0 prepare USER | unbind" >&2
        exit 2
        ;;
esac
