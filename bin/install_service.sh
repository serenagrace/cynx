#!/bin/bash
# Install the cynx unit, root-only USB hooks and narrow sudoers rules.
# This never enables or starts the service. --print-unit renders the unit
# for review or validation without root.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
user=${CYNX_USER:-${SUDO_USER:-$(id -un)}}

fail() {
    echo "$1" >&2
    exit 1
}

render() {
    local group
    group=$(id -gn "$user")
    sed -e "s|@PROJECT@|$root|g" -e "s|@USER@|$user|g" -e "s|@GROUP@|$group|g" \
        "$root/systemd/cynx.service.in"
}

[[ "$user" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "Invalid service user: $user"
id -u "$user" >/dev/null 2>&1 || fail "Unknown service user: $user"
[[ "$root" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "Project path must not contain spaces or special characters."

case "${1:-}" in
    --print-unit)
        render
        exit 0
        ;;
    "") ;;
    *) fail "Usage: sudo $0 [--print-unit]" ;;
esac

[[ $EUID -eq 0 ]] || fail "Run sudo bin/install_service.sh from the service account."
[[ "$user" != root ]] || fail "The network service must not run as root; use sudo from its account or set CYNX_USER."
[[ "$(stat -c %U "$root")" == "$user" ]] || fail "$root must be owned by $user."
[[ -x "$root/env/bin/cynx" ]] || fail "Missing $root/env/bin/cynx; create env and install cynx first."

group=$(id -gn "$user")
if [[ ! -e "$root/config.yaml" ]]; then
    install -o "$user" -g "$group" -m 0644 "$root/config.example.yaml" "$root/config.yaml"
fi

# Root hooks run from a root-owned copy, never from the user-writable checkout.
install -d -o root -g root -m 0755 /usr/local/libexec/cynx
install -o root -g root -m 0755 "$root/bin/service_usb.sh" /usr/local/libexec/cynx/service_usb.sh
install -o root -g root -m 0644 "$root/cynx/nx/gadget.py" /usr/local/libexec/cynx/gadget.py

umask 022
render > /etc/systemd/system/cynx.service

# sudo ignores files containing a dot, so the staged rule is inert until moved.
staged=/etc/sudoers.d/cynx.new
printf '%s ALL=(root) NOPASSWD: %s, %s\n' "$user" \
    '/usr/bin/systemctl --no-block restart cynx.service' \
    '/usr/bin/systemctl --no-block reboot' > "$staged"
chmod 0440 "$staged"
if ! visudo -cf "$staged" >/dev/null; then
    rm -f "$staged"
    fail "Generated sudoers rule failed validation."
fi
mv "$staged" /etc/sudoers.d/cynx

systemctl daemon-reload
echo "Installed cynx.service for $user (not enabled or started)."
echo "Provision .env if needed, then: sudo systemctl enable --now cynx"
