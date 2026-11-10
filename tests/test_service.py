"""Exercise the actual TCP/UDP protocol against an injected USB backend."""

import asyncio
import contextlib
import json
import time
from dataclasses import replace

import pytest

from cynx.config import Config
from cynx.protocol import NEUTRAL, ProtocolError, parse_step, read_frame, send_frame
from cynx.server import RECOVERY_STEPS, Server


class FakeBackend:
    def __init__(self):
        self.bound = False
        self.events = []

    def status(self):
        return {
            "usb_controller_present": True,
            "usb_detected": True,
            "usb_state": "configured" if self.bound else "not attached",
            "spoofing": self.bound,
            "switch_connected": self.bound,
        }

    async def connect(self):
        self.bound = True
        self.events.append("connect")
        await self.release()

    async def send(self, data):
        if not self.bound:
            raise RuntimeError("Not bound")
        self.events.append(data)

    async def release(self):
        self.events.append(NEUTRAL)

    async def detach(self):
        await self.release()
        self.bound = False
        self.events.append("detach")


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.session = None
        self.id = 0

    async def send(self, frame):
        await send_frame(self.writer, frame)

    async def receive(self, timeout=5):
        return await asyncio.wait_for(read_frame(self.reader), timeout)

    async def call(self, command, **values):
        self.id += 1
        await self.send({"id": self.id, "command": command, **values})
        return await self.receive()

    async def step(self, id, text):
        await self.send({"id": id, "session": self.session, "step": text})
        return await self.receive()

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


@contextlib.asynccontextmanager
async def running(secret="", **options):
    backend = FakeBackend()
    actions = []

    async def action(command):
        assert not backend.bound
        assert backend.events[-1] == "detach"
        actions.append(command)

    config = replace(
        Config(
            listen_host="127.0.0.1",
            command_port=0,
            macro_port=0,
            heartbeat_timeout=20,
            reconnect_grace=0.15,
        ),
        **options,
    )
    server = Server(config, secret, backend, action)
    await server.start()
    clients = []

    async def open_client(port=None, ip="127.0.0.1"):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port or server.command_port, local_addr=(ip, 0)
        )
        client = Client(reader, writer)
        clients.append(client)
        return client

    async def login(session=None, password=secret, ip="127.0.0.1"):
        command = await open_client(ip=ip)
        response = await command.call(
            "hello", protocol=1, session=session, passphrase=password
        )
        if response["ok"]:
            command.session = response["result"]["session"]
        return command, response

    async def attach(session, password=secret, ip="127.0.0.1"):
        macro = await open_client(server.macro_port, ip)
        macro.session = session
        await macro.send(
            {"id": 0, "command": "attach", "session": session, "passphrase": password}
        )
        return macro, await macro.receive()

    server.open_client, server.login, server.attach = open_client, login, attach
    server.actions = actions
    try:
        yield server, backend
    finally:
        await server.close()
        for client in clients:
            with contextlib.suppress(ConnectionError):
                await client.close()


async def owned(server):
    command, response = await server.login()
    assert response["ok"]
    macro, response = await server.attach(command.session)
    assert response["ok"]
    return command, macro


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def test_parser_reports_and_modes():
    assert parse_step("L R").report[:3] == b"\x30\x00\x08"
    assert parse_step("ZLZRLSRSHOME C + - ^ >").report == bytes.fromhex(
        "c03f018080808000"
    )
    assert parse_step("(0.1,0.2) H").hold == 0.1
    assert parse_step("tap A").wait == 0
    assert parse_step("hold C").hold == 1
    assert (parse_step("wait").hold, parse_step("wait").wait) == (1, 1)
    assert parse_step("(1.0,1.0)").report == NEUTRAL
    assert parse_step("(0,0) A noblock").noblock
    assert parse_step("AB").report[:2] == b"\x06\x00"


@pytest.mark.parametrize(
    "text",
    [
        "SL",
        "SR",
        "A SL",
        "(nan,0) A",
        "(inf,0) A",
        "(-1,0)",
        "(31,0) A",
        "(0,301)",
        "(0,-1)",
        "tap A\nhold B",
        "wait A",
        "^ V",
        "< >",
        "named_macro",
        "if x then A",
        "",
        "A;B",
    ],
)
def test_parser_rejects_non_steps(text):
    with pytest.raises(ProtocolError):
        parse_step(text)


