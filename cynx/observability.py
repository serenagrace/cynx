"""Bounded owner-alert journal and local rotating service logs."""

import json
import logging
import sys
import time
import traceback
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER = logging.getLogger("cynx")


class ErrorJournal:
    def __init__(self, secret=""):
        self.secret = secret
        self.records = deque(maxlen=50)
        self.highest = 0

    def redact(self, message, *, multiline=False):
        text = str(message)
        if self.secret:
            for value in (
                self.secret,
                json.dumps(self.secret)[1:-1],
                repr(self.secret)[1:-1],
            ):
                text = text.replace(value, "[REDACTED]")
        text = "".join(
            (
                character
                if character.isprintable() or multiline and character in "\n\t"
                else " "
            )
            for character in text
        )
        return text if multiline else " ".join(text.split())

    def sanitize(self, message):
        text = self.redact(message)
        # Fifty records plus status must fit the 16 KiB wire frame, including
        # JSON escaping of arbitrary Unicode and control characters.
        bounded = text[:160]
        while (
            len(json.dumps(bounded + ("..." if bounded != text else "")).encode()) > 160
        ):
            bounded = bounded[:-1]
        return bounded + ("..." if bounded != text else "")

    def report(self, message, error=None):
        if error is not None:
            message = f"{message}: {type(error).__name__}: {error}"
        self.highest += 1
        record = {
            "id": self.highest,
            "time": time.time(),
            "message": self.sanitize(message),
        }
        self.records.append(record)
        detail = self.redact(message, multiline=True)
        if error is not None:
            detail += "\n" + self.redact(
                "".join(traceback.format_exception(error)), multiline=True
            )
        LOGGER.error("Error %s: %s", record["id"], detail)
        return dict(record)

    def snapshot(self):
        return [dict(record) for record in self.records]


def configure_logging():
    """The service working directory owns logs; stdout is collected by journald."""
    directory = Path("logs")
    directory.mkdir(mode=0o700, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in tuple(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    for handler in (
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            directory / "cynx.log",
            maxBytes=1_048_576,
            backupCount=3,
            encoding="utf-8",
        ),
    ):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
