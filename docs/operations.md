# Operations

## Normal control

Cynthia's `/cynx` commands drive the service. Any client follows the same
order:

1. Discover the Pi, or use its configured address.
2. Send `hello` on the command port and `attach` on the macro port.
3. Send `connect` to bind USB, then submit steps on the macro channel.
4. Send `status` about once a second as a heartbeat.
5. Send `disconnect` to run the cleanup sequence and unbind USB. The session
   stays owned, so the same client can `connect` again later.
6. When shutting down for good, send `release` instead of just closing the
   sockets. It runs any cleanup still needed, ends the session and closes both
   channels without a reconnect grace period or error.

`status` and `stop` use the command channel, so they stay responsive during
long holds.

## Stop

`stop` cancels the active step at once, clears the queue and sends a neutral
report. The cancelled step fails with `stopped`. USB stays bound and no menu
sequence runs. If a recovery is in progress, `stop` cancels the menu inputs but
the final unbind still completes.

## Cleanup sequence

`disconnect` and grace-expiry recovery send the same sequence as the `cleanup`
macro in Cynthia's Bluetooth backend, then unbind USB:

| Step | Hold / wait (s) |
| --- | --- |
| `H` (HOME) | 0.15 / 0.15 |
| `wait` (neutral) | 1 / 1 |
| `V` (down) | 0.15 / 0.15 |
| `>` (right) ×6 | 0.15 / 0.15 each |
| `hold A` | 1 / 0.15 |
| `noblock A` | 0.15 / 0.15 |

With default timings this takes about 5.85 seconds. The two seconds of neutral
after HOME let the HOME menu open before DOWN. The sequence assumes the Switch
is on a screen where it reaches the controller menu; delivery is not proof of
game state. The whole cleanup, including release and unbind, is bounded by
`recovery_timeout` (at most 10 seconds), and a vanished USB host cannot block
the final unbind.

## Network loss and recovery

When either channel closes or the command heartbeat stops:

1. The session enters `reconnecting`. The active step may finish; queued work
   pauses at the next step boundary.
2. Within `reconnect_grace` (5 s) the owner can resume with `hello` using the
   same session from the same IP and reattach the macro channel. The completion
   ledger is kept, so already-completed steps are not replayed. No other host
   can claim the controller meanwhile.
3. If grace expires, unfinished work fails with `interrupted`, the queue is
   cleared, controls are released, the cleanup sequence runs and USB is
   unbound. Ownership is then released and the session becomes
   [`session_expired`](protocol.md#expired-sessions).

Explicit `disconnect` uses the identical cleanup and unbind (unless the owner
sends [`skip_cleanup`](protocol.md#commands)) but keeps the
session, both channels and the ledger; only grace expiry, `release` and
service shutdown release the owner. Neither is a transactional rollback of game state.

## Restart and reboot

The `restart` and `reboot` commands release controls and unbind USB, check that
sudo permits the fixed `systemctl` command, send the success reply, and only
then run it. Expect the connection to close right after the reply. After a
restart or reboot the service starts idle and unbound with a new `boot_id`;
clients must rediscover and send `hello` again.

From a shell:

```bash
sudo systemctl restart cynx
sudo systemctl stop cynx            # unbinds; stays stopped until started
sudo systemctl disable cynx         # no autostart at boot
journalctl -u cynx -n 100 --no-pager
```

Code and configuration changes need only a service restart, not a reboot.
Restart while the controller is idle where possible.

## Troubleshooting

Check the USB status fields separately:

| Symptom | Likely cause |
| --- | --- |
| `usb_controller_present: false` | Device-mode overlay missing or `dwc2` not loaded |
| `usb_detected: false` with a controller | Service preparation failed; see the journal |
| `spoofing: true`, `usb_state` not `configured` | Cable, power, dock or the Switch is not enumerating |
| `connect` returns `usb_unavailable` | The host did not accept reports within 3 s; check the Switch is awake and docked |
| `busy` on `hello` | Another host or session owns the service; wait for it to disconnect or expire |

An `accepted` step without `completed` does not show whether the game acted.
Resume with the original session and compare `last_completed_id` before
deciding whether to resend. Never run a second HID writer, such as
`cynx --test`, while the service is active.

## Limitations

- One step runs at a time; there is no client-side buffering.
- No analog sticks, motion controls, SL/SR or wake from sleep.
- USB status reflects enumeration, not whether a game received input.
- Recovery assumes the menu layout reached from HOME and is not a transaction.
- Power loss cannot run cleanup; the stop hook protects crashes and stops, not
  a powered-off Pi.
- Sessions, tombstones and the error history are in memory and reset on
  restart.
- Backend selection, such as falling back to Bluetooth, is decided by Cynthia,
  not by cynx.