def test_parser_respects_configured_limits():
    assert parse_step("(31,301)", 32, 302).wait == 301
    with pytest.raises(ProtocolError):
        parse_step("(0,2)", max_wait=1)


def test_startup_auth_and_exclusive_channels():
    async def run():
        async with running("matching") as (server, backend):
            assert not backend.bound
            assert server.status()["connection_state"] == "idle"
            command, bad = await server.login(password="wrong")
            assert bad["code"] == "unauthorized" and bad["id"] == 1
            assert server.owner is None
            command, macro = await owned(server)
            _, bad = await server.login()
            assert bad["code"] == "busy"
            _, bad = await server.login(session=command.session, ip="127.0.0.2")
            assert bad["code"] == "busy"
            _, bad = await server.attach(command.session, password="wrong")
            assert bad["code"] == "unauthorized"
            _, bad = await server.attach(command.session, ip="127.0.0.2")
            assert bad["code"] == "busy"
            result = await command.call("connect")
            assert result["result"]["spoofing"]
            state = (await command.call("status"))["result"]
            assert state["identity"] == "cynx" and state["protocol"] == 1
            assert state["connection_state"] == "connected"
            assert state["boot_id"] == server.boot_id
            assert state["last_completed_id"] == 0
            assert state["switch_connected"]
            assert state["command_port"] == server.command_port
            assert (await command.call("sh", value="reboot"))[
                "code"
            ] == "unknown_command"

    asyncio.run(run())


def test_blank_auth_and_attach_requires_command_owner():
    async def run():
        async with running() as (server, _):
            _, bad = await server.attach("missing")
            assert bad["code"] == "invalid_session"
            command, result = await server.login(password="ignored_when_disabled")
            assert result["ok"]
            macro, result = await server.attach(command.session)
            assert result["ok"]
            result = await macro.step(1, "A")
            assert result["code"] == "not_connected"

    asyncio.run(run())


def test_frames_split_incomplete_oversized_and_utf8():
    async def run():
        async with running() as (server, _):
            client = await server.open_client()
            client.writer.write(b'{"id":91,"command":"hello",')
            await client.writer.drain()
            await asyncio.sleep(0.02)
            assert server.owner is None
            client.writer.write(b'"protocol":1,"session":null,"passphrase":""}\n')
            assert (await client.receive())["id"] == 91
            for data, code in (
                (b'{"id":4', "invalid_frame"),
                (b"x" * 16385 + b"\n", "frame_too_large"),
                (b"\xff\n", "invalid_frame"),
                (b"[]\n", "invalid_frame"),
            ):
                other = await server.open_client()
                other.writer.write(data)
                await other.writer.drain()
                if not data.endswith(b"\n"):
                    other.writer.write_eof()
                assert (await other.receive())["code"] == code
            # The limit includes the newline; oversized valid JSON is also rejected.
            other = await server.open_client()
            other.writer.write(
                json.dumps({"id": 5, "pad": "x" * 16384}).encode() + b"\n"
            )
            assert (await other.receive())["code"] == "frame_too_large"

    asyncio.run(run())


