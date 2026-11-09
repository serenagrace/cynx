"""Configure this project's USB HID gadget as root.

Run through bin/usb_setup.sh for the standalone demo, or through the service's
root hook, which prepares the gadget unbound with --prepare.
"""

import os
import pwd
import stat
import time
from pathlib import Path

GADGET = Path("/sys/kernel/config/usb_gadget/cynx")

# Standard HID fields: 14 buttons, padding, hat, four axes, and a reserved byte.
REPORT_DESCRIPTOR = bytes.fromhex(
    "05 01 09 05 a1 01 "
    "15 00 25 01 35 00 45 01 75 01 95 0e 05 09 19 01 29 0e 81 02 "
    "95 02 81 01 "
    "05 01 25 07 46 3b 01 75 04 95 01 65 14 09 39 81 42 "
    "65 00 95 01 81 01 "
    "26 ff 00 46 ff 00 09 30 09 31 09 32 09 35 75 08 95 04 81 02 "
    "75 08 95 01 81 01 c0"
)


def setup(*, bind=True, user_name=None) -> None:
    """Create only this gadget; service preparation leaves it UNBOUND."""
    if os.geteuid() != 0:
        raise RuntimeError("Run sudo bin/usb_setup.sh.")
    controllers = sorted(Path("/sys/class/udc").glob("*"))
    if len(controllers) != 1:
        if not bind:
            if (GADGET / "UDC").exists() and (GADGET / "UDC").read_text().strip():
                (GADGET / "UDC").write_text("\n")
            print("No unique UDC; leaving USB unavailable and the service idle.")
            return
        raise RuntimeError(
            "Expected one USB device controller. Enable "
            "dtoverlay=dwc2,dr_mode=peripheral in /boot/firmware/config.txt."
        )
    root = GADGET.parent
    if not root.is_dir():
        if not bind:
            print("USB ConfigFS unavailable; the service will listen idle.")
            return
        raise RuntimeError("USB ConfigFS is unavailable; load libcomposite.")
    if bind and any(path != GADGET for path in root.iterdir()):
        raise RuntimeError("Another USB gadget exists; refusing to alter it.")
    GADGET.mkdir(exist_ok=True)

    def write(name, value):
        (GADGET / name).write_text(value)

    bound = (GADGET / "UDC").read_text().strip()
    if bound and not bind:
        write("UDC", "\n")
        bound = ""
    if not bound:
        write("idVendor", "0x0f0d")
        write("idProduct", "0x00c1")
        write("bcdUSB", "0x0200")
        write("bcdDevice", "0x0100")
        strings = GADGET / "strings/0x409"
        strings.mkdir(parents=True, exist_ok=True)
        write("strings/0x409/serialnumber", "cynx0001")
        write("strings/0x409/manufacturer", "cynx")
        write("strings/0x409/product", "cynx Wired Controller")
        config = GADGET / "configs/c.1"
        config.mkdir(parents=True, exist_ok=True)
        write("configs/c.1/MaxPower", "500")
        write("configs/c.1/bmAttributes", "0x80")
        function = GADGET / "functions/hid.usb0"
        function.mkdir(parents=True, exist_ok=True)
        link = config / "hid.usb0"
        # Configfs locks function options while any configuration references it,
        # even when the UDC itself is unbound.
        if link.is_symlink():
            link.unlink()
        write("functions/hid.usb0/protocol", "0")
        write("functions/hid.usb0/subclass", "0")
        # Linux derives endpoint packet size from report_length; Switch needs 64.
        write("functions/hid.usb0/report_length", "64")
        (function / "report_desc").write_bytes(REPORT_DESCRIPTOR)
        if not link.is_symlink():
            link.symlink_to(function)
        if bind:
            write("UDC", controllers[0].name)

    device = Path("/dev/hidg0")
    if not bind:
        # hidg's kernel cdev is created only on bind. A private node based on
        # configfs's reserved dev number avoids a privileged runtime/udev hook.
        runtime = Path("/dev/cynx")
        if runtime.is_symlink():
            raise RuntimeError("Refusing a symlink at /dev/cynx")
        runtime.mkdir(mode=0o755, exist_ok=True)
        if runtime.stat().st_uid != 0:
            raise RuntimeError("/dev/cynx must be root owned")
        runtime.chmod(0o755)
        device = runtime / "hidg0"
        major, minor = map(
            int, (GADGET / "functions/hid.usb0/dev").read_text().split(":")
        )
        dev = os.makedev(major, minor)
        if device.is_symlink():
            raise RuntimeError("Refusing a symlink HID node")
        if device.exists():
            info = device.stat()
            if not stat.S_ISCHR(info.st_mode):
                raise RuntimeError("Refusing a non-character HID node")
            if info.st_rdev != dev:
                device.unlink()
        if not device.exists():
            os.mknod(device, stat.S_IFCHR | 0o600, dev)
    deadline = time.monotonic() + 5
    while not device.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"USB gadget prepared, but {device} did not appear.")
        time.sleep(0.05)
    user = pwd.getpwnam(user_name or os.environ.get("SUDO_USER", "root"))
    os.chown(device, user.pw_uid, user.pw_gid)
    device.chmod(0o600)
    os.chown(GADGET / "UDC", user.pw_uid, user.pw_gid)
    (GADGET / "UDC").chmod(0o600)
    state = (controllers[0] / "state").read_text().strip()
    print(f"Configured {device} for {user.pw_name}; USB host state: {state}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--unbind", action="store_true")
    parser.add_argument("--user", default=None)
    args = parser.parse_args()
    if args.unbind:
        if os.geteuid() != 0:
            parser.error("--unbind requires root")
        if (GADGET / "UDC").exists() and (GADGET / "UDC").read_text().strip():
            (GADGET / "UDC").write_text("\n")
    else:
        setup(bind=not args.prepare, user_name=args.user)
