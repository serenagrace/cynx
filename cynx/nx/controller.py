"""Send HORIPAD-compatible reports through a Linux USB HID gadget."""

import errno
import fcntl
import math
import os
import select
import struct
import time
from enum import IntFlag
from pathlib import Path


class Button(IntFlag):
    """Button bits in a wired HORIPAD input report."""

    Y = 1 << 0
    B = 1 << 1
    A = 1 << 2
    X = 1 << 3
    L = 1 << 4
    R = 1 << 5
    ZL = 1 << 6
    ZR = 1 << 7
    MINUS = 1 << 8
    PLUS = 1 << 9
    L_STICK = 1 << 10
    R_STICK = 1 << 11
    HOME = 1 << 12
    CAPTURE = 1 << 13


def report(buttons: Button = Button(0)) -> bytes:
    """Encode buttons with a neutral D-pad and centered analog sticks."""
    return struct.pack("<H6B", int(buttons), 8, 128, 128, 128, 128, 0)


class Controller:
    """Own one HID device and perform bounded, locally timed button presses."""

    def __init__(self, device: str = "/dev/hidg0", timeout: float = 10.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Connection timeout must be a positive finite number.")
        self.device = Path(device)
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
        except FileNotFoundError as error:
            raise RuntimeError(
                f"{self.device} is missing; run sudo bin/usb_setup.sh first."
            ) from error
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.release()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.release()
        except (OSError, TimeoutError):
            if exc_type is None:
                raise
        finally:
            os.close(self._fd)
            self._fd = None

    def _send(self, data: bytes) -> None:
        if self._fd is None:
            raise RuntimeError("The controller is not open.")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                written = os.write(self._fd, data)
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.ESHUTDOWN, errno.ENODEV):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "USB host is not accepting reports. Check the dock, "
                        "data cable, power, and whether the Switch is awake."
                    ) from error
                if error.errno == errno.EAGAIN:
                    select.select([], [self._fd], [], min(remaining, 0.05))
                else:
                    time.sleep(min(remaining, 0.05))
                continue
            if written != len(data):
                raise OSError("Incomplete USB HID report.")
            return

    def release(self) -> None:
        """Send a neutral report to release every control."""
        self._send(report())

    def press(self, buttons: Button, hold: float = 0.15, gap: float = 0.5) -> None:
        """Press for at most five seconds, then release even if interrupted."""
        if not math.isfinite(hold) or not 0 < hold <= 5:
            raise ValueError("Hold duration must be greater than zero and at most 5s.")
        if not math.isfinite(gap) or gap < 0:
            raise ValueError("Gap must be a nonnegative finite number.")
        try:
            self._send(report(buttons))
            time.sleep(hold)
        finally:
            self.release()
        time.sleep(gap)
