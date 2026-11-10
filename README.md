# CyNX

[![Tests](https://github.com/serenagrace/cynx/actions/workflows/tests.yml/badge.svg)](https://github.com/serenagrace/cynx/actions/workflows/tests.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: none](https://img.shields.io/badge/license-none-lightgrey.svg)](#license)

**Exclusive LAN-controlled wired Nintendo Switch controller for Raspberry Pi**

CyNX turns a Raspberry Pi with a USB device-mode port into a wired
HORIPAD-compatible controller. [Cynthia](https://github.com/serenagrace/cynthia)
discovers the Pi on the local network, claims an authenticated session, and
sends one controller step at a time. CyNX owns the USB timing, stops input on
request, and safely detaches when the client goes away.

## What it does

- Listens idle at boot; USB controller emulation starts only after an
  authenticated client asks to `connect`.
- Gives the first authenticated host exclusive use of both channels: commands
  and discovery on port 3140, controller steps on port 3141.
- Runs single serialized Cynthia input steps such as `A`, `L R`, `hold C` or
  `(0.1,0.2) H`, with prompt `accepted` and timed `completed` events.
- Answers `status`, `stop`, `disconnect`, `release`, `restart` and `reboot`
  from a fixed allowlist; it never runs shell commands from the network.
- On network loss, waits a short reconnect grace period, then runs the same
  HOME-menu cleanup as Cynthia's Bluetooth backend and unbinds USB.
- Logs locally and reports recent errors in `status` so Cynthia can alert its
  owner.

A delivered report is not proof that a game acted on it. The Switch must be
awake and on a suitable screen. SL/SR, analog sticks, motion and wake from
sleep are not supported.

## Quick start

CyNX needs a Raspberry Pi with a USB device-mode port (tested on a Pi 4B),
64-bit Raspberry Pi OS with systemd, and Python 3.13 or newer. Enable USB
device mode by adding `dtoverlay=dwc2,dr_mode=peripheral` to the `[all]` section
of `/boot/firmware/config.txt`, then reboot.

```bash
git clone https://github.com/serenagrace/cynx.git ~/src/cynx
cd ~/src/cynx
python3 -m venv env
env/bin/python -m pip install -e .
cp config.example.yaml config.yaml
cp .env.example .env && chmod 600 .env   # optional CYNX_PASSPHRASE
sudo bin/install_service.sh              # installs, does not start
sudo systemctl enable --now cynx
```

Connect the Pi's USB-C device port to the dock with a data-capable cable and
power the Pi adequately; see [installation](docs/installation.md) for cabling,
power and verification.

## Documentation

- [Installation](docs/installation.md): hardware, OS, cabling and autostart
- [Configuration and security](docs/configuration.md): ports, passphrase and logs
- [Protocol](docs/protocol.md): discovery, commands, steps and error codes
- [Operations](docs/operations.md): status, recovery, restart and troubleshooting
- [Development](docs/development.md): tests, formatting, CI and versioning

## Development

```bash
env/bin/python -m pip install -e ".[dev]"
bin/black.sh
env/bin/python -m pytest
```

The test suite uses a fake USB backend and loopback sockets; it needs no
Switch, root access or Raspberry Pi.

## Version

The package version is declared in `pyproject.toml` and reported by
`cynx --version` and in `status`.

## License

CyNX does not currently have a license. No permission to use, copy, modify or
distribute it is granted beyond what applicable law allows.
