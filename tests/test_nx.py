"""Exercise wired reports and cleanup without requiring USB hardware."""

import errno

import pytest

from cynx.nx import Button, Controller
from cynx.nx import controller as module
from cynx.nx.controller import report


@pytest.fixture
def device(monkeypatch):
    sent = []
    closed = []
    monkeypatch.setattr(module.os, "open", lambda *args: 7)
    monkeypatch.setattr(module.os, "close", closed.append)
    monkeypatch.setattr(module.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(
        module.os, "write", lambda fd, data: sent.append(data) or len(data)
    )
    monkeypatch.setattr(module.time, "sleep", lambda duration: None)
    return sent, closed


def test_sequence_encodes_combined_buttons_and_releases(device):
    sent, closed = device
    with Controller() as controller:
        controller.press(Button.L | Button.R)
    assert sent == [
        b"\x00\x00\x08\x80\x80\x80\x80\x00",
        b"\x30\x00\x08\x80\x80\x80\x80\x00",
        b"\x00\x00\x08\x80\x80\x80\x80\x00",
        b"\x00\x00\x08\x80\x80\x80\x80\x00",
    ]
    assert closed == [7]


def test_interrupt_during_hold_releases_and_closes(device, monkeypatch):
    sent, closed = device

    def interrupt(duration):
        raise KeyboardInterrupt

    monkeypatch.setattr(module.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        with Controller() as controller:
            controller.press(Button.B)
    assert sent[1] == b"\x02\x00\x08\x80\x80\x80\x80\x00"
    assert sent[-2:] == [report(), report()]
    assert closed == [7]


def test_unavailable_usb_times_out_and_closes(device, monkeypatch):
    _, closed = device
    ticks = iter([0.0, 0.0, 0.2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))

    def disconnected(fd, data):
        raise OSError(errno.ESHUTDOWN, "USB disconnected")

    monkeypatch.setattr(module.os, "write", disconnected)
    with pytest.raises(TimeoutError, match="USB host is not accepting reports"):
        with Controller(timeout=0.1):
            pytest.fail("A disconnected host must not connect.")
    assert closed == [7]


def test_busy_device_is_closed_without_sending(device, monkeypatch):
    sent, closed = device

    def busy(*args):
        raise BlockingIOError("Controller is already in use.")

    monkeypatch.setattr(module.fcntl, "flock", busy)
    with pytest.raises(BlockingIOError):
        with Controller():
            pytest.fail("A second writer must not acquire the controller.")
    assert sent == []
    assert closed == [7]


@pytest.mark.parametrize("hold", [0, -1, 6, float("nan")])
def test_invalid_hold_sends_no_press(device, hold):
    sent, _ = device
    with Controller() as controller:
        with pytest.raises(ValueError, match="Hold duration"):
            controller.press(Button.A, hold=hold)
    assert sent == [report(), report()]


def test_main_runs_requested_sequence(device, monkeypatch, capsys):
    from cynx.__main__ import main

    monkeypatch.setattr("sys.argv", ["cynx", "--test"])
    main()
    assert device[0][1::2][:3] == [
        report(Button.L),
        report(Button.R),
        report(Button.B),
    ]
    assert "all buttons released" in capsys.readouterr().out


def test_main_reports_missing_gadget(monkeypatch, capsys):
    from cynx.__main__ import main

    monkeypatch.setattr("sys.argv", ["cynx", "--test"])

    def missing(*args):
        raise FileNotFoundError

    monkeypatch.setattr(module.os, "open", missing)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "sudo bin/usb_setup.sh" in capsys.readouterr().err
