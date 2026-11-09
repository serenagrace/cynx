"""Run the exclusive LAN controller service (or an explicit wired test)."""

import argparse
import asyncio
import signal
from importlib.metadata import version

from cynx.config import Config, passphrase
from cynx.nx import Button, Controller
from cynx.observability import configure_logging
from cynx.server import Server


async def serve(config):
    configure_logging()
    server = Server(config, passphrase())
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await server.start()
        stopping = asyncio.create_task(stopped.wait())
        closing = asyncio.create_task(server.closed.wait())
        try:
            await asyncio.wait((stopping, closing), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopping, closing):
                task.cancel()
            await asyncio.gather(stopping, closing, return_exceptions=True)
    finally:
        await server.close()


def main() -> None:
    """Listen idle by default; only --test runs the old L/R/B demo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", action="version", version=f"cynx {version('cynx')}"
    )
    parser.add_argument("--device", default="/dev/hidg0")
    parser.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--test", action="store_true", help="Run the wired L/R/B demo")
    args = parser.parse_args()
    try:
        if not args.test:
            asyncio.run(serve(Config.load(args.config)))
            return
        print(f"Opening wired controller at {args.device}...", flush=True)
        with Controller(args.device, args.timeout) as controller:
            for button in (Button.L, Button.R, Button.B):
                print(f"Pressing {button.name}", flush=True)
                controller.press(button)
        print("Test sequence complete; all buttons released.")
    except KeyboardInterrupt:
        parser.exit(130, "\nController test interrupted.\n")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"cynx: {error}\n")


if __name__ == "__main__":
    main()
