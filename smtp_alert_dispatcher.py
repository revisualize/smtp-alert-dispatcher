#!/usr/bin/env python3
"""
smtp_alert_dispatcher.py
Deliver alert email now, or spool to disk and deliver later.

Commands:
    send   --subject S [--body B | body on stdin]
    flush
    status

Exit codes:
    0  send: delivered to every recipient. flush: spool empty after the
       run. status: nothing pending and no dead letters.
    1  send: spooled for retry (all recipients, or the ones the relay
       refused). flush: records still pending. status: records pending.
    2  Configuration or usage error. status also returns 2 when dead
       letters are present.
    3  State not confirmed: a filesystem operation failed partway, so the
       tool cannot say whether the alert is on disk or the spool is in the
       state it reports. Inspect the spool and dead-letter directories
       before retrying.
"""

import argparse
import email.utils
import fcntl
import json
import os
import re
import smtplib
import ssl
import stat
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

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_CONFIGURATION = 2
EXIT_DEAD_LETTERS = 2
EXIT_NOT_CONFIRMED = 3

# Failures of the relay session rather than of one message, when they reach
# flush as raw smtplib exceptions. deliver_message already converts every
# failure before the message is handed over (connect, STARTTLS, AUTH) into
# RelaySessionError; this tuple is the second line, for callers and tests
# that raise the library's classes directly. Unless a class is named here it
# falls into the message-rejection branch, and one shared condition charges
# an attempt against every message in the spool.
CONNECTION_LEVEL_ERRORS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
)

REQUIRED_RECORD_KEYS = ("subject", "body", "attempt_count")

LINE_BREAKS = re.compile(r"[\r\n]+")


class ConfigurationError(ValueError):
    """The configuration cannot be honored as written. Exit 2."""


class RelaySessionError(Exception):
    """The relay session failed for every message, not for this one.

    Raised for a failed connection, STARTTLS refused or unavailable, any
    AUTH failure (including no mechanism in common), credentials configured
    without TLS, a refused sender, and a 421 service-closing reply at any
    stage. flush stops the run without charging an attempt.
    """


class MessageNotSupportedError(smtplib.SMTPException):
    """The relay cannot carry this particular message (for example SMTPUTF8).

    smtplib raises SMTPNotSupportedError both for a session capability
    (STARTTLS missing) and for a property of one message. Inside the
    submission stage it is the second kind, so it is renamed here to keep
    it out of CONNECTION_LEVEL_ERRORS and charge it to the message.
    """


def report(text, stream=None):
    """Print a status line without letting a closed stream change the outcome.

    A cron job with a closed stdout, or a caller that exits before reading,
    turns print into BrokenPipeError. After a delivery that error must not
    send the alert down the failure path and spool it a second time.
    """
    try:
        print(text, file=stream if stream is not None else sys.stdout)
    except (OSError, ValueError):
        pass


def clean_text(text):
    """Replace code points that cannot be encoded as UTF-8 with U+FFFD.

    The only such code points are lone surrogates, which Python produces
    when it decodes bytes that are not UTF-8 with surrogateescape (argv,
    and stdin in some locales). str.encode(errors="replace") would turn
    them into "?", which is indistinguishable from a real question mark,
    so the mapping is explicit.
    """
    return "".join("�" if 0xD800 <= ord(character) <= 0xDFFF else character
                   for character in text)


def clean_header_text(text):
    """Make a header value buildable: line breaks become one space."""
    return clean_text(LINE_BREAKS.sub(" ", text))


def is_header_safe(value):
    """True for a string an email header can carry as given."""
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def is_whole_number(value):
    # type() rather than isinstance(): JSON true is a Python bool, and bool
    # subclasses int, so isinstance(True, int) accepts it as 1.
    return type(value) is int


