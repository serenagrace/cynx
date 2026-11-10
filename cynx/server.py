"""Exclusive two-channel controller service and reconnect/recovery lifecycle."""

import asyncio
import contextlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

from cynx.backend import Backend, authorize_system_action, system_action
from cynx.config import Config
from cynx.observability import LOGGER, ErrorJournal
from cynx.protocol import MAX_FRAME, ProtocolError, parse_step, read_frame, send_frame

# Mirrors Cynthia's NXBT built-in cleanup macro. The full wait gives the HOME
# menu time to open before DOWN; noblock is still sequential on wired USB.
RECOVERY_STEPS = ("H", "wait", "V", *(">",) * 6, "hold A", "noblock A")


@dataclass
class Record:
    id: int
    text: str
    step: object
    done: asyncio.Future
    state: str = "accepted"
    cancel_code: str = "interrupted"


@dataclass
class Session:
    token: str
    ip: str
    command: object = None
    macro: object = None
    heartbeat: float = field(default_factory=time.monotonic)
    records: OrderedDict = field(default_factory=OrderedDict)
    highest: int = 0
    last_completed: int = 0
    recovery_complete: bool = True


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.server = server

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) > MAX_FRAME:
            return
        try:
            request = json.loads(data)
            if (
                not isinstance(request, dict)
                or request.get("type") != "cynx_discover"
                or type(request.get("protocol")) is not int
                or request["protocol"] != 1
                or not isinstance(request.get("nonce"), str)
                or len(request["nonce"]) > 256
            ):
                return
            owner = self.server.owner
            if owner and owner.ip != addr[0]:
                return
            self.transport.sendto(
                json.dumps(
                    {
                        "type": "cynx_offer",
                        **self.server.identity(),
                        "nonce": request["nonce"],
                        "command_port": self.server.command_port,
                        "macro_port": self.server.macro_port,
                    }
                ).encode(),
                addr,
            )
        except (ValueError, UnicodeError, RecursionError):
            return


