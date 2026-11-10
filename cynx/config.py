"""Small, validated service configuration."""

import math
import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml
from dotenv import dotenv_values


@dataclass
class Config:
    """Service settings from config.yaml; unknown keys are rejected."""

    listen_host: str = "0.0.0.0"
    command_port: int = 3140
    macro_port: int = 3141
    max_hold: float = 30.0
    max_wait: float = 300.0
    reconnect_grace: float = 5.0
    heartbeat_timeout: float = 3.0
    recovery_timeout: float = 10.0
    device: str = "/dev/cynx/hidg0"

    def __post_init__(self):
        for name in ("command_port", "macro_port"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 65535:
                raise ValueError(f"{name} must be a valid port")
        if self.command_port and self.command_port == self.macro_port:
            raise ValueError("TCP ports must differ")
        for name in (
            "max_hold",
            "max_wait",
            "reconnect_grace",
            "heartbeat_timeout",
            "recovery_timeout",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        if self.recovery_timeout > 10:
            raise ValueError("recovery_timeout cannot exceed 10 seconds")
        if not isinstance(self.listen_host, str) or not isinstance(self.device, str):
            raise ValueError("listen_host and device must be strings")

    @classmethod
    def load(cls, path):
        raw = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError("config must be a mapping")
        unknown = raw.keys() - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        return cls(**raw)


def passphrase(path=".env"):
    """Read CYNX_PASSPHRASE from the environment or the local dotenv.

    Interpolation is disabled so a secret is never expanded or logged.
    """
    return os.environ.get(
        "CYNX_PASSPHRASE",
        dotenv_values(path, interpolate=False).get("CYNX_PASSPHRASE") or "",
    )
