#!/usr/bin/env python3
"""
smtp_alert_dispatcher.py
Deliver alert email now, or spool to disk and deliver later. Never neither.

Commands:
    send   --subject S [--body B | body on stdin]
    flush
    status

Exit codes: 0 delivered (or flush/status clean), 1 spooled for retry or
spool non-empty, 2 configuration error or dead letters present.
"""

import argparse
import email.utils
import fcntl
import json
import os
import smtplib
import ssl
import sys
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

CONFIGURATION_PATH = Path("/etc/smtp_alert_dispatcher/config.json")

DEFAULT_CONFIGURATION = {
    "relay_host": "mailrelay.example.net",
    "relay_port": 587,
    "use_starttls": True,
    "smtp_username": "",
    "smtp_password_file": "",
    "sender_address": "alerts@example.net",
    "recipient_addresses": ["storage-alerts@example.net"],
    "connect_timeout_seconds": 10,
    "max_delivery_attempts": 20,
    "spool_directory": "/var/spool/smtp_alert_dispatcher/pending",
    "dead_letter_directory": "/var/spool/smtp_alert_dispatcher/dead_letters",
    "lock_file": "/var/lock/smtp_alert_dispatcher.lock",
}

# Failures of the relay session rather than of one message. Authentication,
# HELO and STARTTLS refusals all subclass SMTPException, so unless they are
# named here they fall into the message-rejection branch below and one wrong
# credential charges an attempt against every message in the spool.
CONNECTION_LEVEL_ERRORS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPNotSupportedError,
)

REQUIRED_RECORD_KEYS = ("subject", "body", "attempt_count")


def load_configuration():
    configuration = dict(DEFAULT_CONFIGURATION)
    if CONFIGURATION_PATH.exists():
        with CONFIGURATION_PATH.open() as configuration_handle:
            configuration.update(json.load(configuration_handle))
    for directory_key in ("spool_directory", "dead_letter_directory"):
        Path(configuration[directory_key]).mkdir(parents=True, exist_ok=True)
    # The lock file's directory is bootstrapped too. On a minimal container
    # /var/lock may not exist, and flush would then raise before it could
    # acquire the lock, which is a failure with no spool and no signal.
    Path(configuration["lock_file"]).parent.mkdir(parents=True, exist_ok=True)
    return configuration


def fsync_directory(directory_path):
    """Force a directory entry to disk so a rename survives power loss.

    rename() is atomic in the POSIX namespace, but atomicity is not
    durability. Without this the kernel may have the new name only in the
    page cache, and a power loss can leave the entry missing or the file
    zero length after reboot.
    """
    directory_descriptor = os.open(str(directory_path), os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def write_record_atomically(final_path, message_record):
    """Write a spool record so a reader never sees a partial file.

    Write to a temporary name the *.json glob does not match, fsync the
    file contents, rename over the target, then fsync the directory.
    os.replace rather than Path.rename because this is also the update
    path, and the destination already exists when an attempt is charged.
    """
    temporary_path = final_path.with_suffix(".tmp")
    with open(temporary_path, "w") as record_handle:
        record_handle.write(json.dumps(message_record, indent=2))
        record_handle.flush()
        os.fsync(record_handle.fileno())
    os.replace(temporary_path, final_path)
    fsync_directory(final_path.parent)
    return final_path


def load_spool_record(pending_path):
    """Read one spool record, raising ValueError if it cannot be delivered.

    Checking the keys here rather than letting deliver_message raise KeyError
    keeps every unusable-record failure in one place. A record that parses
    and a record that parses into the wrong shape fail the run identically.
    """
    try:
        message_record = json.loads(pending_path.read_text())
    except json.JSONDecodeError as parse_error:
        raise ValueError(f"unparseable: {parse_error}") from parse_error
    missing_keys = [key for key in REQUIRED_RECORD_KEYS if key not in message_record]
    if missing_keys:
        raise ValueError(f"missing keys: {', '.join(missing_keys)}")
    return message_record


def build_message_record(subject_text, body_text, attempt_count=1):
    """Create a record carrying a stable identity across retries.

    message_id and date are generated once, here, and reused on every
    later delivery attempt. Delivery is at-least-once: a message can be
    accepted by the relay and still be retried if the connection drops
    before the spool file is unlinked. Without a stable Message-ID each
    retry arrives as a distinct message that no client can thread or
    collapse, so a retry storm becomes inbox noise during exactly the
    incident the alert exists to report.
    """
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "message_id": email.utils.make_msgid(),
        "date": email.utils.formatdate(localtime=True),
        "subject": subject_text,
        "body": body_text,
        "attempt_count": attempt_count,
    }


def read_smtp_password(configuration):
    password_file = configuration["smtp_password_file"]
    if not password_file:
        return ""
    return Path(password_file).read_text().strip()


def deliver_message(configuration, message_record):
    """Attempt one SMTP delivery. Raises on any failure; caller spools.

    Message-ID and Date come from the record rather than being generated
    per attempt, so a redelivery is recognisably the same message. Records
    spooled by an earlier version carry neither, so both fall back to a
    freshly generated value rather than raising.
    """
    outgoing_message = EmailMessage()
    outgoing_message["From"] = configuration["sender_address"]
    outgoing_message["To"] = ", ".join(configuration["recipient_addresses"])
    outgoing_message["Subject"] = message_record["subject"]
    outgoing_message["Message-ID"] = message_record.get(
        "message_id") or email.utils.make_msgid()
    outgoing_message["Date"] = message_record.get(
        "date") or email.utils.formatdate(localtime=True)
    outgoing_message.set_content(message_record["body"])

    with smtplib.SMTP(
        configuration["relay_host"],
        configuration["relay_port"],
        timeout=configuration["connect_timeout_seconds"],
    ) as smtp_connection:
        if configuration["use_starttls"]:
            smtp_connection.starttls(context=ssl.create_default_context())
        if configuration["smtp_username"]:
            smtp_connection.login(
                configuration["smtp_username"],
                read_smtp_password(configuration),
            )
        smtp_connection.send_message(outgoing_message)