class Server:
    def __init__(
        self,
        config=None,
        passphrase="",
        backend=None,
        action=system_action,
        authorize=None,
    ):
        self.config = config or Config()
        self.secret = passphrase
        self.errors = ErrorJournal(passphrase)
        self.backend = backend or Backend(self.config.device)
        self.action = action
        if authorize is None and action is system_action:
            authorize = authorize_system_action
        self.authorize = authorize
        self.boot_id = secrets.token_hex(16)
        self.owner = None
        self.tombstones = OrderedDict()
        self.queue = asyncio.Queue(maxsize=1)
        self.ready = asyncio.Event()
        self.active = None
        self.active_task = None
        self.recovery_task = None
        self.grace_task = None
        self.recovering = False
        self.closing = False
        self.maintenance = False
        self.stopping = 0
        self.closed = asyncio.Event()
        self.tasks = set()
        self.writers = set()
        self.listeners = []
        self.transport = None
        self.command_port = self.config.command_port
        self.macro_port = self.config.macro_port
        self.lifecycle = asyncio.Lock()
        self.close_lock = asyncio.Lock()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.task_done)
        return task

    def task_done(self, task):
        self.tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        name = task.get_coro().__name__
        self.errors.report(f"Background {name} task failed", error)
        if task in (getattr(self, "worker", None), getattr(self, "watchdog", None)):
            if not self.closing:
                self.spawn(self.close())

    def identity(self):
        return {"identity": "cynx", "protocol": 1}

    def status(self):
        owner = self.owner
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except OSError:
            uptime = time.monotonic()
        return {
            **self.identity(),
            "version": version("cynx"),
            "uptime": uptime,
            **self.backend.status(),
            "session": owner.token if owner else None,
            "boot_id": self.boot_id,
            "errors": self.errors.snapshot(),
            "last_error": (
                dict(self.errors.records[-1]) if self.errors.records else None
            ),
            "last_completed_id": owner.last_completed if owner else 0,
            "active_id": self.active.id if self.active else None,
            "queue_size": self.queue.qsize(),
            "recovering": self.recovering,
            "connection_state": (
                "recovering"
                if self.recovering
                else (
                    "idle"
                    if owner is None
                    else "connected" if self.channels_ready() else "reconnecting"
                )
            ),
            "command_port": self.command_port,
            "macro_port": self.macro_port,
            "reconnect_grace": self.config.reconnect_grace,
            "heartbeat_timeout": self.config.heartbeat_timeout,
            "recovery_timeout": self.config.recovery_timeout,
            "max_hold": self.config.max_hold,
            "max_wait": self.config.max_wait,
        }

    async def start(self):
        # A previous crash or manual USB test must never auto-restore spoofing.
        try:
            await self.backend.detach()
            for callback, port in (
                (self.command_client, self.command_port),
                (self.macro_client, self.macro_port),
            ):
                self.listeners.append(
                    await asyncio.start_server(
                        callback, self.config.listen_host, port, limit=MAX_FRAME
                    )
                )
            self.command_port, self.macro_port = [
                listener.sockets[0].getsockname()[1] for listener in self.listeners
            ]
            (
                self.transport,
                _,
            ) = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Discovery(self),
                local_addr=(self.config.listen_host, self.command_port),
                allow_broadcast=True,
            )
            self.worker = self.spawn(self.work())
            self.watchdog = self.spawn(self.heartbeat())
            LOGGER.info(
                "Listening idle: command TCP/discovery UDP %s, macro TCP %s",
                self.command_port,
                self.macro_port,
            )
        except BaseException as error:
            if isinstance(error, Exception):
                self.errors.report("Service startup failed", error)
            await self.close()
            raise
        return self

    def channels_ready(self):
        return bool(
            self.owner
            and self.owner.command
            and self.owner.macro
            and not self.owner.command.is_closing()
            and not self.owner.macro.is_closing()
        )

    def refresh_channels(self):
        if self.channels_ready() and not self.recovering and not self.stopping:
            if self.grace_task:
                self.grace_task.cancel()
                self.grace_task = None
            self.ready.set()
        else:
            self.ready.clear()
            if self.owner and not self.recovering and not self.closing:
                if self.grace_task is None:
                    self.grace_task = self.spawn(self.expire(self.owner))

    def authenticate(self, frame):
        supplied = frame.get("passphrase", "")
        if (
            not isinstance(supplied, str)
            or self.secret
            and not hmac.compare_digest(supplied.encode(), self.secret.encode())
        ):
            raise ProtocolError("unauthorized", "Passphrase mismatch")

    def failure(self, id, error, **extra):
        response = {
            "id": id,
            "ok": False,
            "error": self.errors.sanitize(error),
            "code": getattr(error, "code", "hardware_error"),
            **extra,
        }
        if getattr(error, "result", None) is not None:
            response["result"] = error.result
        return response

    def lease_error(self, owner):
        """Distinguish a released lease from a superseded same-session channel."""
        if owner is not self.owner:
            return ProtocolError(
                "session_expired",
                "Session expired; recovery has finished",
                {
                    "last_completed_id": owner.last_completed,
                    "recovery_complete": owner.recovery_complete,
                },
            )
        return ProtocolError("invalid_session", "Connection was replaced")

    def expired(self, frame, ip):
        token = frame.get("session")
        tombstone = self.tombstones.get(token) if isinstance(token, str) else None
        if tombstone and tombstone["ip"] == ip:
            return {
                "id": frame["id"],
                "ok": False,
                "code": "session_expired",
                "error": "Session expired; recovery has finished",
                "result": {
                    "last_completed_id": tombstone["last_completed_id"],
                    "recovery_complete": tombstone["recovery_complete"],
                },
            }
        return self.failure(
            frame["id"], ProtocolError("invalid_session", "Unknown session")
        )

    async def hello(self, frame, writer):
        self.authenticate(frame)
        if (
            frame.get("command") != "hello"
            or type(frame.get("protocol")) is not int
            or frame["protocol"] != 1
        ):
            raise ProtocolError(
                "protocol_mismatch", "First command must be hello protocol 1"
            )
        ip = writer.get_extra_info("peername")[0]
        token = frame.get("session")
        if token is not None and not isinstance(token, str):
            raise ProtocolError("invalid_session", "Session must be a string or null")
        if self.closing or self.maintenance:
            raise ProtocolError("unavailable", "Service is shutting down")
        owner = self.owner
        if token is not None and (not owner or token != owner.token):
            await send_frame(writer, self.expired(frame, ip))
            return None
        if owner:
            if token != owner.token or ip != owner.ip:
                raise ProtocolError("busy", "Another host exclusively owns the service")
            if self.recovering:
                raise ProtocolError("recovering", "Session recovery is in progress")
        else:
            owner = self.owner = Session(secrets.token_urlsafe(24), ip)
        old = owner.command
        owner.command = writer
        owner.heartbeat = time.monotonic()
        if old and old is not writer:
            old.close()
        self.refresh_channels()
        await send_frame(
            writer, {"id": frame["id"], "ok": True, "result": self.status()}
        )
        return owner

    async def command_client(self, reader, writer):
        self.writers.add(writer)
        owner = None
        pending = set()
        frame = {}
        released = False
        try:
            frame = await asyncio.wait_for(read_frame(reader), 10)
            owner = await self.hello(frame, writer)
            if owner is None:
                return
            while not self.closing:
                frame = await read_frame(reader)
                task = self.spawn(self.command(frame, writer, owner))
                pending.add(task)
                task.add_done_callback(pending.discard)
                if frame.get("command") == "release":
                    # A released channel accepts nothing further.
                    released = await asyncio.shield(task) is True
                    if released:
                        break
        except (EOFError, ConnectionError, asyncio.TimeoutError):
            pass
        except ProtocolError as error:
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                await send_frame(writer, self.failure(frame.get("id", 0), error))
        finally:
            if released and pending:
                # Let replies already in flight (e.g. a joined disconnect) land.
                await asyncio.wait(pending, timeout=1)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if owner is self.owner and owner and owner.command is writer:
                owner.command = None
                self.refresh_channels()
            writer.close()
            self.writers.discard(writer)
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def command(self, frame, writer, owner):
        command = frame.get("command")
        if not isinstance(command, str):
            command = None
        executing = True
        try:
            if owner is not self.owner or owner.command is not writer:
                raise self.lease_error(owner)
            if frame.get("session", owner.token) != owner.token:
                raise ProtocolError("invalid_session", "Session mismatch")
            if not isinstance(command, str) or command not in {
                "status",
                "connect",
                "disconnect",
                "release",
                "stop",
                "restart",
                "reboot",
            }:
                raise ProtocolError("unknown_command", "Command is not allowed")
            skip_cleanup = frame.get("skip_cleanup", False)
            if command in {"disconnect", "release"} and type(skip_cleanup) is not bool:
                raise ProtocolError("invalid_frame", "skip_cleanup must be a boolean")
            owner.heartbeat = time.monotonic()
            if command == "connect":
                async with self.lifecycle:
                    if owner is not self.owner:
                        raise self.lease_error(owner)
                    if self.recovering or self.maintenance:
                        raise ProtocolError(
                            "recovering", "Lifecycle transition in progress"
                        )
                    try:
                        await self.backend.connect()
                    finally:
                        # Bind time (up to the enumeration bound) must not
                        # count against the client's serialized heartbeat.
                        if owner is self.owner:
                            owner.heartbeat = time.monotonic()
                    LOGGER.info("USB controller connected")
            elif command == "stop":
                if self.recovery_task and not self.recovery_task.done():
                    self.recovery_task.cancel()
                    await asyncio.gather(self.recovery_task, return_exceptions=True)
                await self.stop_work("stopped")
            elif command == "disconnect":
                await self.cleanup(menu=not skip_cleanup)
            elif command == "release":
                if self.maintenance:
                    raise ProtocolError(
                        "unavailable", "System action already requested"
                    )
                cleaned = False
                while True:
                    async with self.lifecycle:
                        if owner is not self.owner:
                            raise self.lease_error(owner)
                        cleaning = bool(
                            self.recovery_task and not self.recovery_task.done()
                        )
                        if not self.backend.status()["spoofing"] and not cleaning:
                            self.retire(owner)
                            break
                        if cleaned and not cleaning:
                            # Keep the lease so loss recovery can retry detach.
                            raise ProtocolError(
                                "hardware_error", "USB controller did not detach"
                            )
                    await self.cleanup(menu=not skip_cleanup)
                    cleaned = True
                result = {
                    **self.status(),
                    "released": True,
                    "released_session": owner.token,
                    "last_completed_id": owner.last_completed,
                    "recovery_complete": owner.recovery_complete,
                }
            elif command in {"restart", "reboot"}:
                if self.maintenance:
                    raise ProtocolError(
                        "unavailable", "System action already requested"
                    )
                self.maintenance = True
                await self.stop_work("interrupted")
                await self.backend.detach()
                if self.authorize is not None:
                    await self.authorize(command)
            if command != "release":
                result = self.status()
            executing = False
            await send_frame(writer, {"id": frame["id"], "ok": True, "result": result})
            if command == "release":
                # Ownership is already gone, so closing starts no reconnect
                # grace; the command channel closes in its reader loop.
                if owner.macro:
                    owner.macro.close()
                return True
            if command in {"restart", "reboot"}:
                # systemd stops this process almost immediately, so the
                # acknowledgement must be on the wire before dispatch.
                self.spawn(self.dispatch(command))
        except (ProtocolError, OSError, RuntimeError) as error:
            if not executing:
                writer.close()
                return
            if not isinstance(error, ProtocolError):
                self.errors.report(f"{command} command failed", error)
            if command in {"restart", "reboot"}:
                self.maintenance = False
            if command == "connect" and not isinstance(error, ProtocolError):
                error = ProtocolError("usb_unavailable", str(error))
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                await send_frame(writer, self.failure(frame["id"], error))
        except (ConnectionError, asyncio.TimeoutError):
            writer.close()
        except Exception as error:
            self.errors.report(f"{command} command failed unexpectedly", error)
            if command in {"restart", "reboot"}:
                self.maintenance = False
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                await send_frame(
                    writer,
                    self.failure(
                        frame["id"],
                        ProtocolError("internal_error", "Command execution failed"),
                    ),
                )

    async def macro_client(self, reader, writer):
        self.writers.add(writer)
        owner = None
        pending = set()
        frame = {}
        try:
            frame = await asyncio.wait_for(read_frame(reader), 10)
            self.authenticate(frame)
            owner = self.owner
            ip = writer.get_extra_info("peername")[0]
            if frame.get("command") != "attach" or frame["id"] != 0:
                raise ProtocolError(
                    "protocol_mismatch", "First macro frame must be attach id 0"
                )
            if not owner or frame.get("session") != owner.token:
                await send_frame(writer, self.expired(frame, ip))
                return
            if owner.ip != ip:
                raise ProtocolError("busy", "Session belongs to another host IP")
            if self.recovering or not owner.command or self.maintenance:
                raise ProtocolError("unavailable", "Command channel is not available")
            old = owner.macro
            owner.macro = writer
            if old and old is not writer:
                old.close()
            self.refresh_channels()
            await send_frame(
                writer,
                {
                    "id": 0,
                    "ok": True,
                    "result": {**self.identity(), "session": owner.token},
                },
            )
            while not self.closing:
                frame = await read_frame(reader)
                try:
                    record = self.admit(frame, writer, owner)
                    await send_frame(
                        writer,
                        {
                            "id": record.id,
                            "session": owner.token,
                            "ok": True,
                            "event": "accepted",
                        },
                    )
                    task = self.spawn(self.notify(record, owner, writer))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                except ProtocolError as error:
                    await send_frame(
                        writer,
                        self.failure(
                            frame["id"], error, session=owner.token, event="failed"
                        ),
                    )
        except (EOFError, ConnectionError, asyncio.TimeoutError):
            pass
        except ProtocolError as error:
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                await send_frame(writer, self.failure(frame.get("id", 0), error))
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if owner is self.owner and owner and owner.macro is writer:
                owner.macro = None
                self.refresh_channels()
            writer.close()
            self.writers.discard(writer)
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def admit(self, frame, writer, owner):
        if owner is not self.owner or owner.macro is not writer:
            raise self.lease_error(owner)
        if frame.get("session") != owner.token:
            raise ProtocolError("invalid_session", "Session mismatch")
        id, text = frame["id"], frame.get("step")
        if id <= 0:
            raise ProtocolError("invalid_id", "Step id must be positive")
        if id in owner.records:
            record = owner.records[id]
            if record.text != text:
                raise ProtocolError("id_conflict", "Step id already has different text")
            return record
        if (
            self.recovering
            or self.maintenance
            or self.stopping
            or not self.channels_ready()
        ):
            raise ProtocolError("unavailable", "Session is paused")
        if not self.backend.status()["spoofing"]:
            raise ProtocolError("not_connected", "Connect USB before sending steps")
        if id <= owner.highest:
            raise ProtocolError("stale_id", "New step ids must increase")
        step = parse_step(text, self.config.max_hold, self.config.max_wait)
        if self.queue.full():
            raise ProtocolError("queue_full", "One buffered step is already accepted")
        record = Record(id, text, step, asyncio.get_running_loop().create_future())
        owner.records[id] = record
        owner.highest = id
        self.queue.put_nowait(record)
        self.trim(owner)
        return record

    def trim(self, owner):
        while len(owner.records) > 256:
            completed = next(
                (id for id, item in owner.records.items() if item.done.done()), None
            )
            if completed is None:
                break
            del owner.records[completed]

    async def notify(self, record, owner, writer):
        try:
            result = await asyncio.shield(record.done)
            if owner.macro is writer:
                await send_frame(
                    writer, {"id": record.id, "session": owner.token, **result}
                )
        except (ConnectionError, asyncio.TimeoutError):
            writer.close()

    async def perform(self, step):
        try:
            await self.backend.send(step.report)
            await asyncio.sleep(step.hold)
        finally:
            await self.backend.release()
        await asyncio.sleep(step.wait)

    def finish(self, record, result):
        if not record.done.done():
            record.state = result["event"]
            record.done.set_result(result)

    async def work(self):
        while not self.closing:
            await self.ready.wait()
            record = await self.queue.get()
            if not self.channels_ready() or self.recovering or self.stopping:
                # A loss between queue wakeup and dequeue pauses this accepted step.
                self.queue.task_done()
                self.queue.put_nowait(record)
                continue
            owner = self.owner
            self.active = record
            self.active_task = asyncio.create_task(self.perform(record.step))
            try:
                await self.active_task
                owner.last_completed = max(owner.last_completed, record.id)
                self.finish(record, {"ok": True, "event": "completed"})
            except asyncio.CancelledError:
                self.finish(
                    record,
                    {
                        "ok": False,
                        "event": "failed",
                        "error": "Step cancelled",
                        "code": record.cancel_code,
                    },
                )
                if self.closing:
                    raise
            except (OSError, RuntimeError) as error:
                self.errors.report("USB step execution failed", error)
                self.finish(
                    record,
                    {
                        "ok": False,
                        "event": "failed",
                        "error": self.errors.sanitize(error),
                        "code": "usb_unavailable",
                    },
                )
            except Exception:
                self.finish(
                    record,
                    {
                        "ok": False,
                        "event": "failed",
                        "error": "Internal worker failure",
                        "code": "interrupted",
                    },
                )
                raise
            finally:
                self.active = self.active_task = None
                self.queue.task_done()
                self.trim(owner)

    async def stop_work(self, code):
        self.stopping += 1
        self.ready.clear()
        try:
            while not self.queue.empty():
                record = self.queue.get_nowait()
                self.queue.task_done()
                self.finish(
                    record,
                    {
                        "ok": False,
                        "event": "failed",
                        "error": "Step cancelled",
                        "code": code,
                    },
                )
            task = self.active_task
            if task:
                self.active.cancel_code = code
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            try:
                await self.backend.release()
            except (OSError, RuntimeError) as error:
                self.errors.report("USB neutral release failed", error)
        finally:
            self.stopping -= 1
            if not self.closing:
                self.refresh_channels()

    async def expire(self, owner):
        await asyncio.sleep(self.config.reconnect_grace)
        if self.owner is owner and not self.channels_ready():
            self.errors.report(
                "Reconnect grace expired; cancelling work and recovering USB"
            )
            await asyncio.shield(self.begin_recovery())

    async def cleanup(self, *, menu=True):
        """Wait for owner-retaining cleanup, joining one already running.

        ``menu=False`` neutralizes and unbinds without the menu macro, for a
        host that has just verified the Switch is already on Change Grip/Order.
        """
        recovery = self.begin_recovery(release_owner=False, menu=menu)
        try:
            await asyncio.shield(recovery)
        except asyncio.CancelledError:
            if not recovery.cancelled():
                raise

    def begin_recovery(self, *, release_owner=True, menu=True):
        """Run the shared cleanup macro and detach.

        Explicit disconnect only idles USB and keeps the authenticated network
        lease; expired network loss also releases the owner afterwards.
        """
        if self.recovery_task is None or self.recovery_task.done():
            self.recovering = True
            LOGGER.info("USB menu recovery started")
            self.ready.clear()
            self.recovery_task = self.spawn(
                self.recover(self.owner, release_owner, menu)
            )
        return self.recovery_task

    async def recover(self, owner, release_owner=True, menu=True):
        complete = False
        try:
            budget = self.config.recovery_timeout - min(
                0.5, self.config.recovery_timeout / 2
            )
            async with asyncio.timeout(budget):
                async with self.lifecycle:
                    await self.stop_work("interrupted")
                    if menu and self.backend.status()["spoofing"]:
                        for text in RECOVERY_STEPS:
                            await self.perform(parse_step(text))
                complete = True
        except (TimeoutError, OSError, RuntimeError) as error:
            self.errors.report("USB menu recovery failed or timed out", error)
        finally:
            try:
                await self.backend.detach()
            except (OSError, RuntimeError) as error:
                self.errors.report("USB detach failed after recovery", error)
            finally:
                retained = owner is self.owner and owner and not release_owner
                if owner is self.owner and owner:
                    owner.recovery_complete = complete
                    if release_owner:
                        if owner.macro:
                            owner.macro.close()
                        self.retire(owner)
                self.recovering = False
                LOGGER.info("USB recovery ended; controller detached")
                if self.grace_task:
                    self.grace_task.cancel()
                    self.grace_task = None
                if retained and not self.closing:
                    # Cleanup time must not count against the client heartbeat.
                    owner.heartbeat = time.monotonic()
                    self.refresh_channels()

    def retire(self, owner):
        """End the lease, keeping a short tombstone of its completion state."""
        self.tombstones[owner.token] = {
            "ip": owner.ip,
            "last_completed_id": owner.last_completed,
            "recovery_complete": owner.recovery_complete,
            "expires": time.monotonic() + 60,
        }
        while len(self.tombstones) > 16:
            self.tombstones.popitem(last=False)
        self.owner = None
        if self.grace_task:
            self.grace_task.cancel()
            self.grace_task = None

    async def heartbeat(self):
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            for token, value in list(self.tombstones.items()):
                if value["expires"] < now:
                    del self.tombstones[token]
            owner = self.owner
            if (
                owner
                and not self.recovering
                and not self.maintenance
                and not self.lifecycle.locked()
                and owner.command
                and now - owner.heartbeat > self.config.heartbeat_timeout
            ):
                for writer in (owner.command, owner.macro):
                    if writer:
                        writer.close()
                owner.command = owner.macro = None
                self.refresh_channels()

    async def dispatch(self, command):
        """Run an acknowledged system action; USB is already neutral/detached."""
        try:
            await self.action(command)
        except Exception as error:
            self.errors.report(f"{command} command failed", error)
            self.maintenance = False
            return
        LOGGER.info("System %s requested", command)
        await self.close()

    async def close(self):
        async with self.close_lock:
            if self.closed.is_set():
                return
            self.closing = True
            self.ready.clear()
            for listener in self.listeners:
                listener.close()
            # Python 3.13's wait_closed also waits for accepted connections.
            for writer in tuple(self.writers):
                writer.close()
            for listener in self.listeners:
                await listener.wait_closed()
            if self.transport:
                self.transport.close()
            for task in (self.grace_task, self.recovery_task):
                if task and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await self.stop_work("interrupted")
            # The worker is idle now; cancellation cannot leave a physical hold.
            tasks = [task for task in self.tasks if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for writer in tuple(self.writers):
                writer.close()
            try:
                await self.backend.detach()
            except (OSError, RuntimeError) as error:
                self.errors.report("USB detach failed during shutdown", error)
            finally:
                self.owner = None
                self.closed.set()
                LOGGER.info("Service stopped; controller detached")