def validate_configuration(configuration):
    """Reject any value of the wrong type, range, or shape. Exit 2.

    Keys the tool does not know are ignored, so a configuration written for
    a later revision still loads here.
    """
    relay_host = configuration["relay_host"]
    require(isinstance(relay_host, str) and relay_host.strip() == relay_host
            and relay_host != "" and is_header_safe(relay_host),
            "relay_host must be a non-empty host name")
    relay_port = configuration["relay_port"]
    require(is_whole_number(relay_port) and 1 <= relay_port <= 65535,
            "relay_port must be a whole number from 1 to 65535")
    require(type(configuration["use_starttls"]) is bool,
            "use_starttls must be true or false")
    require(is_header_safe(configuration["smtp_username"]),
            "smtp_username must be a single-line string")
    password_file = configuration["smtp_password_file"]
    require(isinstance(password_file, str)
            and (password_file == "" or os.path.isabs(password_file)),
            "smtp_password_file must be empty or an absolute path")
    require(is_header_safe(configuration["sender_address"])
            and configuration["sender_address"].strip() != "",
            "sender_address must be a non-empty single-line address")
    recipients = configuration["recipient_addresses"]
    require(isinstance(recipients, list) and len(recipients) > 0
            and all(is_header_safe(address) and address.strip() != ""
                    for address in recipients),
            "recipient_addresses must be a non-empty list of single-line addresses")
    timeout = configuration["connect_timeout_seconds"]
    require(type(timeout) in (int, float) and timeout > 0,
            "connect_timeout_seconds must be a number greater than 0")
    attempts = configuration["max_delivery_attempts"]
    require(is_whole_number(attempts) and attempts >= 1,
            "max_delivery_attempts must be a whole number of at least 1")
    # Relative paths would resolve against whatever directory cron or the
    # caller started in, so send and flush could use different spools.
    for path_key in ("spool_directory", "dead_letter_directory", "lock_file"):
        path_value = configuration[path_key]
        require(isinstance(path_value, str) and os.path.isabs(path_value),
                f"{path_key} must be an absolute path")


def ensure_private_directory(directory_path):
    """Create every missing component of a directory path with mode 0700.

    Path.mkdir(parents=True, mode=...) applies the mode to the last
    component only; the parents it creates follow the umask. Each component
    is therefore created here, then set to 0700 explicitly, because mkdir's
    mode is itself masked by the umask. Directories that already exist are
    left as they are: the tool does not change modes it did not create.
    """
    missing_components = []
    current_path = directory_path
    while not os.path.lexists(current_path):
        missing_components.append(current_path)
        current_path = current_path.parent
    for component in reversed(missing_components):
        try:
            os.mkdir(component, 0o700)
        except FileExistsError:
            continue
        os.chmod(component, 0o700)
    if not directory_path.is_dir():
        raise ConfigurationError(f"{directory_path} exists and is not a directory")


