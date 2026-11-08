# smtp-alert-dispatcher

[![ci](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/ci.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/ci.yml)
[![test](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/test.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/test.yml)
[![shellcheck](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/shellcheck.yml)
[![python-compat](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/python-compat.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/python-compat.yml)
[![python-lint](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/python-lint.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/python-lint.yml)
[![codeql](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/codeql.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/codeql.yml)
[![markdown-lint](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/markdown-lint.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/markdown-lint.yml)
[![links](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/links.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/links.yml)
[![content-policy](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/content-policy.yml/badge.svg)](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/content-policy.yml)
[![license: all rights reserved](https://img.shields.io/badge/license-all%20rights%20reserved-lightgrey)](LICENSE)

**Project page:** [revisualized.com/projects/smtp-alert-dispatcher](https://revisualized.com/projects/smtp-alert-dispatcher)

Alert email delivery for hosts with no mail agent. A caller hands it a subject and a body. It delivers to the relay over STARTTLS, and when the relay is unreachable or refuses some recipients it writes the message to a disk spool and retries on the next flush.

The design holds one promise: for every recipient, the message is delivered or it is on disk, and the exit code says which. A degraded network is a common source of alerts and exactly the condition under which a fire-and-forget sender loses them. The one window the promise does not cover is a process killed after the relay accepted part of a message and before the spool write finished (see Known limitations).

## Usage

```sh
printf '%s\n' "$alert_body" | smtp_alert_dispatcher.py send \
    --subject "connectivity failures on ${target_host}" ;
```

Exit code 1 from `send` means spooled, not lost. A caller that treats 1 as failure may page twice for one event. Exit code 3 means the tool could not confirm the state it would report (see Exit codes).

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
| `smtp_username` | `""` | Empty disables authentication. Credentials are only sent after STARTTLS: a username with `use_starttls` false is refused at delivery time, and the alert is spooled |
| `smtp_password_file` | `""` | Absolute path, read as UTF-8 at send time; the password itself is never a config value |
| `sender_address` | `alerts@example.net` | |
| `recipient_addresses` | `["storage-alerts@example.net"]` | |
| `connect_timeout_seconds` | `10` | |
| `max_delivery_attempts` | `20` | Attempts before a message is retired to dead letters. The failed `send` counts as attempt 1 |
| `spool_directory` | `/var/spool/smtp_alert_dispatcher/pending` | Absolute path |
| `dead_letter_directory` | `/var/spool/smtp_alert_dispatcher/dead_letters` | Absolute path, a different directory on the same filesystem as the spool (retirement is a rename) |
| `lock_file` | `/var/lock/smtp_alert_dispatcher.lock` | Absolute path |

Every value is checked for type and range when the file is loaded: `use_starttls` must be `true` or `false`, ports and attempt counts must be whole numbers, `connect_timeout_seconds` may be fractional, and addresses must be single-line strings. A value that fails the check is a configuration error, exit 2. Keys the tool does not know are ignored.

## Exit codes

| Code | `send` | `flush` | `status` |
|------|--------|---------|----------|
| 0 | Delivered to every recipient | Spool empty after the run, or another flush is running and the spool is empty | Nothing pending, no dead letters |
| 1 | Spooled for retry: all recipients, or the ones the relay refused | Messages still pending, including when another flush is running | Messages pending |
| 2 | Configuration error, or usage error (no body, bad arguments) | Configuration or usage error | Dead letters present, or a configuration error |
| 3 | Not confirmed: delivery failed and the spool write failed, so the alert may not be on disk | Not confirmed: a filesystem operation failed partway, or a delivered record could not be removed | Not confirmed: the directories could not be read |

Exit 3 never means "nothing happened". A spool write can fail after the rename that made the record visible, so inspect the spool and dead-letter directories before retrying.

## Design notes

**A failure to deliver is not a failure to alert.** `send` returns 1 when it spools, which is a distinct outcome from losing the message, and the exit codes are arranged so a caller can tell them apart.

**Every spool record write goes through one atomic path.** Write to a temporary name the spool glob does not match, fsync the contents, rename over the target, then fsync the directory. Both the initial spool and the attempt-count update during a flush use it. An earlier revision updated the attempt count with a direct write to the live file, which meant a power loss partway through that write truncated a spooled message and lost it outright. The fsync steps matter because rename is atomic in the POSIX namespace but says nothing about data reaching disk; without them a power loss can leave a zero-length record behind a successfully renamed name. Records and the lock file are created with mode 0600 and `O_NOFOLLOW`, and directories the tool creates (parents included) get 0700, whatever the umask. A record in the spool that is a symlink, not a regular file, or not valid is dead-lettered unread.

**Delivery is tracked per recipient.** The relay can accept a message for some recipients and refuse others in the same transaction; it reports that in the return value, not as an error. A record stores the recipients the alert was created for and, after a partial refusal, only the ones still pending. A retry goes to those recipients alone, so a recipient that accepted does not get a duplicate from a retry, and the alert follows the recipients it was created for even if the configuration changes. Every refusal, temporary (4xx) or permanent (5xx), is retried until `max_delivery_attempts`.

**A retry is the same message, not a new one.** Delivery is at-least-once: the relay can accept a message and the spool file still survive if the connection drops before the unlink. `Message-ID` and `Date` are generated once when the alert is created and reused on every later attempt, so a duplicate arrives as a recognisable duplicate that a client can thread or collapse rather than as another unread alert during the incident. Records spooled by a revision older than this behavior carry neither header, and for those a fresh value is generated on each attempt.

**Flush sorts failures by the stage that failed.** An unreachable relay is one condition affecting every queued message, so the run stops rather than burning an attempt on all of them. A rejection of the message itself (refused recipients, a refused body, a message the relay cannot carry) is charged to that message and the run continues. Between those sits the relay session: a refused or unavailable STARTTLS, any AUTH failure including no mechanism in common, a refused sender address, and a 421 "closing" reply at any stage all stop the run without charging anyone, because each is the same for every message. Unless they are classified by stage, a single stale credential charges an attempt against every message in the spool, and at the default ceiling that retires an entire spool to dead letters in about ninety-five minutes. A QUIT reply after the relay accepted the message does not change the result.

**Handler order in the flush loop is load-bearing.** `smtplib.SMTPException` subclasses `OSError`. If the bare `OSError` branch is placed first, every message-level rejection is reclassified as a connection failure, `attempt_count` never rises, and nothing ever reaches the dead-letter directory. A permanently undeliverable message then retries forever with no operator signal. The test suite fails if that ordering regresses, and CI reintroduces that defect and four others on every run to confirm the tests still catch them.

**Retirement is visible, not silent.** A message that exhausts `max_delivery_attempts` moves to the dead-letter directory rather than being deleted, and `status` returns 2 while any remain. A dead letter with the same name is never replaced; the moved record gets a unique name. Dead letters outrank pending messages in the `status` exit code, because a retired message is the louder signal. `flush` reports only the spool.

**Concurrent flushes are serialized by an advisory lock.** A second flush arriving while the first is running declines and reports the spool as it stands: 1 if records are pending, 0 if not. The lock binds only processes that take it; `send` does not, and a record it spools during a flush waits for the next run. Two flush runs over one spool would deliver the same message twice.

## Known limitations

- Delivery is attempted at most once per flush per message, and a run that stops on a relay failure leaves the rest for the next run. There is no backoff, so retry timing is entirely a function of how often the flush is scheduled.
- `send` delivers first and spools on failure. A process killed after the relay accepted some recipients and before the spool write completed loses the refused recipients; a process killed after a full acceptance and before it exits may report nothing. Writing the record before the first attempt would close that window and is not done in this revision.
- The configured directories and their parents are trusted. The tool creates missing ones privately but does not change or verify the owner and mode of directories that already exist; they should be owned by the user that runs the tool and not writable by anyone else.
- The spool is a flat directory scanned with a glob. It is sized for alert volumes, not for a queue.
- `fsync` on every spool write costs an I/O round trip per message. At alert volumes that is irrelevant; at queue volumes it would not be.
- Threshold and deduplication logic is deliberately absent. Deciding what is worth alerting on belongs to the caller.

## Requirements

Python 3.9 or newer. Standard library only, no third-party packages. Linux or another POSIX system with `flock` and `O_NOFOLLOW`. Write access to the spool, dead-letter, and lock directories, which by default are under `/var`, so the default configuration runs as root.

## Tests

```sh
python3 -m unittest discover -s test -v ;
```

## Continuous integration

Each badge above is its own GitHub Actions workflow in `.github/workflows/`. Every workflow runs on each push and pull request, can be re-run by hand from the Actions tab, and links to its run history.

| Workflow | A green badge means |
|---|---|
| [`ci`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/ci.yml) | `bash test/run_all_tests.sh` passed on Python 3.9 and 3.12 and reported a non-zero count of executed tests, and shellcheck found nothing at style severity. |
| [`test`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/test.yml) | `bash test/run_all_tests.sh` passed on Python 3.9 and 3.12. The run fails if any suite fails or if zero tests executed, and the job summary lists each suite with its test count. |
| [`shellcheck`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/shellcheck.yml) | Every shell script outside `test/fixtures/` parses with `bash -n` and has no shellcheck findings at style severity. |
| [`python-compat`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/python-compat.yml) | The test suite passed under every Python release from 3.9 through 3.14. |
| [`python-lint`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/python-lint.yml) | ruff found no defects (unused imports, undefined names, and similar) in any Python module. |
| [`codeql`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/codeql.yml) | GitHub CodeQL security analysis of the Python code reported no results. |
| [`markdown-lint`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/markdown-lint.yml) | Every Markdown file passes markdownlint. |
| [`links`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/links.yml) | Every link in every Markdown file resolved on the latest run. It also runs weekly, because a link can break with no commit here. |
| [`content-policy`](https://github.com/revisualize/smtp-alert-dispatcher/actions/workflows/content-policy.yml) | Every tracked file meets the publishing rules: UTF-8, LF line endings, no em dashes, scripts documented as `bash name.sh`, and vendor-neutral wording. |

A badge reports the latest run of those checks. What the tool needs on your own host is listed under Requirements.

## License

See [LICENSE](LICENSE). All rights reserved.
