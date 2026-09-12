#!/usr/bin/env python3
"""
Tests for smtp_alert_dispatcher.

Organized around the one promise the tool makes: a message is delivered, or
it is on disk. Never neither. Each test either proves that promise holds or
proves a specific way it could quietly stop holding.

No network. deliver_message is replaced in every test that exercises
delivery, because a test that needs a relay is a test that gets skipped.
"""

import fcntl
import json
import smtplib
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smtp_alert_dispatcher as dispatcher


def reject_at_message_level(*args, **kwargs):
    raise smtplib.SMTPDataError(550, b"mailbox unavailable")


def fail_at_connection_level(*args, **kwargs):
    raise smtplib.SMTPServerDisconnected("relay went away")


def deliver_successfully(*args, **kwargs):
    return None


class DispatcherTestCase(unittest.TestCase):
    """Base class giving every test an isolated spool rooted in a temp dir."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        spool = root / "pending"
        dead = root / "dead_letters"
        spool.mkdir()
        dead.mkdir()
        self.configuration = {
            "relay_host": "mailrelay.example.net",
            "relay_port": 587,
            "use_starttls": True,
            "smtp_username": "",
            "smtp_password_file": "",
            "sender_address": "alerts@example.net",
            "recipient_addresses": ["storage-alerts@example.net"],
            "connect_timeout_seconds": 10,
            "max_delivery_attempts": 3,
            "spool_directory": str(spool),
            "dead_letter_directory": str(dead),
            "lock_file": str(root / "dispatcher.lock"),
        }

    def pending(self):
        return sorted(Path(self.configuration["spool_directory"]).glob("*.json"))

    def dead_letters(self):
        return sorted(Path(self.configuration["dead_letter_directory"]).glob("*.json"))

    def spool(self, subject="subject", body="body", attempts=1):
        record = dispatcher.build_message_record(subject, body, attempts)
        return dispatcher.spool_message(self.configuration, record)

    def patch_delivery(self, replacement):
        patcher = mock.patch.object(dispatcher, "deliver_message", replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def silence_stdout(self):
        captured = StringIO()
        patcher = mock.patch.object(sys, "stdout", captured)
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured


class SpoolingIsAtomic(DispatcherTestCase):

    def test_spooled_record_is_readable_and_complete(self):
        path = self.spool("disk filling", "body text")
        record = json.loads(path.read_text())
        self.assertEqual(record["subject"], "disk filling")
        self.assertEqual(record["body"], "body text")
        self.assertEqual(record["attempt_count"], 1)
        self.assertTrue(record["created_at"])

    def test_no_temporary_file_survives_the_write(self):
        self.spool()
        leftovers = list(Path(self.configuration["spool_directory"]).glob("*.tmp"))
        self.assertEqual(leftovers, [], "a .tmp survived; the rename is not atomic")

    def test_a_half_written_message_is_invisible_to_flush_and_status(self):
        """The atomic write depends on the temp suffix missing the *.json glob.

        If that suffix ever becomes .json, flush will try to parse a
        partially written file and raise mid-run.
        """
        self.silence_stdout()
        spool = Path(self.configuration["spool_directory"])
        (spool / "1700000000_partial.tmp").write_text('{"incomp')
        self.assertEqual(self.pending(), [])
        self.assertEqual(dispatcher.command_status(self.configuration), 0)


class SendDeliversOrSpools(DispatcherTestCase):

    def setUp(self):
        super().setUp()
        self.silence_stdout()

    def test_success_returns_zero_and_spools_nothing(self):
        self.patch_delivery(deliver_successfully)
        arguments = SimpleNamespace(subject="all clear", body="body")
        self.assertEqual(dispatcher.command_send(self.configuration, arguments), 0)
        self.assertEqual(self.pending(), [])

    def test_a_refused_connection_spools_and_returns_one(self):
        def refuse(*args, **kwargs):
            raise ConnectionRefusedError("connection refused")

        self.patch_delivery(refuse)
        arguments = SimpleNamespace(subject="disk filling", body="body")
        self.assertEqual(dispatcher.command_send(self.configuration, arguments), 1)
        self.assertEqual(len(self.pending()), 1)

    def test_an_smtp_rejection_spools_too(self):
        self.patch_delivery(reject_at_message_level)
        arguments = SimpleNamespace(subject="subject", body="body")
        self.assertEqual(dispatcher.command_send(self.configuration, arguments), 1)
        self.assertEqual(len(self.pending()), 1)


class FlushSemantics(DispatcherTestCase):

    def setUp(self):
        super().setUp()
        self.silence_stdout()

    def test_delivered_messages_leave_the_spool(self):
        for index in range(3):
            self.spool(f"subject {index}")
        self.patch_delivery(deliver_successfully)
        self.assertEqual(dispatcher.command_flush(self.configuration), 0)
        self.assertEqual(self.pending(), [])

    def test_a_connection_level_failure_stops_the_run(self):
        """One unreachable relay must not burn an attempt on every message."""
        for index in range(3):
            self.spool(f"subject {index}")

        calls = []

        def unreachable(*args, **kwargs):
            calls.append(1)
            return fail_at_connection_level()

        self.patch_delivery(unreachable)
        self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        self.assertEqual(len(calls), 1, "flush kept trying after the relay dropped")
        self.assertEqual(len(self.pending()), 3)

    def test_a_message_level_rejection_charges_one_attempt(self):
        self.spool()
        self.patch_delivery(reject_at_message_level)
        dispatcher.command_flush(self.configuration)
        record = json.loads(self.pending()[0].read_text())
        self.assertEqual(record["attempt_count"], 2)

    def test_one_poison_message_does_not_stall_the_spool(self):
        self.spool("poison")
        self.spool("healthy")

        def reject_only_poison(configuration, message_record):
            if message_record["subject"] == "poison":
                return reject_at_message_level()

        self.patch_delivery(reject_only_poison)
        dispatcher.command_flush(self.configuration)
        remaining = [json.loads(p.read_text())["subject"] for p in self.pending()]
        self.assertEqual(remaining, ["poison"], "the healthy message was not delivered")

    def test_the_attempt_ceiling_retires_a_message_to_dead_letters(self):
        """max_delivery_attempts is 3 here, so the third run retires it."""
        self.spool()
        self.patch_delivery(reject_at_message_level)
        for _ in range(3):
            dispatcher.command_flush(self.configuration)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.dead_letters()), 1)

    def test_a_concurrent_flush_declines_rather_than_double_sending(self):
        self.spool()
        self.patch_delivery(deliver_successfully)
        holder = open(self.configuration["lock_file"], "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(dispatcher.command_flush(self.configuration), 0)
            self.assertEqual(len(self.pending()), 1,
                             "the locked-out flush delivered anyway")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


class HandlerOrderRegression(DispatcherTestCase):
    """smtplib.SMTPException subclasses OSError. Handler order decides everything.

    If the bare `except OSError` moves above the `except
    smtplib.SMTPException` branch in command_flush, every message-level
    rejection is reclassified as a connection failure. Flush breaks out of
    the loop instead of charging an attempt, attempt_count never rises,
    nothing ever reaches the dead-letter directory, and a permanently
    undeliverable message is retried forever with no operator signal.

    The promise still technically holds. The operator still gets nothing.
    """

    def setUp(self):
        super().setUp()
        self.silence_stdout()

    def test_the_premise_still_holds(self):
        self.assertTrue(
            issubclass(smtplib.SMTPException, OSError),
            "premise changed: SMTPException no longer subclasses OSError",
        )

    def test_smtp_exception_is_caught_before_bare_oserror(self):
        self.spool()
        self.patch_delivery(reject_at_message_level)
        dispatcher.command_flush(self.configuration)
        record = json.loads(self.pending()[0].read_text())
        self.assertEqual(
            record["attempt_count"], 2,
            "attempt_count did not rise, so the OSError handler shadowed the "
            "SMTPException handler and dead-lettering is unreachable",
        )


class StatusExitCodes(DispatcherTestCase):

    def setUp(self):
        super().setUp()
        self.captured = self.silence_stdout()

    def write_dead_letter(self):
        path = Path(self.configuration["dead_letter_directory"]) / "1700000000_x.json"
        path.write_text(json.dumps({"subject": "s", "body": "b", "attempt_count": 20}))

    def test_clean_spool_reports_zero(self):
        self.assertEqual(dispatcher.command_status(self.configuration), 0)
        self.assertIn("pending: 0", self.captured.getvalue())

    def test_pending_messages_report_one(self):
        self.spool()
        self.assertEqual(dispatcher.command_status(self.configuration), 1)

    def test_dead_letters_report_two(self):
        self.write_dead_letter()
        self.assertEqual(dispatcher.command_status(self.configuration), 2)

    def test_dead_letters_outrank_pending(self):
        """A dead letter is the louder signal; it must win the exit code."""
        self.spool()
        self.write_dead_letter()
        self.assertEqual(dispatcher.command_status(self.configuration), 2)


class AtomicUpdateDuringFlush(DispatcherTestCase):
    """The flush write-back must be as atomic as the initial spool.

    An earlier revision incremented attempt_count with a direct
    write_text() to the live *.json file. A power loss or a full disk
    partway through that write truncates a spooled message and loses it
    outright, which is the single outcome this tool exists to prevent.
    The suite did not catch it because the atomicity test only exercised
    spool_message, where the property held.
    """

    def setUp(self):
        super().setUp()
        self.silence_stdout()

    def test_charging_an_attempt_leaves_no_temporary_file(self):
        self.spool()
        self.patch_delivery(reject_at_message_level)
        dispatcher.command_flush(self.configuration)
        leftovers = list(Path(self.configuration["spool_directory"]).glob("*.tmp"))
        self.assertEqual(leftovers, [], "the flush write-back is not atomic")

    def test_a_crash_mid_update_leaves_the_original_record_intact(self):
        """Simulates power loss between writing the temp file and renaming."""
        path = self.spool("disk filling", "body text")
        before = path.read_text()
        self.patch_delivery(reject_at_message_level)

        def fail_at_rename(*args, **kwargs):
            raise OSError("simulated power loss before rename")

        with mock.patch.object(dispatcher.os, "replace", fail_at_rename):
            try:
                dispatcher.command_flush(self.configuration)
            except OSError:
                pass

        self.assertEqual(path.read_text(), before,
                         "the live spool record was mutated in place")
        json.loads(path.read_text())

    def test_the_updated_record_is_still_valid_json(self):
        self.spool()
        self.patch_delivery(reject_at_message_level)
        dispatcher.command_flush(self.configuration)
        record = json.loads(self.pending()[0].read_text())
        self.assertEqual(record["attempt_count"], 2)
        self.assertIn("subject", record)


class StableMessageIdentity(DispatcherTestCase):
    """Delivery is at-least-once, so a retry must be the same message.

    A message can be accepted by the relay and still retried when the
    connection drops before the spool file is unlinked. Without a stable
    Message-ID every retry arrives as a distinct mail that no client can
    thread or collapse.
    """

    def setUp(self):
        super().setUp()
        self.silence_stdout()

    def test_a_record_carries_an_identity(self):
        record = dispatcher.build_message_record("subject", "body")
        self.assertTrue(record["message_id"].startswith("<"))
        self.assertTrue(record["date"])

    def test_identity_survives_repeated_flush_attempts(self):
        # Raised above the fixture default so the message is still pending
        # after two charged attempts rather than retired to dead letters.
        self.configuration["max_delivery_attempts"] = 10
        path = self.spool()
        original = json.loads(path.read_text())
        self.patch_delivery(reject_at_message_level)
        for _ in range(2):
            dispatcher.command_flush(self.configuration)
        current = json.loads(self.pending()[0].read_text())
        self.assertEqual(current["message_id"], original["message_id"])
        self.assertEqual(current["date"], original["date"])
        self.assertEqual(current["attempt_count"], 3)

    def test_the_headers_actually_reach_the_message(self):
        record = dispatcher.build_message_record("disk filling", "body")
        captured = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, **kwargs):
                pass

            def send_message(self, outgoing):
                captured["message_id"] = outgoing["Message-ID"]
                captured["date"] = outgoing["Date"]

        with mock.patch.object(dispatcher.smtplib, "SMTP", FakeSMTP):
            dispatcher.deliver_message(self.configuration, record)
        self.assertEqual(captured["message_id"], record["message_id"])
        self.assertEqual(captured["date"], record["date"])

    def test_a_legacy_record_without_an_identity_still_delivers(self):
        """Records spooled by an earlier revision must not raise."""
        legacy = {"subject": "s", "body": "b", "attempt_count": 1,
                  "created_at": "2026-09-07T00:00:00-0700"}
        sent = []

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, **kwargs):
                pass

            def send_message(self, outgoing):
                sent.append(outgoing["Message-ID"])

        with mock.patch.object(dispatcher.smtplib, "SMTP", FakeSMTP):
            dispatcher.deliver_message(self.configuration, legacy)
        self.assertTrue(sent[0].startswith("<"))


class DefensiveEntryPoints(DispatcherTestCase):

    def setUp(self):
        super().setUp()
        self.silence_stdout()

    def test_send_refuses_rather_than_hanging_on_a_tty(self):
        """Reading a tty for the body waits forever and looks like a hang."""
        arguments = SimpleNamespace(subject="subject", body=None)
        with mock.patch.object(sys.stdin, "isatty", lambda: True):
            self.assertEqual(dispatcher.command_send(self.configuration, arguments), 2)
        self.assertEqual(self.pending(), [])

    def test_the_lock_file_is_not_truncated_by_a_flush(self):
        Path(self.configuration["lock_file"]).write_text("owner marker\n")
        self.patch_delivery(deliver_successfully)
        dispatcher.command_flush(self.configuration)
        self.assertEqual(Path(self.configuration["lock_file"]).read_text(),
                         "owner marker\n")

    def test_the_lock_directory_is_bootstrapped(self):
        """A minimal container may have no /var/lock; flush must not raise."""
        root = Path(self.temporary_directory.name) / "missing" / "deeper"
        config = dict(self.configuration)
        config["lock_file"] = str(root / "dispatcher.lock")
        patched = dict(dispatcher.DEFAULT_CONFIGURATION)
        patched.update(config)
        with mock.patch.object(dispatcher, "DEFAULT_CONFIGURATION", patched), \
             mock.patch.object(dispatcher, "CONFIGURATION_PATH", Path("/nonexistent")):
            resolved = dispatcher.load_configuration()
        self.assertTrue(Path(resolved["lock_file"]).parent.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