def load_configuration():
    configuration = dict(DEFAULT_CONFIGURATION)
    configuration["recipient_addresses"] = list(DEFAULT_CONFIGURATION["recipient_addresses"])
    if CONFIGURATION_PATH.exists():
        with CONFIGURATION_PATH.open(encoding="utf-8") as configuration_handle:
            overrides = json.load(configuration_handle)
        if not isinstance(overrides, dict):
            raise ConfigurationError("the configuration file must hold a JSON object")
        configuration.update(overrides)
    validate_configuration(configuration)
    spool_directory = Path(configuration["spool_directory"])
    dead_letter_directory = Path(configuration["dead_letter_directory"])
    ensure_private_directory(spool_directory)
    ensure_private_directory(dead_letter_directory)
    # The lock file's directory is bootstrapped too. On a minimal container
    # /var/lock may not exist, and flush would then fail before it could
    # take the lock.
    ensure_private_directory(Path(configuration["lock_file"]).parent)
    # Retirement is a rename from the spool into the dead-letter directory.
    # The same directory makes retirement a no-op, so a "retired" record is
    # retried forever; a different filesystem makes the rename fail.
    if os.path.samefile(spool_directory, dead_letter_directory):
        raise ConfigurationError("spool_directory and dead_letter_directory must differ")
    if spool_directory.stat().st_dev != dead_letter_directory.stat().st_dev:
        raise ConfigurationError(
            "dead_letter_directory must be on the same filesystem as spool_directory")
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

    The temporary file is opened with O_NOFOLLOW and mode 0600: a symlink
    planted at the fixed temporary name is refused rather than followed,
    and the record's mode does not depend on the caller's umask.
    """
    temporary_path = final_path.with_suffix(".tmp")
    record_descriptor = os.open(
        str(temporary_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600)
    try:
        # An existing temporary file left by a crash keeps its old mode
        # through O_TRUNC, so the mode is set on every write.
        os.fchmod(record_descriptor, 0o600)
        record_handle = os.fdopen(record_descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(record_descriptor)
        raise
    with record_handle:
        record_handle.write(json.dumps(message_record, indent=2))
        record_handle.flush()
        os.fsync(record_handle.fileno())
    os.replace(temporary_path, final_path)
    fsync_directory(final_path.parent)
    return final_path


def check_address_list(record_key, value):
    if not (isinstance(value, list) and len(value) > 0
            and all(is_header_safe(address) and address.strip() != ""
                    for address in value)):
        raise ValueError(f"{record_key} must be a non-empty list of single-line addresses")


def validate_spool_record(message_record):
    """Raise ValueError unless every field the delivery path uses is usable.

    Keys alone are not enough: JSON null, a number, or a list passes a key
    test or fails it with TypeError, which is outside every handler in
    flush. Every type and header value is checked here so that each
    unusable record takes the one dead-letter path.
    """
    if not isinstance(message_record, dict):
        raise ValueError("record is not a JSON object")
    missing_keys = [key for key in REQUIRED_RECORD_KEYS if key not in message_record]
    if missing_keys:
        raise ValueError(f"missing keys: {', '.join(missing_keys)}")
    if not is_header_safe(message_record["subject"]):
        raise ValueError("subject must be a single-line string")
    body = message_record["body"]
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError as encode_error:
        raise ValueError(f"body cannot be encoded: {encode_error}") from encode_error
    attempt_count = message_record["attempt_count"]
    if not is_whole_number(attempt_count) or attempt_count < 0:
        raise ValueError("attempt_count must be a whole number of 0 or more")
    for header_key in ("message_id", "date"):
        if header_key in message_record and not is_header_safe(message_record[header_key]):
            raise ValueError(f"{header_key} must be a single-line string")
    for list_key in ("recipient_addresses", "pending_recipients"):
        if list_key in message_record:
            check_address_list(list_key, message_record[list_key])


def load_spool_record(pending_path):
    """Read one spool record, raising ValueError or OSError if it is unusable.

    O_NOFOLLOW refuses a symlink planted in the spool (it would deliver
    whatever file it points at); O_NONBLOCK and the regular-file check keep
    a FIFO named *.json from blocking the run. The caller dead-letters on
    either exception.
    """
    record_descriptor = os.open(
        str(pending_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(record_descriptor, "rb") as record_handle:
        if not stat.S_ISREG(os.fstat(record_handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        raw_record = record_handle.read()
    try:
        message_record = json.loads(raw_record.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
        raise ValueError(f"unparseable: {parse_error}") from parse_error
    validate_spool_record(message_record)
    return message_record


def build_message_record(subject_text, body_text, attempt_count=1,
                         recipient_addresses=None):
    """Create a record carrying a stable identity across retries.

    message_id and date are generated once, here, and reused on every
    later delivery attempt of this record. Delivery is at-least-once: a
    message can be accepted by the relay and still be retried if the
    connection drops before the spool file is unlinked. With a stable
    Message-ID a client can recognize the retry as the same message; a
    fresh ID per attempt makes each retry look like a new alert during
    exactly the incident the alert exists to report.

    The subject and body are cleaned so the record is always buildable:
    line breaks in the subject become spaces, and code points that cannot
    be encoded as UTF-8 become U+FFFD. An alert with a damaged character is
    delivered; an alert that raises while being built is lost.

    recipient_addresses, when given, fixes who the alert is for at the
    moment it is created, so a configuration change before the retry does
    not send it to someone else.
    """
    message_record = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "message_id": email.utils.make_msgid(),
        "date": email.utils.formatdate(localtime=True),
        "subject": clean_header_text(subject_text),
        "body": clean_text(body_text),
        "attempt_count": attempt_count,
    }
    if recipient_addresses is not None:
        message_record["recipient_addresses"] = list(recipient_addresses)
    return message_record


def header_recipients(configuration, message_record):
    """Everyone the alert is for: the To header."""
    return message_record.get("recipient_addresses") or configuration["recipient_addresses"]


def envelope_recipients(configuration, message_record):
    """Who this attempt delivers to: only the recipients still pending."""
    return message_record.get("pending_recipients") or header_recipients(
        configuration, message_record)


def build_email_message(configuration, message_record):
    """Assemble the message. Raises ValueError if a value cannot be a header.

    Message-ID and Date come from the record rather than being generated
    per attempt, so a redelivery is recognizably the same message. Records
    spooled by an earlier revision carry neither, so both fall back to a
    freshly generated value rather than raising; for those records the
    fallback is new on every attempt.
    """
    outgoing_message = EmailMessage()
    outgoing_message["From"] = configuration["sender_address"]
    outgoing_message["To"] = ", ".join(header_recipients(configuration, message_record))
    outgoing_message["Subject"] = message_record["subject"]
    outgoing_message["Message-ID"] = message_record.get(
        "message_id") or email.utils.make_msgid()
    outgoing_message["Date"] = message_record.get(
        "date") or email.utils.formatdate(localtime=True)
    outgoing_message.set_content(message_record["body"])
    return outgoing_message


def read_smtp_password(configuration):
    password_file = configuration["smtp_password_file"]
    if not password_file:
        return ""
    return Path(password_file).read_text(encoding="utf-8").strip()


def open_relay_session(configuration, smtp_connection):
    """STARTTLS and AUTH. Every failure here is a RelaySessionError.

    Credentials are only ever sent after STARTTLS succeeds. A username with
    use_starttls false is refused here, at delivery time, rather than at
    configuration load, so send still spools the alert and status still
    reports; the refusal message says what to change.
    """
    try:
        if configuration["use_starttls"]:
            smtp_connection.starttls(context=ssl.create_default_context())
        if configuration["smtp_username"]:
            if not configuration["use_starttls"]:
                raise RelaySessionError(
                    "smtp_username is set and use_starttls is false; "
                    "credentials are only sent over TLS")
            smtp_connection.login(configuration["smtp_username"],
                                  read_smtp_password(configuration))
    except RelaySessionError:
        raise
    except (smtplib.SMTPException, OSError, ValueError) as session_error:
        # ValueError covers a password file that is not valid UTF-8.
        raise RelaySessionError(f"relay session setup failed: {session_error}") from session_error


def close_relay_session(smtp_connection):
    """QUIT, ignoring the reply. Its outcome never changes a delivery result.

    smtplib.SMTP.__exit__ raises when QUIT gets anything but 221. After the
    relay has accepted the message that exception would send a delivered
    alert down the failure path and spool it again.
    """
    try:
        smtp_connection.quit()
    except (smtplib.SMTPException, OSError):
        try:
            smtp_connection.close()
        except OSError:
            pass


def deliver_message(configuration, message_record):
    """Attempt one SMTP delivery.

    Returns the dict of recipients the relay refused while accepting the
    others (empty when all accepted). Raises RelaySessionError for a failure
    of the session, smtplib.SMTPException for a rejection of this message,
    and OSError for a socket failure partway through.
    """
    outgoing_message = build_email_message(configuration, message_record)
    try:
        smtp_connection = smtplib.SMTP(
            configuration["relay_host"],
            configuration["relay_port"],
            timeout=configuration["connect_timeout_seconds"],
        )
    except (smtplib.SMTPException, OSError) as connect_error:
        raise RelaySessionError(f"cannot reach relay: {connect_error}") from connect_error
    try:
        open_relay_session(configuration, smtp_connection)
        try:
            refused_recipients = smtp_connection.send_message(
                outgoing_message,
                to_addrs=envelope_recipients(configuration, message_record))
        except smtplib.SMTPSenderRefused as sender_error:
            # The sender is configuration, shared by every queued message.
            raise RelaySessionError(f"relay refused the sender: {sender_error}") from sender_error
        except smtplib.SMTPRecipientsRefused as refused_error:
            refusal_codes = [code for code, _ in refused_error.recipients.values()]
            if refusal_codes and all(code == 421 for code in refusal_codes):
                raise RelaySessionError(f"relay closing: {refused_error}") from refused_error
            raise
        except smtplib.SMTPNotSupportedError as unsupported_error:
            raise MessageNotSupportedError(str(unsupported_error)) from unsupported_error
        except smtplib.SMTPResponseException as response_error:
            # 421 is "service not available, closing channel" (RFC 5321,
            # 4.2.3) at any stage: a property of the relay, not the message.
            if response_error.smtp_code == 421:
                raise RelaySessionError(f"relay closing: {response_error}") from response_error
            raise
        except smtplib.SMTPServerDisconnected as disconnected_error:
            raise RelaySessionError(f"relay disconnected: {disconnected_error}") from disconnected_error
        return refused_recipients or {}
    finally:
        close_relay_session(smtp_connection)


def spool_message(configuration, message_record):
    """Persist a prepared record atomically."""
    spool_directory = Path(configuration["spool_directory"])
    final_path = spool_directory / f"{int(time.time())}_{uuid.uuid4().hex}.json"
    return write_record_atomically(final_path, message_record)


def spool_for_retry(configuration, message_record, reason):
    """Spool after a failed or partial delivery. Exit 1, or 3 if the write failed."""
    try:
        spooled_path = spool_message(configuration, message_record)
    except OSError as spool_error:
        report(f"not confirmed: delivery failed ({reason}) and the spool write "
               f"failed ({spool_error}); the alert may not be on disk",
               sys.stderr)
        return EXIT_NOT_CONFIRMED
    report(f"spooled: {reason} -> {spooled_path.name}", sys.stderr)
    return EXIT_PENDING


def read_body(arguments):
    if arguments.body is not None:
        return arguments.body
    if sys.stdin.isatty():
        return None
    # Read bytes and decode explicitly, so a byte that is not UTF-8 becomes
    # U+FFFD instead of a decode error or a surrogate that fails later.
    standard_input_buffer = getattr(sys.stdin, "buffer", None)
    if standard_input_buffer is None:
        return sys.stdin.read()
    return standard_input_buffer.read().decode("utf-8", errors="replace")


def command_send(configuration, arguments):
    body_text = read_body(arguments)
    if body_text is None:
        # Reading a tty here blocks until the operator types an end of file,
        # which looks like a hang rather than a usage error to someone
        # testing the command by hand during an incident.
        report("no body: pass --body or pipe the body on stdin", sys.stderr)
        return EXIT_CONFIGURATION

    message_record = build_message_record(
        arguments.subject, body_text,
        recipient_addresses=configuration["recipient_addresses"])
    try:
        refused_recipients = deliver_message(configuration, message_record)
    except Exception as delivery_error:
        # Deliberately broad. Whatever stopped delivery (an SMTP reply, a
        # socket, a password file that will not decode, a value that cannot
        # be a header), the alert exists only in this process until it is
        # on disk. Keeping it is the tool's one job; classifying the failure
        # is flush's job on the next run.
        return spool_for_retry(configuration, message_record,
                               f"{type(delivery_error).__name__}: {delivery_error}")
    if refused_recipients:
        message_record["pending_recipients"] = sorted(refused_recipients)
        refused_text = ", ".join(f"{address} {code}" for address, (code, _)
                                 in sorted(refused_recipients.items()))
        return spool_for_retry(configuration, message_record,
                               f"relay refused {refused_text}; others delivered")
    report("delivered")
    return EXIT_OK


def count_records(directory_path):
    return len(list(Path(directory_path).glob("*.json")))


def retire_record(pending_path, dead_letter_directory):
    """Move a record to dead letters without replacing an existing one.

    os.replace overwrites its destination, so a dead letter with the same
    name (a restored record, an operator's copy) would be destroyed. The
    destination is made unique instead. Flush holds the lock, so no other
    flush is choosing a name at the same moment.
    """
    destination_path = dead_letter_directory / pending_path.name
    while os.path.lexists(destination_path):
        destination_path = dead_letter_directory / (
            f"{pending_path.stem}_{uuid.uuid4().hex[:8]}.json")
    os.replace(pending_path, destination_path)
    fsync_directory(dead_letter_directory)
    fsync_directory(pending_path.parent)
    return destination_path


def charge_attempt(configuration, pending_path, message_record, reason):
    """Count one failed attempt; retire the record at the ceiling."""
    message_record["attempt_count"] += 1
    # Same atomic path as the initial spool. Writing in place here would
    # mean a power loss mid-update truncates a live spool file and loses
    # the message outright.
    write_record_atomically(pending_path, message_record)
    if message_record["attempt_count"] >= configuration["max_delivery_attempts"]:
        retired_path = retire_record(pending_path, Path(configuration["dead_letter_directory"]))
        report(f"dead lettered after {message_record['attempt_count']} attempts: "
               f"{retired_path.name}: {reason}", sys.stderr)
    else:
        report(f"attempt {message_record['attempt_count']} failed: "
               f"{pending_path.name}: {reason}", sys.stderr)


def flush_spool(configuration):
    spool_directory = Path(configuration["spool_directory"])
    dead_letter_directory = Path(configuration["dead_letter_directory"])
    for pending_path in sorted(spool_directory.glob("*.json")):
        try:
            message_record = load_spool_record(pending_path)
            build_email_message(configuration, message_record)
        except (ValueError, OSError) as record_error:
            # Every unusable record, whatever made it unusable, takes this
            # one path. One poison record must not end the run and strand
            # every message behind it in sort order.
            retired_path = retire_record(pending_path, dead_letter_directory)
            report(f"unusable spool record, dead lettered: "
                   f"{retired_path.name}: {record_error}", sys.stderr)
            continue
        try:
            refused_recipients = deliver_message(configuration, message_record)
        except SESSION_LEVEL_ERRORS as session_error:
            # Failure of the relay session, not of this message: one
            # condition affecting every queued message, so stop the run
            # rather than charging an attempt against all of them.
            report(f"relay session failed, stopping flush: {session_error}", sys.stderr)
            break
        except smtplib.SMTPException as rejection_error:
            # Message-level rejection: charge this message, continue the run.
            # Ordering note: SMTPException subclasses OSError since Python
            # 3.4, so the bare OSError handler below MUST come after this
            # one or this branch is unreachable and nothing ever dead
            # letters.
            charge_attempt(configuration, pending_path, message_record,
                           f"rejected: {rejection_error}")
            continue
        except OSError as connection_error:
            # Socket-level failure partway through: stop.
            report(f"relay unreachable, stopping flush: {connection_error}", sys.stderr)
            break
        if refused_recipients:
            message_record["pending_recipients"] = sorted(refused_recipients)
            charge_attempt(configuration, pending_path, message_record,
                           f"relay refused {', '.join(sorted(refused_recipients))}; "
                           f"others delivered")
            continue
        try:
            os.unlink(pending_path)
        except OSError as removal_error:
            # Delivered, but the record is still on disk and the next flush
            # will deliver it again. That is a filesystem problem, not a
            # relay problem, and it is reported as one.
            report(f"delivered, but could not remove spool record {pending_path.name}: "
                   f"{removal_error}; the next flush will send a duplicate", sys.stderr)
            return EXIT_NOT_CONFIRMED
        report(f"delivered from spool: {pending_path.name}")
    return EXIT_PENDING if count_records(spool_directory) else EXIT_OK


SESSION_LEVEL_ERRORS = (RelaySessionError,) + CONNECTION_LEVEL_ERRORS


def open_lock_file(lock_path):
    """Open the lock without following a symlink and without truncating it."""
    return os.open(
        str(lock_path),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600)


def command_flush(configuration):
    lock_descriptor = open_lock_file(configuration["lock_file"])
    lock_acquired = False
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another flush holds the lock. Report the spool as it stands,
            # so 0 still means "nothing pending" to whatever reads the code.
            report("another flush is running", sys.stderr)
            if count_records(configuration["spool_directory"]):
                return EXIT_PENDING
            return EXIT_OK
        lock_acquired = True
        return flush_spool(configuration)
    finally:
        # Released and closed on every path, including an exception from
        # inside the run. Closing also releases the lock, so the close runs
        # even if the explicit unlock raises.
        try:
            if lock_acquired:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def command_status(configuration):
    pending_count = count_records(configuration["spool_directory"])
    dead_count = count_records(configuration["dead_letter_directory"])
    report(f"pending: {pending_count}  dead_letters: {dead_count}")
    if dead_count:
        return EXIT_DEAD_LETTERS
    return EXIT_PENDING if pending_count else EXIT_OK


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__,
                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommand_parsers = argument_parser.add_subparsers(dest="command", required=True)
    send_parser = subcommand_parsers.add_parser("send")
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body")
    subcommand_parsers.add_parser("flush")
    subcommand_parsers.add_parser("status")
    arguments = argument_parser.parse_args()

    try:
        configuration = load_configuration()
    except (OSError, ValueError) as configuration_error:
        report(f"configuration error: {configuration_error}", sys.stderr)
        return EXIT_CONFIGURATION

    try:
        if arguments.command == "send":
            return command_send(configuration, arguments)
        if arguments.command == "flush":
            return command_flush(configuration)
        return command_status(configuration)
    except OSError as state_error:
        # A filesystem operation failed partway (a full disk, an fsync after
        # a rename, a symlink at the lock path). What is on disk may differ
        # from what the run was about to report, so say exactly that.
        report(f"not confirmed: {arguments.command} stopped on a filesystem error: "
               f"{state_error}; inspect the spool and dead-letter directories "
               f"before retrying", sys.stderr)
        return EXIT_NOT_CONFIRMED


if __name__ == "__main__":
    sys.exit(main())
