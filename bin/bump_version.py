#!/usr/bin/env python3
"""Bump the package version.

Usage: bump_version.py {major|minor|patch|X.Y.Z} [--dry-run]
"""

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def bump(current: str, argument: str) -> str:
    """Return a semantic version for the requested bump."""
    if SEMVER.fullmatch(argument):
        return argument
    major, minor, patch = map(int, current.split("."))
    if argument == "major":
        return f"{major + 1}.0.0"
    if argument == "minor":
        return f"{major}.{minor + 1}.0"
    if argument == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Invalid version or bump: {argument}")


def main() -> None:
    """Update pyproject.toml unless a dry run is requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="major, minor, patch, or X.Y.Z")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = ROOT / "pyproject.toml"
    text = path.read_text()
    match = VERSION.search(text)
    if match is None or not SEMVER.fullmatch(match.group(2)):
        parser.error("No valid X.Y.Z version found in pyproject.toml")
    current = match.group(2)
    try:
        new = bump(current, args.version)
    except ValueError as error:
        parser.error(str(error))
    print(f"{current} -> {new}")
    if not args.dry_run:
        path.write_text(VERSION.sub(rf"\g<1>{new}\g<3>", text, count=1))


if __name__ == "__main__":
    main()