def test_prompt_accept_completion_after_gap_duplicate_no_replay_and_id_conflict():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            before = len(backend.events)
            start = time.monotonic()
            accepted = await macro.step(1, "(0.08,0.08) A noblock")
            assert accepted["event"] == "accepted"
            assert time.monotonic() - start < 0.07
            assert (await command.call("status"))["result"]["active_id"] == 1
            completed = await macro.receive()
            assert completed["event"] == "completed"
            assert time.monotonic() - start >= 0.15
            assert backend.events[before:] == [parse_step("A").report, NEUTRAL]
            count = len(backend.events)
            assert (await macro.step(1, "(0.08,0.08) A noblock"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"
            assert len(backend.events) == count
            assert (await macro.step(1, "B"))["code"] == "id_conflict"
            assert (await macro.step(0, "A"))["code"] == "invalid_id"
            assert (await macro.step(3, "(0,0)"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"
            assert (await macro.step(2, "A"))["code"] == "stale_id"

    asyncio.run(run())


def test_in_progress_duplicate_attaches_waiter_without_replaying():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            text = "(0.1,0.02) B"
            assert (await macro.step(1, text))["event"] == "accepted"
            assert (await macro.step(1, text))["event"] == "accepted"
            assert [(await macro.receive())["event"] for _ in range(2)] == [
                "completed"
            ] * 2
            assert backend.events.count(parse_step("B").report) == 1

    asyncio.run(run())


def test_stop_interrupts_active_clears_queue_and_remains_bound():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            assert (await macro.step(1, "(5,0) A"))["event"] == "accepted"
            await eventually(lambda: server.active is not None)
            assert (await macro.step(2, "B"))["event"] == "accepted"
            assert (await macro.step(3, "C"))["code"] == "queue_full"
            start = time.monotonic()
            result = await command.call("stop")
            assert result["ok"] and time.monotonic() - start < 0.1
            failures = [await macro.receive(), await macro.receive()]
            assert {item["id"] for item in failures} == {1, 2}
            assert all(item["code"] == "stopped" for item in failures)
            assert server.queue.empty() and server.active is None
            assert backend.bound and backend.events[-1] == NEUTRAL
            assert parse_step("B").report not in backend.events
            assert (await macro.step(3, "(0,0) C"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"

    asyncio.run(run())


def test_disconnect_exact_recovery_and_neutral_detach():
    async def run():
        async with running() as (server, backend):
            command, _ = await owned(server)
            await command.call("connect")
            before = len(backend.events)
            # Real NXBT-equivalent cleanup timing is about 5.85 seconds.
            await command.send({"id": 99, "command": "disconnect"})
            response = await command.receive(timeout=9)
            assert response["ok"]
            assert not backend.bound and server.owner is not None
            reports = [
                item
                for item in backend.events[before:]
                if isinstance(item, bytes) and item != NEUTRAL
            ]
            assert reports == [
                parse_step(text).report for text in ("H", "V", *(">",) * 6, "A", "A")
            ]
            assert backend.events[-2:] == [NEUTRAL, "detach"]
            state = response["result"]
            assert state["session"] == command.session
            assert state["connection_state"] == "connected"
            assert not state["spoofing"] and not state["recovering"]

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["macro", "command"])
def test_reconnect_finishes_active_preserves_ledger_and_replaces_safely(channel):
    async def run():
        async with running(reconnect_grace=0.4) as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            text = "(0.04,0.04) A"
            await macro.step(1, text)
            await (macro if channel == "macro" else command).close()
            await eventually(lambda: not server.channels_ready())
            await asyncio.sleep(0.1)
            assert server.owner.last_completed == 1
            command2, hello = await server.login(session=command.session)
            assert hello["result"]["last_completed_id"] == 1
            macro2, attached = await server.attach(command.session)
            assert attached["ok"]
            await asyncio.sleep(0.02)
            assert (
                server.owner.command.get_extra_info("peername")[1]
                == command2.writer.get_extra_info("sockname")[1]
            )
            assert (
                server.owner.macro.get_extra_info("peername")[1]
                == macro2.writer.get_extra_info("sockname")[1]
            )
            assert (await macro2.step(1, text))["event"] == "accepted"
            assert (await macro2.receive())["event"] == "completed"
            assert backend.events.count(parse_step("A").report) == 1
            assert (await command2.call("status"))["result"][
                "connection_state"
            ] == "connected"

    asyncio.run(run())


def test_loss_pauses_buffered_step_at_boundary_until_reconnect():
    async def run():
        async with running(reconnect_grace=0.5) as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            await macro.step(1, "(0.05,0.02) A")
            await eventually(lambda: server.active is not None)
            await macro.step(2, "(0,0) B")
            await macro.close()
            await eventually(lambda: not server.channels_ready())
            await asyncio.sleep(0.1)
            assert server.owner.last_completed == 1 and server.queue.qsize() == 1
            assert parse_step("B").report not in backend.events
            macro2, _ = await server.attach(command.session)
            await eventually(lambda: server.owner.last_completed == 2)
            assert (await macro2.step(2, "(0,0) B"))["event"] == "accepted"
            assert (await macro2.receive())["event"] == "completed"

    asyncio.run(run())


def test_grace_timeout_cancels_active_releases_recovers_and_tombstones():
    async def run():
        async with running(reconnect_grace=0.05, recovery_timeout=0.1) as (
            server,
            backend,
        ):
            command, macro = await owned(server)
            await command.call("connect")
            await macro.step(1, "(0,0) B")
            await macro.receive()
            await macro.step(2, "(5,0) A")
            record = server.owner.records[2]
            await macro.close()
            _, busy = await server.login()
            assert busy["code"] == "busy"
            await eventually(lambda: server.recovering)
            # Recovery interrupts the active step inside its own task.
            result = await asyncio.wait_for(asyncio.shield(record.done), 1)
            assert result["code"] == "interrupted"
            _, busy = await server.login()
            assert busy["code"] == "busy"
            await eventually(lambda: server.owner is None)
            assert not backend.bound and server.queue.empty()
            assert parse_step("H").report in backend.events
            _, expired = await server.login(session=command.session)
            assert expired["code"] == "session_expired"
            assert expired["result"] == {
                "last_completed_id": 1,
                "recovery_complete": False,
            }
            _, fresh = await server.login()
            assert fresh["ok"]

    asyncio.run(run())


def test_heartbeat_timeout_even_with_macro_traffic_and_status_refresh():
    async def run():
        async with running(
            heartbeat_timeout=0.12, reconnect_grace=0.05, recovery_timeout=0.08
        ) as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            for _ in range(3):
                await asyncio.sleep(0.06)
                assert (await command.call("status"))["ok"]
            await macro.step(1, "(0,0) A")
            await macro.receive()
            await eventually(lambda: server.owner is None)
            assert not backend.bound

    asyncio.run(run())


def test_shutdown_cancels_active_neutralizes_and_unbinds():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            await macro.step(1, "(5,0) A")
            record = server.owner.records[1]
            await server.close()
            assert server.closed.is_set()
            assert record.done.result()["code"] == "interrupted"
            assert not backend.bound and backend.events[-2:] == [NEUTRAL, "detach"]

    asyncio.run(run())


@pytest.mark.parametrize("command", ["restart", "reboot"])
def test_fixed_system_actions_cleanup_before_dispatch(command):
    async def run():
        async with running() as (server, backend):
            client, macro = await owned(server)
            await client.call("connect")
            await macro.step(1, "(5,0) A")
            dispatched = asyncio.Event()
            original = server.action

            async def action(name):
                # The acknowledgement must already be readable when the
                # runner starts; a real systemd stop follows immediately.
                assert not dispatched.is_set()
                dispatched.set()
                await original(name)

            server.action = action
            await client.send({"id": 77, "command": command})
            reply = await client.receive()
            assert reply["id"] == 77 and reply["ok"]
            assert not reply["result"]["spoofing"]
            await asyncio.wait_for(dispatched.wait(), 1)
            assert server.actions == [command]
            await asyncio.wait_for(server.closed.wait(), 1)
            assert not backend.bound

    asyncio.run(run())


def test_discovery_unicast_offer_does_not_claim_or_spoof():
    class Receiver(asyncio.DatagramProtocol):
        def __init__(self, future):
            self.future = future

        def datagram_received(self, data, addr):
            if not self.future.done():
                self.future.set_result(json.loads(data))

    async def run():
        async with running() as (server, backend):
            future = asyncio.get_running_loop().create_future()
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Receiver(future), local_addr=("127.0.0.1", 0)
            )
            try:
                transport.sendto(
                    b'{"type":"cynx_discover","protocol":1,"nonce":"abc"}',
                    ("127.0.0.1", server.command_port),
                )
                offer = await asyncio.wait_for(future, 1)
                assert offer == {
                    "type": "cynx_offer",
                    "identity": "cynx",
                    "protocol": 1,
                    "nonce": "abc",
                    "command_port": server.command_port,
                    "macro_port": server.macro_port,
                }
                assert server.owner is None and not backend.bound
            finally:
                transport.close()

    asyncio.run(run())


def test_ledger_bounded_and_evicted_id_cannot_replay():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            for id in range(1, 260):
                assert (await macro.step(id, "(0,0) A"))["event"] == "accepted"
                assert (await macro.receive())["event"] == "completed"
            assert len(server.owner.records) == 256
            assert (await macro.step(1, "(0,0) A"))["code"] == "stale_id"
            assert server.owner.last_completed == 259

    asyncio.run(run())


def test_unicode_auth_and_malformed_commands_are_bounded_errors():
    async def run():
        async with running("π secret") as (server, _):
            command, macro = await owned(server)
            assert (await command.call(["bad"]))["code"] == "unknown_command"
            assert (await command.call("status"))["ok"]
            _, wrong = await server.login(password="π wrong")
            assert wrong["code"] == "unauthorized"
            assert "π wrong" not in str(wrong)

    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True])
def test_heartbeat_does_not_self_expire_during_slow_connect(fails):
    async def run():
        async with running(heartbeat_timeout=0.08) as (server, backend):
            command, macro = await owned(server)
            original = backend.connect

            async def slow():
                await asyncio.sleep(0.15)
                if fails:
                    raise TimeoutError("USB host is not accepting reports")
                await original()

            backend.connect = slow
            response = await command.call("connect")
            assert response["ok"] is not fails
            # The client's next heartbeat follows the reply; the slow bind
            # itself must not count against liveness.
            await asyncio.sleep(0.06)
            assert server.owner is not None and server.channels_ready()
            assert (await command.call("status"))["ok"]

    asyncio.run(run())


def test_status_and_stop_responsive_during_recovery():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            await command.send({"id": 20, "command": "disconnect"})
            await eventually(lambda: server.recovering)
            await command.send({"id": 21, "command": "status"})
            status = await command.receive()
            assert status["id"] == 21 and status["result"]["recovering"]
            start = time.monotonic()
            await command.send({"id": 22, "command": "stop"})
            responses = [await command.receive(), await command.receive()]
            assert {item["id"] for item in responses} == {20, 22}
            assert all(item["ok"] for item in responses)
            assert time.monotonic() - start < 0.1
            assert not backend.bound and server.owner is not None

    asyncio.run(run())


def test_usb_step_failure_is_terminal_and_duplicate_does_not_replay():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            attempts = []

            async def unplugged(data):
                attempts.append(data)
                raise OSError("USB disconnected")

            backend.send = unplugged
            assert (await macro.step(1, "(0,0) A"))["event"] == "accepted"
            failed = await macro.receive()
            assert failed["event"] == "failed"
            assert failed["code"] == "usb_unavailable"
            assert server.owner.last_completed == 0
            assert (await macro.step(1, "(0,0) A"))["event"] == "accepted"
            assert (await macro.receive())["code"] == "usb_unavailable"
            assert attempts == [parse_step("A").report]
            assert (await command.call("status"))["ok"]

    asyncio.run(run())


def test_connect_usb_unavailable_rejection_preserves_command_session():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            original = backend.connect

            async def unavailable():
                raise RuntimeError("USB controller unavailable")

            backend.connect = unavailable
            rejected = await command.call("connect")
            assert not rejected["ok"] and rejected["code"] == "usb_unavailable"
            assert (await command.call("status"))["result"][
                "session"
            ] == command.session
            backend.connect = original
            assert (await command.call("connect"))["ok"]

    asyncio.run(run())


def test_owner_status_reports_hardware_errors_but_not_unauthenticated_probes():
    async def run():
        async with running("private") as (server, backend):
            _, rejected = await server.login(password="invalid-probe-secret")
            assert rejected["code"] == "unauthorized"
            assert server.errors.snapshot() == []
            command, macro = await owned(server)
            assert (await command.call("unknown-command"))["code"] == "unknown_command"
            assert server.errors.snapshot() == []

            async def missing():
                raise OSError("USB device private not available")

            backend.connect = missing
            assert (await command.call("connect"))["code"] == "usb_unavailable"
            state = (await command.call("status"))["result"]
            assert len(state["errors"]) == 1
            assert state["errors"][0]["id"] == 1
            assert "connect command failed" in state["errors"][0]["message"]
            assert "private" not in state["errors"][0]["message"]
            assert state["last_error"] == state["errors"][0]
            assert state["boot_id"] == server.boot_id

    asyncio.run(run())


@pytest.mark.parametrize("command", ["restart", "reboot"])
def test_system_action_failures_are_reported_to_owner(command):
    async def run():
        async with running() as (server, backend):
            client, macro = await owned(server)

            async def broken(action):
                raise RuntimeError("systemctl action denied")

            server.action = broken
            # Dispatch happens after the acknowledgement; failure is journaled.
            assert (await client.call(command))["ok"]
            await eventually(lambda: not server.maintenance)
            state = (await client.call("status"))["result"]
            assert f"{command} command failed" in state["last_error"]["message"]
            assert not server.maintenance

    asyncio.run(run())


def test_grace_recovery_error_survives_for_next_authenticated_owner():
    async def run():
        async with running(reconnect_grace=0.03) as (server, backend):
            command, macro = await owned(server)
            await command.close()
            await macro.close()
            await eventually(lambda: server.owner is None)
            _, hello = await server.login()
            assert any(
                "Reconnect grace expired" in record["message"]
                for record in hello["result"]["errors"]
            )

    asyncio.run(run())


@pytest.mark.parametrize("task_name", ["work", "heartbeat"])
def test_unexpected_critical_task_failure_is_logged_and_detaches(task_name):
    async def run():
        async with running() as (server, backend):
            current = server.worker if task_name == "work" else server.watchdog
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)

            async def crash():
                raise ValueError(f"{task_name} unexpected failure")

            task = server.spawn(crash())
            if task_name == "work":
                server.worker = task
            else:
                server.watchdog = task
            await asyncio.wait_for(server.closed.wait(), 1)
            assert not backend.bound
            assert any(
                "Background crash task failed" in record["message"]
                for record in server.errors.snapshot()
            )

    asyncio.run(run())


def test_recovery_matches_nxbt_cleanup_timing_and_order():
    """Cynthia cleanup: H, wait, V, > x6, hold A, noblock A."""

    async def run():
        async with running() as (server, backend):
            command, _ = await owned(server)
            await command.call("connect")
            performed = []
            original = server.perform

            async def record(step):
                performed.append(step)
                await original(parse_step("(0,0)"))

            server.perform = record
            assert (await command.call("disconnect"))["ok"]
            neutral = parse_step("wait").report
            assert [(step.report, step.hold, step.wait) for step in performed] == [
                (parse_step("H").report, 0.15, 0.15),
                (neutral, 1, 1),
                (parse_step("V").report, 0.15, 0.15),
                *[(parse_step(">").report, 0.15, 0.15)] * 6,
                (parse_step("A").report, 1, 0.15),
                (parse_step("A").report, 0.15, 0.15),
            ]
            assert performed[-1].noblock
            # Two seconds of neutral separate HOME from DOWN.
            assert performed[0].wait + performed[1].hold + performed[1].wait == 2.15
            total = sum(step.hold + step.wait for step in performed)
            assert total == pytest.approx(5.85)
            budget = server.config.recovery_timeout - 0.5
            assert total < budget
            assert not backend.bound

    asyncio.run(run())


def test_noblock_prefix_matches_cynthia_serialization():
    assert parse_step("noblock A").noblock
    assert parse_step("noblock A").report == parse_step("A").report
    with pytest.raises(ProtocolError):
        parse_step("noblock")


def test_connection_loss_after_grace_uses_same_cleanup_as_disconnect():
    async def run():
        async with running(reconnect_grace=0.2) as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")
            performed = []
            original = server.perform

            async def record(step):
                performed.append((time.monotonic(), step))
                await original(parse_step("(0,0)"))

            server.perform = record
            # Accepted work may finish during grace; no recovery starts before it.
            assert (await macro.step(1, "(0,0) A"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"
            lost = time.monotonic()
            await command.close()
            await macro.close()
            await eventually(lambda: server.owner is None)
            recovery = [step for _, step in performed[1:]]
            assert performed[1][0] - lost >= 0.18
            assert [
                (step.report, step.hold, step.wait, step.noblock) for step in recovery
            ] == [
                (
                    parse_step(text).report,
                    parse_step(text).hold,
                    parse_step(text).wait,
                    parse_step(text).noblock,
                )
                for text in RECOVERY_STEPS
            ]
            assert not backend.bound

    asyncio.run(run())


def test_explicit_disconnect_keeps_network_lease_for_reconnecting_usb():
    async def run():
        async with running(heartbeat_timeout=0.5) as (server, backend):
            command, macro = await owned(server)
            assert (await command.call("connect"))["ok"]
            assert (await macro.step(1, "(0,0) A"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"

            async def short_menu_step(step):
                await server.backend.send(step.report)
                await asyncio.sleep(0.07)
                await server.backend.release()

            # Ten cleanup steps outlast the heartbeat timeout without a status call.
            server.perform = short_menu_step
            await command.send({"id": 50, "command": "disconnect"})
            await eventually(lambda: server.recovering)
            assert (await command.receive())["ok"]
            del server.perform
            state = (await command.call("status"))["result"]
            assert state["session"] == command.session
            assert state["connection_state"] == "connected"
            assert state["last_completed_id"] == 1
            assert not state["spoofing"]
            _, blocked = await server.login(ip="127.0.0.2")
            assert blocked["code"] == "busy"
            _, blocked = await server.login()
            assert blocked["code"] == "busy"
            assert (await command.call("connect"))["ok"]
            assert backend.bound
            assert (await macro.step(2, "(0,0) B"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"
            assert server.owner.command is not None and server.owner.macro is not None

    asyncio.run(run())


def test_network_loss_after_disconnect_still_expires_and_releases_owner():
    async def run():
        async with running(reconnect_grace=0.05) as (server, backend):
            command, macro = await owned(server)
            await command.call("connect")

            async def instant(step):
                await server.backend.send(step.report)
                await server.backend.release()

            server.perform = instant
            assert (await command.call("disconnect"))["ok"]
            assert server.owner is not None
            await macro.close()
            # The command socket stays open but heartbeats stop: grace expires.
            await eventually(lambda: server.owner is None)
            stale = await command.call("connect")
            assert stale["code"] == "session_expired"
            assert stale["result"] == {
                "last_completed_id": 0,
                "recovery_complete": True,
            }
            assert not backend.bound
            _, expired = await server.login(session=command.session)
            assert expired["code"] == "session_expired"
            assert expired["result"]["recovery_complete"]
            _, fresh = await server.login(ip="127.0.0.2")
            assert fresh["ok"]

    asyncio.run(run())


def test_replaced_same_session_command_channel_is_not_reported_expired():
    async def run():
        async with running(reconnect_grace=1) as (server, _):
            command, macro = await owned(server)
            command2, hello = await server.login(session=command.session)
            assert hello["ok"]
            try:
                response = await command.call("status")
            except EOFError:
                response = {"code": "invalid_session"}
            assert response["code"] == "invalid_session"
            assert (await command2.call("status"))["ok"]

    asyncio.run(run())


@pytest.mark.parametrize("command", ["restart", "reboot"])
def test_system_action_ack_is_on_wire_before_runner_closes_connections(command):
    async def run():
        async with running() as (server, backend):
            client, macro = await owned(server)
            await client.call("connect")
            seen = []

            async def stop_like_systemd(name):
                # The ACK must already be readable by the client when the
                # runner starts; then systemd tears every connection down.
                seen.append((await client.receive(timeout=0.5), backend.bound))
                for writer in list(server.writers):
                    writer.transport.abort()

            server.action = stop_like_systemd
            await client.send({"id": 9, "command": command})
            await eventually(lambda: seen)
            ((reply, bound),) = seen
            assert reply["id"] == 9 and reply["ok"] and not bound
            assert backend.events[-1] == "detach"
            with pytest.raises((EOFError, ConnectionError)):
                await client.receive()

    asyncio.run(run())


@pytest.mark.parametrize("command", ["restart", "reboot"])
def test_denied_system_action_is_rejected_before_ack(command):
    async def run():
        async with running() as (server, backend):
            client, macro = await owned(server)

            async def denied(name):
                raise RuntimeError("Fixed systemctl action is not permitted")

            server.authorize = denied
            response = await client.call(command)
            assert not response["ok"]
            assert server.actions == []
            assert not server.maintenance
            assert (await client.call("status"))["ok"]

    asyncio.run(run())


def test_release_cleans_live_usb_acks_then_frees_owner_without_grace_error():
    async def run():
        async with running(reconnect_grace=0.05) as (server, backend):
            command, macro = await owned(server)
            assert (await command.call("connect"))["ok"]
            assert (await macro.step(1, "(0,0) A"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"
            performed = []

            async def instant(step):
                performed.append(step)
                await server.backend.send(step.report)
                await server.backend.release()

            server.perform = instant
            reply = await command.call("release")
            assert reply["ok"]
            result = reply["result"]
            assert result["released"] and result["released_session"] == command.session
            assert result["session"] is None
            assert result["connection_state"] == "idle"
            assert result["last_completed_id"] == 1
            assert result["recovery_complete"] and not result["spoofing"]
            assert len(performed) == len(RECOVERY_STEPS)
            assert backend.events[-1] == "detach" and not backend.bound
            # The acknowledgement arrived first; then the server closes both channels.
            for client in (command, macro):
                with pytest.raises((EOFError, ConnectionError)):
                    await client.receive(timeout=1)
            await asyncio.sleep(0.2)
            assert server.owner is None and not server.recovering
            assert server.errors.snapshot() == []
            _, expired = await server.login(session=command.session)
            assert expired["code"] == "session_expired"
            assert expired["result"] == {
                "last_completed_id": 1,
                "recovery_complete": True,
            }
            other, response = await server.login(ip="127.0.0.2")
            assert response["ok"]
            assert response["result"]["errors"] == []

    asyncio.run(run())


@pytest.mark.parametrize("during", [False, True])
def test_release_after_or_during_disconnect_runs_cleanup_once(during):
    async def run():
        async with running(reconnect_grace=0.05) as (server, backend):
            command, macro = await owned(server)
            assert (await command.call("connect"))["ok"]
            performed = []

            async def slow(step):
                performed.append(step)
                await server.backend.send(step.report)
                await asyncio.sleep(0.01)
                await server.backend.release()

            server.perform = slow
            await command.send({"id": 90, "command": "disconnect"})
            if during:
                await eventually(lambda: server.recovering)
            else:
                assert (await command.receive())["ok"]
            # Release joins a running cleanup; it never cancels or repeats it.
            await command.send({"id": 91, "command": "release"})
            replies = {}
            for _ in range(2 if during else 1):
                frame = await command.receive()
                replies[frame["id"]] = frame
            assert replies.get(90, {"ok": True})["ok"]
            reply = replies[91]
            assert reply["ok"]
            assert reply["result"]["released"]
            assert reply["result"]["recovery_complete"]
            assert len(performed) == len(RECOVERY_STEPS)
            events = backend.events[backend.events.index("connect") :]
            assert events.count("detach") == 1 and not backend.bound
            await asyncio.sleep(0.2)
            assert server.owner is None and server.errors.snapshot() == []

    asyncio.run(run())


def test_release_waits_for_inflight_connect_and_never_leaves_usb_bound():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            original = backend.connect

            async def slow_connect():
                await asyncio.sleep(0.1)
                await original()

            backend.connect = slow_connect
            server.perform = lambda step: asyncio.sleep(0)
            await command.send({"id": 1, "command": "connect"})
            await command.send({"id": 2, "command": "release"})
            assert (await command.receive())["id"] == 1
            reply = await command.receive()
            assert reply["id"] == 2 and reply["ok"]
            assert not backend.bound and server.owner is None

    asyncio.run(run())


def test_release_keeps_lease_when_usb_fails_to_detach():
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            assert (await command.call("connect"))["ok"]
            server.perform = lambda step: asyncio.sleep(0)

            async def stuck():
                raise OSError("unbind failed")

            backend.detach = stuck
            reply = await command.call("release")
            assert reply["code"] == "hardware_error"
            assert server.owner is not None and backend.bound
            assert (await command.call("status"))["ok"]

    asyncio.run(run())


@pytest.mark.parametrize("command_name", ["disconnect", "release"])
def test_skip_cleanup_interrupts_neutralizes_and_unbinds_without_menu(command_name):
    async def run():
        async with running(reconnect_grace=0.05) as (server, backend):
            command, macro = await owned(server)
            assert (await command.call("connect"))["ok"]
            assert (await macro.step(1, "(0,0) A"))["event"] == "accepted"
            assert (await macro.receive())["event"] == "completed"
            assert (await macro.step(2, "(5,0) B"))["event"] == "accepted"
            await eventually(lambda: server.active is not None)
            performed = []

            async def record(step):
                performed.append(step)

            server.perform = record
            mark = len(backend.events)
            reply = await command.call(command_name, skip_cleanup=True)
            assert reply["ok"]
            failed = await macro.receive()
            assert failed["id"] == 2 and failed["code"] == "interrupted"
            assert performed == []
            assert backend.events[mark:][-2:] == [NEUTRAL, "detach"]
            assert not backend.bound
            result = reply["result"]
            assert result["last_completed_id"] == 1
            if command_name == "release":
                assert result["released"] and result["recovery_complete"]
                assert server.owner is None
            else:
                assert result["session"] == command.session
                assert result["connection_state"] == "connected"
                assert (await command.call("connect"))["ok"]
            assert server.errors.snapshot() == []

    asyncio.run(run())


@pytest.mark.parametrize("value", ["true", 1, None, [True]])
def test_skip_cleanup_rejects_non_boolean_and_false_keeps_menu(value):
    async def run():
        async with running() as (server, backend):
            command, macro = await owned(server)
            assert (await command.call("connect"))["ok"]
            performed = []

            async def record(step):
                performed.append(step)

            server.perform = record
            for name in ("disconnect", "release"):
                reply = await command.call(name, skip_cleanup=value)
                assert reply["code"] == "invalid_frame"
            assert backend.bound and server.owner is not None and performed == []
            assert (await command.call("disconnect", skip_cleanup=False))["ok"]
            assert len(performed) == len(RECOVERY_STEPS) and not backend.bound

    asyncio.run(run())