def spool_message(configuration, message_record):
    """Persist a prepared record atomically."""
    spool_directory = Path(configuration["spool_directory"])
    final_path = spool_directory / f"{int(time.time())}_{uuid.uuid4().hex}.json"
    return write_record_atomically(final_path, message_record)


def command_send(configuration, arguments):
    if arguments.body is not None:
        body_text = arguments.body
    elif not sys.stdin.isatty():
        body_text = sys.stdin.read()
    else:
        # Reading a tty here waits forever and looks like a hang rather
        # than a usage error, which is the wrong thing to do to someone
        # testing the command by hand during an incident.
        print("no body: pass --body or pipe the body on stdin",
              file=sys.stderr)
        return 2

    message_record = build_message_record(arguments.subject, body_text)
    try:
        deliver_message(configuration, message_record)
        print("delivered")
        return 0
    except (smtplib.SMTPException, OSError) as delivery_error:
        spooled_path = spool_message(configuration, message_record)
        print(f"spooled: {delivery_error} -> {spooled_path.name}", file=sys.stderr)
        return 1


def command_flush(configuration):
    # Append rather than write: "w" truncates the lock file on every run,
    # which is a pointless mutation of a file that exists only to be held.
    lock_handle = open(configuration["lock_file"], "a")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        print("another flush is running", file=sys.stderr)
        return 0

    pending_paths = sorted(Path(configuration["spool_directory"]).glob("*.json"))
    dead_letter_directory = Path(configuration["dead_letter_directory"])
    for pending_path in pending_paths:
        try:
            message_record = load_spool_record(pending_path)
        except (ValueError, OSError) as record_error:
            # This read sat outside the try below until this revision. One
            # unusable record then raised through the loop, killed the run,
            # and stranded every message behind it in sort order, while the
            # process exited 1, which is the code for an ordinary non-empty
            # spool. A poisoned spool and a busy spool looked identical.
            os.replace(pending_path, dead_letter_directory / pending_path.name)
            fsync_directory(dead_letter_directory)
            fsync_directory(pending_path.parent)
            print(f"unusable spool record, dead lettered: "
                  f"{pending_path.name}: {record_error}", file=sys.stderr)
            continue
        try:
            deliver_message(configuration, message_record)
            pending_path.unlink()
            print(f"delivered from spool: {pending_path.name}")
        except CONNECTION_LEVEL_ERRORS as session_error:
            # Failure of the relay session, not of this message: one
            # condition affecting every queued message, so stop the run
            # rather than charging an attempt against all of them.
            print(f"relay session failed, stopping flush: {session_error}",
                  file=sys.stderr)
            break
        except smtplib.SMTPException:
            # Message-level rejection: charge this message, continue the run.
            # Ordering note: SMTPException subclasses OSError since Python
            # 3.4, so the bare OSError handler below MUST come after this
            # one or this branch is unreachable and nothing ever dead
            # letters. Found by the functional gate, not by review.
            message_record["attempt_count"] += 1
            # Same atomic path as the initial spool. Writing in place here
            # would mean a power loss mid-update truncates a live spool
            # file and loses the message outright, which is the one
            # outcome this tool exists to prevent.
            write_record_atomically(pending_path, message_record)
            if message_record["attempt_count"] >= configuration["max_delivery_attempts"]:
                dead_path = dead_letter_directory / pending_path.name
                os.replace(pending_path, dead_path)
                fsync_directory(dead_path.parent)
                fsync_directory(pending_path.parent)
                print(f"dead lettered after {message_record['attempt_count']} "
                      f"attempts: {pending_path.name}", file=sys.stderr)
        except OSError as connection_error:
            # Socket-level failure (refused, timeout, unreachable): stop.
            print(f"relay unreachable, stopping flush: {connection_error}",
                  file=sys.stderr)
            break
    remaining_count = len(list(Path(configuration["spool_directory"]).glob("*.json")))
    fcntl.flock(lock_handle, fcntl.LOCK_UN)
    lock_handle.close()
    return 1 if remaining_count else 0


def command_status(configuration):
    pending_count = len(list(Path(configuration["spool_directory"]).glob("*.json")))
    dead_count = len(list(Path(configuration["dead_letter_directory"]).glob("*.json")))
    print(f"pending: {pending_count}  dead_letters: {dead_count}")
    if dead_count:
        return 2
    return 1 if pending_count else 0


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    subcommand_parsers = argument_parser.add_subparsers(dest="command", required=True)
    send_parser = subcommand_parsers.add_parser("send")
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body")
    subcommand_parsers.add_parser("flush")
    subcommand_parsers.add_parser("status")
    arguments = argument_parser.parse_args()

    try:
        configuration = load_configuration()
    except (OSError, json.JSONDecodeError) as configuration_error:
        print(f"configuration error: {configuration_error}", file=sys.stderr)
        return 2

    if arguments.command == "send":
        return command_send(configuration, arguments)
    if arguments.command == "flush":
        return command_flush(configuration)
    return command_status(configuration)


if __name__ == "__main__":
    sys.exit(main())
