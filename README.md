# smtp-alert-dispatcher

![ci](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/ci.yml/badge.svg)

Alert email delivery for hosts with no mail agent. A caller hands it a subject and a body. It delivers to the relay over STARTTLS, and when the relay is unreachable it writes the message to a disk spool and retries on the next flush.

The design holds one promise: the message is delivered, or it is on disk. Never neither. A degraded network is exactly the condition that produces alerts and exactly the condition under which a fire-and-forget sender loses them.

## Usage

```sh
printf '%s\n' "$alert_body" | smtp_alert_dispatcher.py send \
    --subject "connectivity failures on ${target_host}" ;
```

Exit code 1 from `send` means spooled, not lost. A caller that treats 1 as failure will page twice for one event.

Flush the spool on a timer. It is a no-op when the spool is empty:

```sh
*/5 * * * * root /usr/local/bin/smtp_alert_dispatcher.py flush
```

Check for retired messages from whatever health check wraps everything else:

```sh
/usr/local/bin/smtp_alert_dispatcher.py status ;
```

## Configuration

`/etc/smtp_alert_dispatcher/config.json` overlays the built-in defaults. Any key you omit keeps its default value.

| Key | Default | Notes |
|-----|---------|-------|
| `relay_host` | `mailrelay.example.net` | |
| `relay_port` | `587` | |
| `use_starttls` | `true` | |
| `smtp_username` | `""` | Empty disables authentication |
| `smtp_password_file` | `""` | Path, read at send time; the password is never a config value |
| `sender_address` | `alerts@example.net` | |
| `recipient_addresses` | `["storage-alerts@example.net"]` | |
| `connect_timeout_seconds` | `10` | |
| `max_delivery_attempts` | `20` | Attempts before a message is retired to dead letters |
| `spool_directory` | `/var/spool/smtp_alert_dispatcher/pending` | |
| `dead_letter_directory` | `/var/spool/smtp_alert_dispatcher/dead_letters` | |
| `lock_file` | `/var/lock/smtp_alert_dispatcher.lock` | |

## Exit codes

| Code | `send` | `flush` | `status` |
|------|--------|---------|----------|
| 0 | Delivered | Spool empty after the run | Nothing pending |
| 1 | Spooled for retry | Messages still pending | Messages pending |
| 2 | Configuration error | Configuration error | Dead letters present |

## Design notes

**A failure to deliver is not a failure to alert.** `send` returns 1 when it spools, which is a distinct outcome from losing the message, and the exit codes are arranged so a caller can tell them apart.

**Every spool write goes through one atomic path.** Write to a temporary name the spool glob does not match, fsync the contents, rename over the target, then fsync the directory. Both the initial spool and the attempt-count update during a flush use it. An earlier revision updated the attempt count with a direct write to the live file, which meant a power loss partway through that write truncated a spooled message and lost it outright. The fsync steps matter because rename is atomic in the POSIX namespace but says nothing about data reaching disk; without them a power loss can leave a zero-length record behind a successfully renamed name.

**A retry is the same message, not a new one.** Delivery is at-least-once: the relay can accept a message and the spool file still survive if the connection drops before the unlink. `Message-ID` and `Date` are generated once when the alert is created and reused on every later attempt, so a duplicate arrives as a recognisable duplicate that a client can thread or collapse rather than as another unread alert during the incident.

**Flush sorts failures into three classes.** An unreachable relay is one condition affecting every queued message, so the run stops rather than burning an attempt on all of them. A rejected recipient is specific to one message, so that message is charged an attempt and the run continues. Between those sits the relay session itself: `SMTPAuthenticationError`, `SMTPHeloError` and `SMTPNotSupportedError` all subclass `SMTPException`, so unless they are named explicitly a single stale credential charges an attempt against every message in the spool. At the default ceiling that retires an entire spool to dead letters in about ninety-five minutes.

**Handler order in the flush loop is load-bearing.** `smtplib.SMTPException` subclasses `OSError`. If the bare `OSError` branch is placed first, every message-level rejection is reclassified as a connection failure, `attempt_count` never rises, and nothing ever reaches the dead-letter directory. A permanently undeliverable message then retries forever with no operator signal. The test suite fails if that ordering regresses, and CI reintroduces that defect and four others on every run to confirm the tests still catch them.

**Retirement is visible, not silent.** A message that exhausts `max_delivery_attempts` moves to the dead-letter directory rather than being deleted, and `status` returns 2 while any remain. Dead letters outrank pending messages in the exit code, because a retired message is the louder signal.

**Concurrent flushes are prevented by an advisory lock.** A second flush arriving while the first is running declines and returns 0. Two flush runs over one spool would deliver the same message twice.

## Known limitations

- Delivery is attempted once per flush per message. There is no backoff, so retry timing is entirely a function of how often the flush is scheduled.
- The spool is a flat directory scanned with a glob. It is sized for alert volumes, not for a queue.
- `fsync` on every spool write costs an I/O round trip per message. At alert volumes that is irrelevant; at queue volumes it would not be.
- Threshold and deduplication logic is deliberately absent. Deciding what is worth alerting on belongs to the caller.

## Requirements

Python 3.9 or newer. Standard library only, no third-party packages.

## Tests

```sh
python3 -m unittest discover -s test -v ;
```

## License

See [LICENSE](LICENSE). This code is published for viewing as a sample of the author's work. All rights reserved.
