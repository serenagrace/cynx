# Development

## Layout

| Path | Purpose |
| --- | --- |
| `cynx/__main__.py` | `cynx` entry point: serve by default, `--test` for the wired demo |
| `cynx/server.py` | Async listeners, ownership, step worker, heartbeat and recovery |
| `cynx/protocol.py` | Step parser, HID report encoding and JSON framing |
| `cynx/backend.py` | Non-blocking HID writes, UDC bind/unbind and fixed system actions |
| `cynx/config.py` | `config.yaml` validation and passphrase loading |
| `cynx/observability.py` | Error journal, redaction and rotating logs |
| `cynx/nx/` | Gadget setup (root) and the synchronous demo controller |
| `bin/` | Service installer, root USB hooks, formatter and version tools |
| `systemd/cynx.service.in` | Unit template rendered by the installer |
| `tests/` | Unit and loopback protocol tests |

## Set up

cynx supports Python 3.13 and newer. The Pi deployment uses 3.13.

```bash
python3 -m venv env
env/bin/python -m pip install -e ".[dev]"
```

## Test and format

```bash
bin/black.sh                         # isort, then Black
env/bin/python -m pytest
```

Tests run real TCP and UDP listeners on loopback ephemeral ports with a fake
USB backend and mocked system actions. They cover framing, authentication,
ownership, step parsing, acknowledgements, duplicate suppression, stop,
reconnects, heartbeat and grace handling, recovery timing, error reporting,
restart/reboot ordering and shutdown. They never touch USB, need no root, and
never reboot anything. One test runs the real-timing cleanup sequence and takes
about six seconds.

Hardware behavior (USB enumeration, the menu sequence on a real Switch, boot
autostart and power loss) needs a coordinated manual check on the Pi.

## Continuous integration

`.github/workflows/tests.yml` runs on every push and pull request on Ubuntu for
Python 3.13 and 3.14. It checks isort and Black, runs the test suite, builds
the sdist and wheel and checks their version and contents, runs ShellCheck and
`bash -n` on the scripts, and validates the rendered unit with
`systemd-analyze verify`. CI needs no secrets or hardware.

## Packaging

```bash
env/bin/python -m build
```

The wheel contains the `cynx` package and the `cynx` console script. The sdist
also includes the docs, tests, scripts, unit template and example
configuration. The service runs from a checkout, so install from git as
described in [installation](installation.md). The project is not published to
PyPI.

## Versioning

The version lives only in `pyproject.toml` and is reported by `cynx --version`
and `status.version`. Bump it with:

```bash
env/bin/python bin/bump_version.py patch     # or minor, major, X.Y.Z
env/bin/python bin/bump_version.py patch --dry-run
bin/install.sh                               # refresh installed metadata
```

## Conventions

- Keep the wire protocol in [protocol](protocol.md) in sync with the code.
- Never add commands that run arbitrary programs; system actions are fixed
  argument lists.
- Keep network handling non-blocking; HID writes are bounded and async.
- Never log or echo the passphrase.
