"""Async, bounded HID writes. No blocking networking or press threads."""

import asyncio
import errno
import fcntl
import os
import time
from pathlib import Path

from cynx.nx.gadget import GADGET
from cynx.protocol import NEUTRAL


class Backend:
    def __init__(self, device="/dev/cynx/hidg0"):
        self.device = device
        self.fd = None

    def status(self):
        controllers = sorted(Path("/sys/class/udc").glob("*"))
        bound = (
            (GADGET / "UDC").read_text().strip() if (GADGET / "UDC").exists() else ""
        )
        state = "unavailable"
        if controllers:
            try:
                state = (controllers[0] / "state").read_text().strip()
            except OSError:
                state = "unknown"
        return {
            "usb_controller_present": bool(controllers),
            "usb_detected": bool(controllers) and Path(self.device).exists(),
            "usb_state": state,
            "spoofing": bool(bound),
            "switch_connected": bool(bound) and state == "configured",
        }

    async def connect(self):
        if self.fd is not None:
            return
        controllers = sorted(Path("/sys/class/udc").glob("*"))
        if len(controllers) != 1:
            raise RuntimeError("No unique USB device controller")
        try:
            (GADGET / "UDC").write_text(controllers[0].name)
            self.fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A fresh bind must complete host enumeration before HID writes;
            # subsequent reports retain the much shorter disconnect bound.
            await self.send(NEUTRAL, timeout=3.0)
        except BaseException:
            await self.detach()
            raise

    async def send(self, data, *, timeout=0.25):
        if self.fd is None:
            raise RuntimeError("Controller is not connected")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.write(self.fd, data) != len(data):
                    raise OSError("Incomplete HID report")
                return
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.ESHUTDOWN, errno.ENODEV):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("USB host is not accepting reports") from error
                await asyncio.sleep(0.01)

    async def release(self):
        if self.fd is not None:
            await self.send(NEUTRAL)

    async def detach(self):
        try:
            try:
                await self.release()
            except (OSError, RuntimeError):
                pass
        finally:
            try:
                if (GADGET / "UDC").exists() and (GADGET / "UDC").read_text().strip():
                    (GADGET / "UDC").write_text("\n")
            finally:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None


SYSTEM_ACTIONS = {
    "restart": (
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "--no-block",
        "restart",
        "cynx.service",
    ),
    "reboot": ("sudo", "-n", "/usr/bin/systemctl", "--no-block", "reboot"),
}


async def run_fixed(argv):
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            result = await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return result


async def authorize_system_action(action):
    """Ask sudo whether the fixed argv is allowed, without executing it."""
    argv = SYSTEM_ACTIONS[action]
    if await run_fixed((*argv[:2], "-l", *argv[2:])):
        raise RuntimeError("Fixed systemctl action is not permitted")


async def system_action(action):
    """Only these fixed argv are permitted; no shell or user-controlled argv."""
    if await run_fixed(SYSTEM_ACTIONS[action]):
        raise RuntimeError("Fixed systemctl action failed")
