"""Exercise version changes without modifying the project manifest."""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    (tmp_path / "bin").mkdir()
    shutil.copy2(ROOT / "bin/bump_version.py", tmp_path / "bin/bump_version.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cynx"\nversion = "1.2.3"\ndependencies = []\n'
    )
    return tmp_path


def run_bump(project, *arguments):
    return subprocess.run(
        [sys.executable, str(project / "bin/bump_version.py"), *arguments],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("argument", "expected"),
    [("major", "2.0.0"), ("minor", "1.3.0"), ("patch", "1.2.4"), ("0.0.0", "0.0.0")],
)
def test_bump_updates_only_version(project, argument, expected):
    result = run_bump(project, argument)
    assert result.returncode == 0, result.stderr
    manifest = tomllib.loads((project / "pyproject.toml").read_text())
    assert manifest["project"] == {
        "name": "cynx",
        "version": expected,
        "dependencies": [],
    }


def test_dry_run_preserves_manifest(project):
    path = project / "pyproject.toml"
    before = path.read_bytes()
    result = run_bump(project, "patch", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.2.3 -> 1.2.4"
    assert path.read_bytes() == before


@pytest.mark.parametrize("argument", ["invalid", "1.2", "01.2.3"])
def test_invalid_bump_preserves_manifest(project, argument):
    path = project / "pyproject.toml"
    before = path.read_bytes()
    result = run_bump(project, argument)
    assert result.returncode == 2
    assert "Invalid version or bump" in result.stderr
    assert path.read_bytes() == before
