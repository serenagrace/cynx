# Protocol (version 1)

cynx has three listeners, all bound to `listen_host`:

| Channel | Transport | Default port |
| --- | --- | --- |
| Discovery | UDP | `command_port` (3140) |
| Command | TCP | `command_port` (3140) |
| Macro | TCP | `macro_port` (3141) |

## Framing

Every TCP message is one UTF-8 JSON object followed by a newline. A frame may
be at most 16384 bytes including the newline. Oversized, incomplete or
non-JSON frames are rejected with `frame_too_large` or `invalid_frame`.

Command requests carry an integer `id` and a `command`. Replies echo the `id`:

```json
{"id": 4, "ok": true, "result": {}}
{"id": 4, "ok": false, "error": "Human-readable reason", "code": "stable_code", "result": {}}
```

`result` on a failure is optional. Clients should branch on `code`, not
`error` text.

## Discovery

Broadcast to the command port:

```json
{"type": "cynx_discover", "protocol": 1, "nonce": "random"}
```

cynx replies by unicast to the sender:

```json
{"type": "cynx_offer", "identity": "cypi", "protocol": 1, "nonce": "random", "command_port": 3140, "macro_port": 3141}
```

Discovery never claims, authenticates or binds anything. While a session is
owned, offers go only to the owner's IP address.

## Sessions and ownership

The first command frame must be `hello`:

```json
{"id": 1, "command": "hello", "protocol": 1, "passphrase": "", "session": null}
```

