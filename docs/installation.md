# Installation

## Requirements

- A Raspberry Pi with a USB device-mode (OTG) controller. The tested setup is a
  Pi 4B using its USB-C port. USB host-only ports cannot act as a controller.
- 64-bit Raspberry Pi OS or another Debian-based Linux with systemd, sudo,
  configfs, `dwc2` and `libcomposite`.
- Python 3.13 or newer with `venv` and `pip`. If the OS ships an older Python,
  upgrade to a release that provides 3.13 rather than replacing the system
  interpreter in place.
- A trusted LAN shared with the Cynthia host. Discovery uses UDP broadcast, so
  it only works within one broadcast domain; routed networks need the Pi's
  address configured on the client.

## USB cabling and power

Connect the Pi's USB-C **device** port to a USB-A host port on the Switch dock
with a data-capable cable. Charge-only cables cannot carry HID traffic.

On a Pi 4 the USB-C connector is also the power input, and a dock port alone may
not supply enough current. Use a power and data arrangement that gives the Pi
adequate power without unsafe backfeeding, such as a suitable powered hub or
splitter. Use Ethernet for SSH so USB changes never cut off management access.

Keep the Switch awake. Sleep, controller assignment and the current game screen
all affect whether a delivered report has the intended effect.

## Enable USB device mode

Add this line to the `[all]` section of `/boot/firmware/config.txt` and reboot:

```ini
dtoverlay=dwc2,dr_mode=peripheral
```

After rebooting, `ls /sys/class/udc` should list one controller.

## Install the package

```bash
sudo apt update
sudo apt install git python3 python3-venv python3-pip
git clone https://github.com/serenagrace/cynx.git ~/src/cynx
cd ~/src/cynx
python3 --version                   # 3.13 or newer
python3 -m venv env
env/bin/python -m pip install -e .
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

Set `CYPI_PASSPHRASE` in `.env` if Cynthia uses one; see
[configuration](configuration.md). Never copy tokens or secrets from a shared
drive.

## Install the service

Run the installer with sudo from the account that will own the service. That
account must own the checkout and must not be root.

```bash
sudo bin/install_service.sh
sudo systemctl enable --now cynx
systemctl is-enabled cynx            # enabled
systemctl is-active cynx             # active
```

The installer:

- Renders `systemd/cynx.service.in` into `/etc/systemd/system/cynx.service` for
  the invoking account and checkout path. Set `CYNX_USER` to choose a different
  unprivileged account. Preview the unit without root using
  `bin/install_service.sh --print-unit`.
- Copies the root USB hooks (`bin/service_usb.sh` and `cynx/nx/gadget.py`) to
  root-owned `/usr/local/libexec/cynx`. They run with the system Python in
  isolated mode, never from the user-writable checkout or virtualenv.
- Adds `/etc/sudoers.d/cynx`, allowing that account to run exactly
  `systemctl --no-block restart cynx.service` and `systemctl --no-block reboot`.
- Creates `config.yaml` from the example if it is missing.

It never enables or starts the service. Rerun it after changing the USB hooks
or the unit template; the service picks up changes on its next start.

## How the service runs

The unit starts after `network-online.target`, uses `Restart=always`, and runs
`env/bin/cynx --config config.yaml` as the unprivileged account from the
checkout directory.

- **Before start**, a root-only `ExecStartPre` hook loads `libcomposite` and
  prepares this project's configfs gadget **unbound**. It grants the service
  account access to the gadget's `UDC` binding file and a private
  `/dev/cynx/hidg0` character node. The node is created from the function's
  reserved device number on devtmpfs, so it survives every bind and unbind.
  Missing USB hardware leaves the service listening idle.
- **After stop or crash**, a root-only `ExecStopPost` hook force-unbinds the
  gadget.
- The service never restores a previous USB connection on start. USB stays
  unbound until an authenticated client sends `connect`.
- Other configfs gadgets are never removed or modified. If another gadget holds
  the UDC, `connect` fails instead of taking it.

## Verify

```bash
cat /sys/kernel/config/usb_gadget/cynx/UDC       # empty while idle
cat /sys/class/udc/*/state                       # "not attached" while idle
ss -lntu | grep -E ':314[01]\b'                  # TCP 3140, 3141 and UDP 3140
journalctl -u cynx -n 20 --no-pager
```

## Standalone wired test

Without the service, you can bind the gadget directly and press L, R, then B.
Stop the service first so only one process writes HID reports, and open a
screen where those buttons are safe.

```bash
sudo systemctl stop cynx
sudo bin/usb_setup.sh                # binds and grants you /dev/hidg0
env/bin/cynx --test                  # optional: --timeout 15
```

Each press is held for 150 ms with a 500 ms gap, and all controls are released
on exit or interruption. Controller assignment may require the Change
Grip/Order screen or pressing L and R together. Afterwards, run
`sudo systemctl start cynx`; its start hook prepares the gadget unbound again.

## Uninstall

```bash
sudo systemctl disable --now cynx
sudo rm /etc/systemd/system/cynx.service /etc/sudoers.d/cynx
sudo rm -r /usr/local/libexec/cynx
sudo systemctl daemon-reload
```
