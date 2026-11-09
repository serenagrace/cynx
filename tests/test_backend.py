"""Verify cancellation-safe nonblocking USB writes and fixed system actions."""

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from cynx import backend as module
from cynx.backend import Backend, system_action
from cynx.protocol import NEUTRAL


def test_nonblocking_write_retries_yield_and_cancel(monkeypatch):
    async def run():
        backend = Backend()
        backend.fd = 7
        attempts = []

        def blocked(fd, data):
            attempts.append(data)
            raise OSError(errno.EAGAIN, "busy")

        monkeypatch.setattr(module.os, "write", blocked)
        task = asyncio.create_task(backend.send(NEUTRAL))
        await asyncio.sleep(0.025)
        assert len(attempts) >= 2
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert attempts == [NEUTRAL] * len(attempts)

    asyncio.run(run())


def test_detach_unbinds_and_closes_even_if_release_fails(monkeypatch):
    async def run():
        backend = Backend()
        backend.fd = 7
        closed, written = [], []
        monkeypatch.setattr(module.os, "close", closed.append)
        monkeypatch.setattr(Path, "exists", lambda path: True)
        monkeypatch.setattr(Path, "read_text", lambda path: "test.udc")
        monkeypatch.setattr(
            Path, "write_text", lambda path, text: written.append((path, text))
        )

        async def broken():
            raise TimeoutError("vanished")

        backend.release = broken
        await backend.detach()
        assert closed == [7]
        assert written == [(module.GADGET / "UDC", "\n")]
        assert backend.fd is None

    asyncio.run(run())


def test_status_does_not_claim_cable_when_unbound(monkeypatch):
    monkeypatch.setattr(Path, "glob", lambda path, pattern: [Path("/udc/test")])
    monkeypatch.setattr(Path, "exists", lambda path: True)
    monkeypatch.setattr(
        Path, "read_text", lambda path: "not attached" if path.name == "state" else ""
    )
    status = Backend().status()
    assert status["usb_detected"] and status["usb_controller_present"]
    assert not status["spoofing"] and not status["switch_connected"]


def test_detach_is_idempotent_when_configfs_is_already_unbound(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda path: True)
    monkeypatch.setattr(Path, "read_text", lambda path: "")

    def forbidden(*args):
        pytest.fail("Writing empty UDC when already unbound raises ENODEV on configfs")

    monkeypatch.setattr(Path, "write_text", forbidden)
    asyncio.run(Backend().detach())


def test_root_prepare_with_no_udc_leaves_server_start_possible(monkeypatch):
    from cynx.nx import gadget

    monkeypatch.setattr(gadget.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "glob", lambda *args: [])
    monkeypatch.setattr(Path, "exists", lambda path: False)
    gadget.setup(bind=False, user_name="serena")
    with pytest.raises(RuntimeError, match="Expected one USB"):
        gadget.setup(bind=True)


def test_connect_allows_initial_host_enumeration_beyond_normal_write_bound(monkeypatch):
    async def run():
        backend = Backend()
        binds, reports = [], []
        ticks = iter((0.0, 0.4))
        monkeypatch.setattr(
            module, "time", SimpleNamespace(monotonic=lambda: next(ticks))
        )
        monkeypatch.setattr(Path, "glob", lambda *args: [Path("/udc/test")])
        monkeypatch.setattr(Path, "write_text", lambda path, text: binds.append(text))
        monkeypatch.setattr(module.os, "open", lambda *args: 7)
        monkeypatch.setattr(module.fcntl, "flock", lambda *args: None)

        def write(fd, data):
            reports.append(data)
            if len(reports) == 1:
                raise OSError(errno.ESHUTDOWN, "host still enumerating")
            return len(data)

        monkeypatch.setattr(module.os, "write", write)
        await backend.connect()
        assert backend.fd == 7
        assert binds == ["test"]
        assert reports == [NEUTRAL, NEUTRAL]

    asyncio.run(run())


def test_normal_write_keeps_short_usb_disconnect_bound(monkeypatch):
    backend = Backend()
    backend.fd = 7
    ticks = iter((0.0, 0.4))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    def disconnected(fd, data):
        raise OSError(errno.ESHUTDOWN, "USB disconnected")

    monkeypatch.setattr(module.os, "write", disconnected)
    with pytest.raises(TimeoutError):
        asyncio.run(backend.send(NEUTRAL))


@pytest.mark.parametrize("action", ["restart", "reboot"])
def test_systemctl_argv_fixed_no_shell(monkeypatch, action):
    calls = []

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(system_action(action))
    expected = ("sudo", "-n", "/usr/bin/systemctl", "--no-block", action)
    if action == "restart":
        expected += ("cynx.service",)
    assert calls == [expected]
    with pytest.raises(KeyError):
        asyncio.run(system_action("shell"))