A successful result contains `identity: "cypi"`, `protocol: 1`, an opaque
random `session`, a `boot_id` unique to this service run, `command_port`,
`macro_port`, `reconnect_grace`, `heartbeat_timeout`, `recovery_timeout` and
every [status](#status) field.

- The first authenticated host exclusively owns **both** channels, locked to
  its IP address and session. A wrong passphrase is rejected before anything is
  claimed.
- Other hosts, and new `hello` requests with `session: null`, are rejected
  with `busy` (or `recovering`) while a session is owned, reconnecting or
  recovering.
- The owner may resume by sending `hello` with its existing `session`, which
  safely replaces that session's old connection. A replaced connection is
  closed or answered with `invalid_session`.
- Every later command uses the same connection and may include `session`.

### Liveness

Any valid command counts as a heartbeat; macro traffic does not. Clients should
send `status` about once a second. If no command arrives within
`heartbeat_timeout` (3 s), or either channel closes, the session enters
`reconnecting`; see [operations](operations.md#network-loss-and-recovery).
Explicit `connect`/`disconnect` cleanup never times out the owner's own
session.

### Expired sessions

After a lost session's grace period expires, cynx runs recovery and releases
ownership. For 60 seconds afterwards (up to 16 sessions), resuming that session
with `hello`, or sending any request on one of its still-open connections,
returns:

```json
{"id": 7, "ok": false, "code": "session_expired", "error": "...", "result": {"last_completed_id": 12, "recovery_complete": true}}
```

`recovery_complete` means the cleanup sequence was delivered, not that the game
confirmed it. Sessions do not survive a service restart; `boot_id` changes.

## Commands

The command set is fixed. No request can run an arbitrary program or shell
string.

| Command | Effect |
| --- | --- |
| `hello` | Authenticate and claim or resume a session |
| `status` | Return [status](#status); the usual heartbeat |
| `connect` | Bind USB and send a neutral report. Fails with `usb_unavailable` if the host does not enumerate; the session stays usable |
| `disconnect` | Run the cleanup sequence and unbind USB; the session and both channels stay owned |
| `stop` | Immediately cancel the active step, clear the queue and send neutral; USB stays bound |
| `restart` | Release and unbind, acknowledge, then restart `cynx.service` |
| `reboot` | Release and unbind, acknowledge, then reboot the Pi |

`restart` and `reboot` first check, without executing anything, that sudo
permits the fixed `systemctl` command. A denied check returns `ok: false` and
the service keeps running. Otherwise the success reply is written to the
socket before the action runs, and the connection closes shortly afterwards.
A failure after that point is recorded in `status.errors`.

## Macro channel

The first macro frame attaches the channel to the owned session:

```json
{"id": 0, "command": "attach", "session": "...", "passphrase": ""}
```

The reply has the usual `ok`/`result` form, with `identity: "cypi"` and the
`session`. Each following frame submits one input step:

```json
{"id": 1, "session": "...", "step": "(0.15,0.15) L R"}
```

cynx answers promptly with:

```json
{"id": 1, "session": "...", "ok": true, "event": "accepted"}
```

and, only after both the hold and the following wait have elapsed:

```json
{"id": 1, "session": "...", "ok": true, "event": "completed"}
```

or a failure:

```json
{"id": 1, "session": "...", "ok": false, "event": "failed", "error": "...", "code": "stopped"}
```

`completed` means the timed reports were delivered to the USB host. It is not
an acknowledgement from the Switch or the game.

### Step IDs and replay protection

- New step IDs must be positive and increasing. Lower, unknown IDs fail with
  `stale_id`.
- Resubmitting a retained ID with identical text never runs it again. A
  completed step returns `accepted` and `completed`; an active step waits for
  its result; a failed step returns its failure again. Different text under
  the same ID fails with `id_conflict`.
- The ledger keeps at least the last 256 records and never evicts unfinished
  steps.
- The internal queue holds one accepted step. Cynthia keeps one step in flight.
- `status.last_completed_id` is the highest successfully completed ID, so a
  reconnecting client can learn whether its last step finished.

### Step syntax

A frame carries exactly one serialized Cynthia input step, not a named macro or
conditional.

```text
[noblock] [(hold,wait) | tap | hold | wait] [controls...] [noblock]
```

| Form | Hold / wait (s) |
| --- | --- |
| plain, e.g. `A`, `L R` | 0.15 / 0.15 |
| `(hold,wait)`, e.g. `(0.1,0.2) H`, `(1.0,1.0)` | as given |
| `tap A` | 0.05 / 0 |
| `hold C` | 1 / 0.15 |
| `wait` (no controls) | 1 / 1 |

- Controls: `Y B A X L R ZL ZR LS RS H C + - ^ V < >`. `HOME` is an alias for
  `H`. Controls are usually space separated; contiguous tokens such as `LR`
  also work.
- `^ V < >` form the D-pad, including diagonals; opposite directions are
  rejected. D-pad and buttons may be combined.
- A step with no controls holds neutral.
- Durations must be finite and non-negative, holds at most `max_hold` and
  waits at most `max_wait`. Zero durations are allowed.
- `SL` and `SR` are Joy-Con-only controls and are rejected with
  `unsupported_control`, never remapped.
- `noblock` is accepted for compatibility but steps still run sequentially and
  complete before the next starts; it does not enable buffering.

Each step becomes an 8-byte HID report: 14 button bits (little-endian
`uint16`), a hat value (0 up, 2 right, 4 down, 6 left, odd values for
diagonals, 8 neutral), four centered axes (128) and a zero byte.

## Status

`status` (and a successful `hello`) returns:

| Field | Meaning |
| --- | --- |
| `identity`, `protocol`, `version` | `cypi`, `1`, package version |
| `uptime` | Pi uptime in seconds |
| `boot_id` | Unique per service run; changes on restart |
| `session` | Current owner's session, or null |
| `connection_state` | `idle` (no owner), `connected` (both channels live), `reconnecting` or `recovering` |
| `usb_controller_present` | Linux has a USB device controller |
| `usb_detected` | A controller exists **and** the HID node exists; not proof of a cable |
| `usb_state` | Kernel UDC state, such as `not attached` or `configured` |
| `spoofing` | This gadget is bound |
| `switch_connected` | Bound and the UDC is `configured` (USB enumeration, not game readiness) |
| `last_completed_id` | Highest completed step ID, initially 0 |
| `active_id`, `queue_size` | Running step ID or null, and queued steps |
| `recovering` | Cleanup or detach is in progress |
| `errors`, `last_error` | Recent errors, see below |
| `command_port`, `macro_port`, limits | Configured ports and time limits |

`errors` holds up to the last 50 records `{id, time, message}`. `id` increases
monotonically within a `boot_id`, `time` is a Unix timestamp, and `message` is
a redacted summary of at most 160 encoded bytes, so a full status always fits
one frame. `last_error` is the newest record or null. Deduplicate on
`(boot_id, id)`. Recorded errors include hardware failures, failed commands,
grace-expiry recovery, worker or watchdog failures and failed restart or reboot
actions.

## Error codes

| Code | Meaning |
| --- | --- |
| `unauthorized` | Wrong passphrase |
| `protocol_mismatch` | First frame was not `hello` (or macro `attach` with id 0), or protocol is not 1 |
| `busy` | Another session owns the controller |
| `recovering` | Recovery is running; try again later |
| `session_expired` | The session's lease was released; see [above](#expired-sessions) |
| `invalid_session` | Missing, mismatched or replaced session or connection |
| `unknown_command` | Command is not in the allowlist |
| `invalid_frame`, `frame_too_large` | Malformed or oversized frame |
| `invalid_step`, `unsupported_control` | Step text rejected; nothing was sent |
| `invalid_id`, `stale_id`, `id_conflict` | Step ID rejected |
| `queue_full` | One step is already queued |
| `not_connected` | Send `connect` before steps |
| `unavailable` | Paused, stopping, or a system action is already requested |
| `usb_unavailable` | USB bind or a HID write failed |
| `interrupted` | Step cancelled by grace-expiry recovery or shutdown |
| `stopped` | Step cancelled by `stop` |
| `internal_error`, `hardware_error` | Unexpected failure; see the service log |

`usb_unavailable`, `interrupted` and `stopped` are terminal for that step ID;
resubmitting it returns the same failure.
