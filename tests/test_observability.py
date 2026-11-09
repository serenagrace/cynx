"""Owner alerts stay bounded; local diagnostics retain safe traceback detail."""

import json
import logging

from cynx.observability import ErrorJournal
from cynx.protocol import MAX_FRAME
from cynx.server import Server


def test_error_journal_monotonic_bounded_and_redacted(caplog):
    secret = "private-π-passphrase"
    journal = ErrorJournal(secret)
    with caplog.at_level(logging.ERROR, logger="cynx"):
        for id in range(1, 76):
            journal.report(
                f"Failure {id} {secret} {json.dumps(secret)[1:-1]}\n" + "detail" * 100
            )
    records = journal.snapshot()
    assert len(records) == 50
    assert [record["id"] for record in records] == list(range(26, 76))
    assert all(record["time"] > 0 for record in records)
    assert all(len(json.dumps(record["message"]).encode()) <= 160 for record in records)
    assert secret not in caplog.text
    assert json.dumps(secret)[1:-1] not in caplog.text
    assert "[REDACTED]" in caplog.text
    records[-1]["message"] = "mutated copy"
    assert journal.snapshot()[-1]["message"] != "mutated copy"


def test_full_traceback_kept_locally_but_not_in_status(caplog):
    journal = ErrorJournal("do-not-disclose")
    try:
        raise RuntimeError("do-not-disclose " + "long traceback detail " * 100)
    except RuntimeError as error:
        with caplog.at_level(logging.ERROR, logger="cynx"):
            record = journal.report("USB execution failed", error)
    assert "Traceback" in caplog.text
    assert "test_full_traceback_kept_locally_but_not_in_status" in caplog.text
    assert len(caplog.text) > 160
    assert "do-not-disclose" not in caplog.text
    assert "Traceback" not in record["message"]
    assert len(json.dumps(record["message"]).encode()) <= 160


def test_full_error_history_status_stays_below_frame_limit(caplog):
    server = Server()
    # Suppress the intentionally enormous local diagnostics in pytest output.
    with caplog.at_level(logging.CRITICAL, logger="cynx"):
        for _ in range(75):
            server.errors.report("USB error: " + '"\\😃π' * 1000)
    status = server.status()
    assert len(status["errors"]) == 50
    assert status["last_error"] == status["errors"][-1]
    assert status["boot_id"] == server.boot_id
    encoded = json.dumps({"id": 1, "ok": True, "result": status}).encode() + b"\n"
    assert len(encoded) < MAX_FRAME
