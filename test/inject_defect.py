#!/usr/bin/env python3
"""
inject_defect.py

Reintroduces one known defect into smtp_alert_dispatcher.py so CI can
confirm the test suite still fails when it should.

A suite that has only ever run against correct code has been demonstrated,
not tested. Four of the defects below were real: two shipped and were
caught in review, one shipped and was caught by the suite itself, and one
shipped and was caught by driving the flush loop against a poisoned spool.

Injected one at a time, never together. Injecting them together lets one
mask another. The inverted handler order makes the message-level rejection
branch unreachable, so an in-place write inside that branch never executes
and its tests pass against broken code.

Usage:
    python3 test/inject_defect.py handler_order
    python3 test/inject_defect.py --list
"""

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "smtp_alert_dispatcher.py"

ATOMIC_CALL = "            write_record_atomically(pending_path, message_record)"
IN_PLACE_CALL = (
    "            pending_path.write_text(json.dumps(message_record, indent=2))"
)

STABLE_IDENTITY = (
    '    outgoing_message["Message-ID"] = message_record.get(\n'
    '        "message_id") or email.utils.make_msgid()'
)
PER_ATTEMPT_IDENTITY = '    outgoing_message["Message-ID"] = email.utils.make_msgid()'

GUARDED_LOAD_START = "        try:\n            message_record = load_spool_record(pending_path)"
UNGUARDED_LOAD = "        message_record = json.loads(pending_path.read_text())\n        try:"


def invert_handler_order(source):
    """SMTPException subclasses OSError, so order decides reachability.

    With the broad handler first, every message-level rejection is
    reclassified as a connection failure. attempt_count never rises and
    nothing ever reaches the dead-letter directory.
    """
    smtp_start = source.index("        except smtplib.SMTPException:")
    oserror_start = source.index("        except OSError as connection_error:")
    tail_start = source.index("    remaining_count =")
    smtp_block = source[smtp_start:oserror_start]
    oserror_block = source[oserror_start:tail_start]
    return source.replace(smtp_block + oserror_block, oserror_block + smtp_block)


def use_in_place_write(source):
    """Update the attempt count by writing over the live spool file.

    A power loss partway through that write truncates a spooled message
    and loses it, which is the outcome the tool exists to prevent.
    """
    return source.replace(ATOMIC_CALL, IN_PLACE_CALL)


def regenerate_identity_per_attempt(source):
    """Generate a fresh Message-ID on every delivery attempt.

    Delivery is at-least-once, so a retry after a lost acknowledgment
    then arrives as a distinct message no client can thread or collapse.
    """
    return source.replace(STABLE_IDENTITY, PER_ATTEMPT_IDENTITY)


def narrow_session_errors(source):
    """Classify every SMTPException below the connection tuple as a rejection.

    Authentication, HELO and STARTTLS refusals subclass SMTPException, so
    dropping them from the tuple charges an attempt against every message
    in the spool for one condition that belongs to the session. At the
    default ceiling a single wrong password retires the whole spool.
    """
    narrowed = (
        "CONNECTION_LEVEL_ERRORS = (\n"
        "    smtplib.SMTPServerDisconnected,\n"
        "    smtplib.SMTPConnectError,\n"
        ")"
    )
    original_start = source.index("CONNECTION_LEVEL_ERRORS = (")
    original_end = source.index(")", source.index("smtplib.SMTPNotSupportedError,")) + 1
    return source[:original_start] + narrowed + source[original_end:]


def unguard_spool_read(source):
    """Read the spool record outside the try, as the first revision did.

    One unusable record then raises through the loop, ends the run, and
    strands every message behind it in sort order. The process exits 1,
    which is the code for an ordinary non-empty spool, so a poisoned
    spool and a busy spool are indistinguishable from outside.
    """
    guard_start = source.index(GUARDED_LOAD_START)
    body_start = source.index("        try:\n            deliver_message(")
    return source[:guard_start] + UNGUARDED_LOAD + source[body_start + len("        try:"):]


DEFECTS = {
    "handler_order": invert_handler_order,
    "in_place_write": use_in_place_write,
    "per_attempt_identity": regenerate_identity_per_attempt,
    "narrow_session_errors": narrow_session_errors,
    "unguarded_spool_read": unguard_spool_read,
}


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    if argv[1] == "--list":
        for name in DEFECTS:
            print(name)
        return 0
    name = argv[1]
    if name not in DEFECTS:
        print(f"unknown defect: {name}", file=sys.stderr)
        print(f"known: {', '.join(DEFECTS)}", file=sys.stderr)
        return 2

    source = SOURCE.read_text()
    injected = DEFECTS[name](source)
    if injected == source:
        # A silent no-op would let CI report a pass it never earned.
        print(f"injection '{name}' changed nothing; the anchor text has moved",
              file=sys.stderr)
        return 1
    SOURCE.write_text(injected)
    print(f"injected: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
