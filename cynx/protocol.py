"""One-step Cynthia input parsing and bounded newline JSON framing."""

import asyncio
import json
import math
import re
import struct
from dataclasses import dataclass

MAX_FRAME = 16384
TOKENS = {
    name: 1 << bit
    for bit, name in enumerate(
        ("Y", "B", "A", "X", "L", "R", "ZL", "ZR", "-", "+", "LS", "RS", "H", "C")
    )
}
TOKENS["HOME"] = TOKENS["H"]
TOKEN = re.compile(r"HOME|ZL|ZR|LS|RS|[YBAXLRHC+\-^V<>]")
DIRECTIONS = {
    frozenset(): 8,
    frozenset("^"): 0,
    frozenset("^>"): 1,
    frozenset(">"): 2,
    frozenset("V>"): 3,
    frozenset("V"): 4,
    frozenset("V<"): 5,
    frozenset("<"): 6,
    frozenset("^<"): 7,
}
NEUTRAL = struct.pack("<H6B", 0, 8, 128, 128, 128, 128, 0)


class ProtocolError(ValueError):
    def __init__(self, code, message, result=None):
        super().__init__(message)
        self.code = code
        self.result = result


@dataclass(frozen=True)
class Step:
    report: bytes
    hold: float
    wait: float
    noblock: bool = False


def parse_step(text, max_hold=30.0, max_wait=300.0):
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("invalid_step", "Expected one serialized Input step")
    text = text.strip()
    noblock = False
    if re.match(r"noblock(?:\s|$)", text):
        noblock = True
        text = text[len("noblock") :].strip()
        if not text:
            raise ProtocolError("invalid_step", "noblock requires an Input step")
    elif re.search(r"\bnoblock$", text):
        noblock = True
        text = text[: -len("noblock")].strip()
        if not text:
            raise ProtocolError("invalid_step", "noblock requires an Input step")
    hold, wait = 0.15, 0.15
    mode = None
    if text.startswith("("):
        match = re.match(r"\(([^,()]+),([^,()]+)\)(?:\s+|$)", text)
        if not match:
            raise ProtocolError("invalid_step", "Expected (hold,wait) durations")
        try:
            hold, wait = map(float, match.groups())
        except ValueError as error:
            raise ProtocolError("invalid_step", "Invalid durations") from error
        text = text[match.end() :].strip()
    else:
        match = re.match(r"(tap|hold|wait)(?:\s+|$)", text)
        if match:
            mode = match[1]
            hold, wait = {"tap": (0.05, 0), "hold": (1, 0.15), "wait": (1, 1)}[mode]
            text = text[match.end() :].strip()
    if (
        not math.isfinite(hold)
        or not math.isfinite(wait)
        or not 0 <= hold <= max_hold
        or not 0 <= wait <= max_wait
    ):
        raise ProtocolError(
            "invalid_step", "Durations exceed finite nonnegative limits"
        )
    if mode == "wait" and text:
        raise ProtocolError("invalid_step", "wait takes no buttons")
    buttons, directions = 0, set()
    for rest in text.split():
        while rest:
            if rest.startswith(("SL", "SR")):
                raise ProtocolError(
                    "unsupported_control", "SL/SR are Joy-Con-only controls"
                )
            match = TOKEN.match(rest)
            if not match:
                raise ProtocolError(
                    "invalid_step", "Unknown control or multiple Input steps"
                )
            token = match[0]
            rest = rest[len(token) :]
            if token in "^V<>":
                directions.add(token)
            else:
                buttons |= TOKENS[token]
    if frozenset(directions) not in DIRECTIONS:
        raise ProtocolError("invalid_step", "Conflicting D-pad directions")
    data = struct.pack(
        "<H6B", buttons, DIRECTIONS[frozenset(directions)], 128, 128, 128, 128, 0
    )
    return Step(data, hold, wait, noblock)


async def read_frame(reader):
    try:
        raw = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as error:
        raise ProtocolError("frame_too_large", "Frame exceeds 16384 bytes") from error
    if not raw:
        raise EOFError
    if len(raw) > MAX_FRAME:
        raise ProtocolError("frame_too_large", "Frame exceeds 16384 bytes")
    if not raw.endswith(b"\n"):
        raise ProtocolError("invalid_frame", "Incomplete newline JSON frame")
    try:
        frame = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProtocolError("invalid_frame", "Expected UTF-8 JSON") from error
    if not isinstance(frame, dict) or type(frame.get("id")) is not int:
        raise ProtocolError("invalid_frame", "Expected object with integer id")
    return frame


async def send_frame(writer, frame):
    writer.write(
        (json.dumps(frame, separators=(",", ":"), allow_nan=False) + "\n").encode()
    )
    await asyncio.wait_for(writer.drain(), 1)
