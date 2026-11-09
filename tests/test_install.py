"""Check committed install artifacts without root or USB hardware."""

import getpass
import os
import subprocess
from pathlib import Path

from cynx.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads_to_service_defaults():
    assert Config.load(ROOT / "config.example.yaml") == Config()


def test_unit_renders_for_unprivileged_account_and_checkout():
    user = getpass.getuser()
    unit = subprocess.run(
        ["bash", str(ROOT / "bin/install_service.sh"), "--print-unit"],
        env={**os.environ, "CYNX_USER": user},
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "@" not in unit
    assert f"User={user}\n" in unit
    assert f"WorkingDirectory={ROOT}\n" in unit
    assert f"ExecStart={ROOT}/env/bin/cynx --config config.yaml\n" in unit
    assert f"service_usb.sh prepare {user}\n" in unit
    assert "ExecStopPost=+/usr/local/libexec/cynx/service_usb.sh unbind\n" in unit


def test_installer_rejects_unknown_service_account():
    result = subprocess.run(
        ["bash", str(ROOT / "bin/install_service.sh"), "--print-unit"],
        env={**os.environ, "CYNX_USER": "cynx-no-such-user"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Unknown service user" in result.stderr
