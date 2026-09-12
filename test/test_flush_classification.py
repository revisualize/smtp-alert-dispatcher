#!/usr/bin/env python3
"""
Flush classification and record-integrity tests for smtp_alert_dispatcher.

Companion to test_smtp_alert_dispatcher.py, covering the part of the flush
state machine that decides what kind of failure just happened. Two questions
only: does an unusable record stop the run, and is a failure of the relay
session being charged to a single message.

No network. deliver_message is replaced in every test.
"""

import json
import os
import shutil
import smtplib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import smtp_alert_dispatcher as dispatcher


class FlushClassificationTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.pending = self.root / "pending"
        self.dead = self.root / "dead_letters"
        self.pending.mkdir()
        self.dead.mkdir()
        self.configuration = dict(dispatcher.DEFAULT_CONFIGURATION)
        self.configuration.update({
            "spool_directory": str(self.pending),
            "dead_letter_directory": str(self.dead),
            "lock_file": str(self.root / "flush.lock"),
            "max_delivery_attempts": 3,
        })
        self.original_deliver = dispatcher.deliver_message

    def tearDown(self):
        dispatcher.deliver_message = self.original_deliver
        shutil.rmtree(self.root, ignore_errors=True)

    def spool(self, name, attempt_count=1):
        record = dispatcher.build_message_record(name, "body", attempt_count)
        (self.pending / f"{name}.json").write_text(json.dumps(record, indent=2))

    def names(self, directory):
        return sorted(path.name for path in directory.glob("*.json"))

    def raise_on_delivery(self, error):
        def deliver(configuration, message_record):
            raise error
        dispatcher.deliver_message = deliver

    # Record integrity

    def test_unparseable_record_does_not_strand_the_queue(self):
        """A truncated record used to raise outside the try and kill the run."""
        (self.pending / "1_truncated.json").write_text('{"subject":"x","bo')
        self.spool("2_first")
        self.spool("3_second")
        delivered = []
        dispatcher.deliver_message = (
            lambda configuration, record: delivered.append(record["subject"]))

        self.assertEqual(dispatcher.command_flush(self.configuration), 0)
        self.assertEqual(delivered, ["2_first", "3_second"])
        self.assertEqual(self.names(self.pending), [])
        self.assertEqual(self.names(self.dead), ["1_truncated.json"])

    def test_record_missing_a_required_key_does_not_strand_the_queue(self):
        """Valid JSON in the wrong shape raised KeyError one frame later."""
        (self.pending / "1_shapeless.json").write_text('{"attempt_count": 1}')
        self.spool("2_first")
        delivered = []
        dispatcher.deliver_message = (
            lambda configuration, record: delivered.append(record["subject"]))

        self.assertEqual(dispatcher.command_flush(self.configuration), 0)
        self.assertEqual(delivered, ["2_first"])
        self.assertEqual(self.names(self.dead), ["1_shapeless.json"])

    def test_unusable_record_is_visible_to_status(self):
        """A quarantined record must raise the dead-letter exit code."""
        (self.pending / "1_truncated.json").write_text("{")
        dispatcher.deliver_message = lambda configuration, record: None

        dispatcher.command_flush(self.configuration)
        self.assertEqual(dispatcher.command_status(self.configuration), 2)

    # Session-level failures

    def test_authentication_failure_stops_the_run(self):
        """Stale credentials are one condition, not one rejection per message."""
        for name in ("1_a", "2_b", "3_c"):
            self.spool(name)
        self.raise_on_delivery(
            smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials"))

        for _ in range(3):
            self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        self.assertEqual(len(self.names(self.pending)), 3)
        self.assertEqual(self.names(self.dead), [])

    def test_starttls_refusal_stops_the_run(self):
        """A relay declining STARTTLS is a session property, not a bad message."""
        self.spool("1_a")
        self.raise_on_delivery(
            smtplib.SMTPNotSupportedError("STARTTLS not supported"))

        self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        self.assertEqual(self.names(self.pending), ["1_a.json"])
        self.assertEqual(self.names(self.dead), [])

    def test_helo_rejection_stops_the_run(self):
        """A rejected greeting fails every message identically."""
        self.spool("1_a")
        self.raise_on_delivery(smtplib.SMTPHeloError(550, b"bad helo"))

        self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        self.assertEqual(self.names(self.pending), ["1_a.json"])

    def test_session_failure_leaves_attempt_counts_untouched(self):
        """The spool must not age because the credentials were wrong."""
        self.spool("1_a")
        self.raise_on_delivery(
            smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials"))

        dispatcher.command_flush(self.configuration)
        record = json.loads((self.pending / "1_a.json").read_text())
        self.assertEqual(record["attempt_count"], 1)

    # Message-level failures still behave

    def test_recipient_rejection_still_charges_one_attempt(self):
        """The session-level tuple must not swallow genuine rejections."""
        self.spool("1_a")
        self.spool("2_b")
        self.raise_on_delivery(
            smtplib.SMTPRecipientsRefused({"nobody@example.net": (550, b"no user")}))

        dispatcher.command_flush(self.configuration)
        for name in ("1_a", "2_b"):
            record = json.loads((self.pending / f"{name}.json").read_text())
            self.assertEqual(record["attempt_count"], 2)

    def test_exhausted_message_still_dead_letters(self):
        """max_delivery_attempts is a ceiling, not a suggestion."""
        self.spool("1_a", attempt_count=2)
        self.raise_on_delivery(
            smtplib.SMTPRecipientsRefused({"nobody@example.net": (550, b"no user")}))

        dispatcher.command_flush(self.configuration)
        self.assertEqual(self.names(self.pending), [])
        self.assertEqual(self.names(self.dead), ["1_a.json"])

    def test_socket_failure_still_stops_the_run(self):
        """An unreachable relay ends the run rather than burning the queue."""
        self.spool("1_a")
        self.spool("2_b")
        self.raise_on_delivery(ConnectionRefusedError(111, "Connection refused"))

        self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        self.assertEqual(len(self.names(self.pending)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
