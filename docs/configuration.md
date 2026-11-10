# Configuration and security

## config.yaml

The service reads `config.yaml` from its working directory. Copy
`config.example.yaml` to start; the real file is gitignored. Unknown keys and
invalid values stop the service at startup with an error.

| Key | Default | Meaning |
| --- | --- | --- |
| `listen_host` | `0.0.0.0` | Address for the TCP listeners and UDP discovery |
| `command_port` | `3140` | TCP command channel **and** UDP discovery |
| `macro_port` | `3141` | TCP macro (input step) channel |
| `max_hold` | `30` | Longest allowed hold, in seconds |
| `max_wait` | `300` | Longest allowed wait after a step, in seconds |
| `reconnect_grace` | `5` | Seconds a lost client has to reconnect before recovery |
| `heartbeat_timeout` | `3` | Seconds without a command before the client counts as lost |
| `recovery_timeout` | `10` | Bound for the cleanup sequence and detach; at most 10 |
| `device` | `/dev/cynx/hidg0` | HID node prepared by the service's root hook |

Restart the service after changing configuration:
`sudo systemctl restart cynx`.

## Passphrase

`.env` in the working directory may set:

```dotenv
CYNX_PASSPHRASE=
```

An empty or missing value disables authentication. When it is set, Cynthia
must send the same value in `hello` and `attach`. The value is compared in
constant time, is never logged or echoed in errors, and is redacted from error
summaries and logs. A `CYNX_PASSPHRASE` environment variable takes precedence
over the file.

Keep `.env` mode 600, never commit it, and never store it on a shared drive.
Provision the same value on the Cynthia host.

## Network exposure

- The command port must accept both TCP and UDP; opening only TCP breaks
  discovery. The macro port needs TCP only.
- Restrict both ports to trusted LAN hosts and never forward them from the
  internet.
- Traffic is not encrypted. The passphrase protects ownership of the
  controller, not the confidentiality of traffic, and does not prevent
  denial of service.
- Ownership is also locked to the first authenticated client's IP address.

## Logging

The service writes `logs/cynx.log` under its working directory and mirrors the
same lines to stdout, which journald collects:

```bash
journalctl -u cynx -n 100 --no-pager
tail -n 100 logs/cynx.log
```

`logs/` is gitignored and created with private permissions. The log rotates at
1 MiB and keeps three backups. Local logs keep full tracebacks; the passphrase
is redacted, and requests carrying it are never logged.

Recent errors are also kept in memory and returned in `status` as concise
summaries, so the client can relay them; see [protocol](protocol.md#status).
Routine discovery, rejected authentication and invalid requests are not
recorded as errors.
