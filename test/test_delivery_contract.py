#!/usr/bin/env python3
"""
Delivery-contract tests for smtp_alert_dispatcher.

Each test here failed against the code before the change that makes it
pass. Test and class names say what behavior is under test.

No network. The relay is FakeRelay below, which follows smtplib.SMTP's
stages closely enough to fail the way a real relay does: STARTTLS, AUTH,
MAIL, RCPT, DATA, and QUIT can each be told to fail, and it records the
envelope of every message it accepted.
"""

import errno
import fcntl
import io
import json
import os
import smtplib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smtp_alert_dispatcher as dispatcher


class FakeRelay:
    """A stand-in for smtplib.SMTP with a failure switch per SMTP stage."""

    accepted = []
    starttls_error = None
    login_error = None
    send_error = None
    refused = {}
    quit_code = 221
    login_calls = []

    @classmethod
    def reset(cls):
        cls.accepted = []
        cls.starttls_error = None
        cls.login_error = None
        cls.send_error = None
        cls.refused = {}
        cls.quit_code = 221
        cls.login_calls = []

    def __init__(self, *args, **kwargs):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        # Mirrors smtplib.SMTP.__exit__: a QUIT reply other than 221 raises.
        self.quit()
        return False

    def starttls(self, **kwargs):
        if FakeRelay.starttls_error is not None:
            raise FakeRelay.starttls_error

    def login(self, user, password):
        FakeRelay.login_calls.append((user, password))
        if FakeRelay.login_error is not None:
            raise FakeRelay.login_error

    def send_message(self, message, from_addr=None, to_addrs=None, **kwargs):
        if FakeRelay.send_error is not None:
            raise FakeRelay.send_error
        if to_addrs is None:
            to_addrs = [address.strip() for address in message["To"].split(",")]
        delivered_to = [address for address in to_addrs
                        if address not in FakeRelay.refused]
        FakeRelay.accepted.append({"subject": str(message["Subject"]),
                                   "to": delivered_to})
        return {address: FakeRelay.refused[address]
                for address in to_addrs if address in FakeRelay.refused}

    def quit(self):
        if FakeRelay.quit_code != 221:
            raise smtplib.SMTPResponseException(FakeRelay.quit_code, b"quit failed")

    def close(self):
        self.closed = True


class ContractTestCase(unittest.TestCase):

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.pending_directory = self.root / "pending"
        self.dead_directory = self.root / "dead_letters"
        self.pending_directory.mkdir()
        self.dead_directory.mkdir()
        self.configuration = dict(dispatcher.DEFAULT_CONFIGURATION)
        self.configuration.update({
            "recipient_addresses": ["a@example.net", "b@example.net"],
            "spool_directory": str(self.pending_directory),
            "dead_letter_directory": str(self.dead_directory),
            "lock_file": str(self.root / "dispatcher.lock"),
            "max_delivery_attempts": 3,
        })
        FakeRelay.reset()
        relay_patch = mock.patch.object(dispatcher.smtplib, "SMTP", FakeRelay)
        relay_patch.start()
        self.addCleanup(relay_patch.stop)
        self.standard_output = io.StringIO()
        self.standard_error = io.StringIO()
        for name, stream in (("stdout", self.standard_output),
                             ("stderr", self.standard_error)):
            patcher = mock.patch.object(sys, name, stream)
            patcher.start()
            self.addCleanup(patcher.stop)

    def pending(self):
        return sorted(self.pending_directory.glob("*.json"))

    def dead(self):
        return sorted(self.dead_directory.glob("*.json"))

    def read(self, path):
        return json.loads(Path(path).read_text())

    def spool(self, subject="subject", attempts=1, name=None):
        record = dispatcher.build_message_record(subject, "body", attempts)
        if name is None:
            return dispatcher.spool_message(self.configuration, record)
        path = self.pending_directory / name
        path.write_text(json.dumps(record))
        return path

    def send(self, subject="subject", body="body"):
        return dispatcher.command_send(
            self.configuration, SimpleNamespace(subject=subject, body=body))

    def write_configuration(self, content):
        configuration_path = self.root / "config.json"
        configuration_path.write_text(content)
        return configuration_path

    def run_main(self, arguments, configuration_text):
        configuration_path = self.write_configuration(configuration_text)
        with mock.patch.object(dispatcher, "CONFIGURATION_PATH", configuration_path), \
             mock.patch.object(sys, "argv", ["smtp_alert_dispatcher.py"] + arguments):
            return dispatcher.main()


class RecipientLevelDelivery(ContractTestCase):
    """a refused recipient is kept for retry, never dropped."""

    def test_send_with_one_of_two_refused_spools_the_refused_one(self):
        FakeRelay.refused = {"b@example.net": (550, b"no such user")}
        self.assertEqual(self.send(), 1)
        records = [self.read(path) for path in self.pending()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pending_recipients"], ["b@example.net"])

    def test_send_with_two_of_three_refused_spools_both(self):
        self.configuration["recipient_addresses"] = [
            "a@example.net", "b@example.net", "c@example.net"]
        FakeRelay.refused = {"b@example.net": (450, b"try later"),
                             "c@example.net": (550, b"no such user")}
        self.assertEqual(self.send(), 1)
        record = self.read(self.pending()[0])
        self.assertEqual(record["pending_recipients"],
                         ["b@example.net", "c@example.net"])

    def test_flush_retries_only_the_refused_recipient(self):
        FakeRelay.refused = {"b@example.net": (450, b"try later")}
        self.send()
        FakeRelay.reset()
        self.assertEqual(dispatcher.command_flush(self.configuration), 0)
        self.assertEqual(FakeRelay.accepted[-1]["to"], ["b@example.net"],
                         "the retry went to a recipient that already accepted")

    def test_flush_partial_refusal_keeps_the_refused_and_charges(self):
        self.spool()
        FakeRelay.refused = {"a@example.net": (450, b"try later")}
        self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        record = self.read(self.pending()[0])
        self.assertEqual(record["pending_recipients"], ["a@example.net"])
        self.assertEqual(record["attempt_count"], 2)

    def test_recipients_are_fixed_when_the_alert_is_created(self):
        FakeRelay.send_error = ConnectionRefusedError(111, "refused")
        self.assertEqual(self.send(), 1)
        self.configuration["recipient_addresses"] = ["new@example.net"]
        FakeRelay.reset()
        dispatcher.command_flush(self.configuration)
        self.assertEqual(FakeRelay.accepted[-1]["to"],
                         ["a@example.net", "b@example.net"])


class TypedSpoolRecords(ContractTestCase):
    """any unusable record is dead-lettered; the queue moves."""

    BAD_RECORDS = {
        "null": "null",
        "number": "5",
        "list_of_key_names": '["subject", "body", "attempt_count"]',
        "string_of_key_names": '"subject body attempt_count"',
        "text_count": '{"subject": "s", "body": "b", "attempt_count": "1"}',
        "boolean_count": '{"subject": "s", "body": "b", "attempt_count": true}',
        "negative_count": '{"subject": "s", "body": "b", "attempt_count": -5}',
        "float_count": '{"subject": "s", "body": "b", "attempt_count": 1.5}',
        "null_subject": '{"subject": null, "body": "b", "attempt_count": 1}',
        "list_body": '{"subject": "s", "body": ["b"], "attempt_count": 1}',
        "empty_pending_recipients":
            '{"subject": "s", "body": "b", "attempt_count": 1, "pending_recipients": []}',
        "message_id_with_linefeed":
            '{"subject": "s", "body": "b", "attempt_count": 1, "message_id": "x\\ny"}',
        "date_with_linefeed":
            '{"subject": "s", "body": "b", "attempt_count": 1, "date": "x\\ry"}',
        "subject_with_linefeed":
            '{"subject": "a\\nb", "body": "b", "attempt_count": 1}',
    }

    def test_each_wrong_shape_is_dead_lettered_and_the_queue_moves(self):
        for label, payload in self.BAD_RECORDS.items():
            with self.subTest(label):
                for path in self.pending() + self.dead():
                    path.unlink()
                FakeRelay.reset()
                (self.pending_directory / "1_bad.json").write_text(payload)
                self.spool("healthy", name="2_good.json")
                result = dispatcher.command_flush(self.configuration)
                self.assertEqual(result, 0)
                self.assertEqual([path.name for path in self.dead()], ["1_bad.json"])
                self.assertEqual([entry["subject"] for entry in FakeRelay.accepted],
                                 ["healthy"])


class UnbuildableInput(ContractTestCase):
    """an alert with a hostile subject or damaged bytes is still kept."""

    def test_subject_with_linefeed_is_spooled_not_lost(self):
        FakeRelay.send_error = ConnectionRefusedError(111, "refused")
        self.assertEqual(self.send(subject="disk\nfull"), 1)
        self.assertEqual(self.read(self.pending()[0])["subject"], "disk full")

    def test_body_with_undecodable_bytes_is_spooled_not_lost(self):
        FakeRelay.send_error = ConnectionRefusedError(111, "refused")
        self.assertEqual(self.send(body="bad \udcff byte"), 1)
        self.assertEqual(self.read(self.pending()[0])["body"], "bad � byte")

    def test_stdin_bytes_that_are_not_utf8_are_delivered(self):
        stdin = SimpleNamespace(isatty=lambda: False,
                                buffer=io.BytesIO(b"body \xff\xfe end\n"),
                                read=lambda: "body \udcff\udcfe end\n")
        with mock.patch.object(sys, "stdin", stdin):
            self.assertEqual(self.send(body=None), 0)
        self.assertEqual(len(FakeRelay.accepted), 1)


class SpoolFailureIsNotSpooled(ContractTestCase):
    """exit 1 means on disk; a failed spool write says so with exit 3."""

    def test_failed_spool_write_returns_three(self):
        FakeRelay.send_error = ConnectionRefusedError(111, "refused")

        def disk_full(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(dispatcher, "write_record_atomically", disk_full):
            self.assertEqual(self.send(), 3)
        self.assertIn("not confirmed", self.standard_error.getvalue())


class TypedConfiguration(ContractTestCase):
    """a configuration the tool cannot honor is exit 2."""

    def base(self, **overrides):
        configuration = {
            "spool_directory": str(self.pending_directory),
            "dead_letter_directory": str(self.dead_directory),
            "lock_file": str(self.root / "dispatcher.lock"),
        }
        configuration.update(overrides)
        return json.dumps(configuration)

    def test_configuration_errors_exit_two(self):
        cases = {
            "not_an_object": '["a"]',
            "text_attempts": self.base(max_delivery_attempts="20"),
            "text_recipients": self.base(recipient_addresses="ops@example.net"),
            "empty_recipients": self.base(recipient_addresses=[]),
            "text_starttls": self.base(use_starttls="false"),
            "boolean_port": self.base(relay_port=True),
            "port_out_of_range": self.base(relay_port=70000),
            "zero_attempts": self.base(max_delivery_attempts=0),
            "sender_with_linefeed":
                self.base(sender_address="a@example.net\nBcc: x@example.net"),
            "recipient_with_linefeed":
                self.base(recipient_addresses=["a@example.net\nBcc: x@example.net"]),
            "relative_spool": self.base(spool_directory="pending"),
            "same_directories":
                self.base(dead_letter_directory=str(self.pending_directory)),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertEqual(self.run_main(["status"], text), 2)

    def test_a_fractional_timeout_is_accepted(self):
        self.assertEqual(self.run_main(["status"], self.base(connect_timeout_seconds=0.5)), 0)


class CredentialsOnlyOverTls(ContractTestCase):
    """AUTH is never sent without STARTTLS; the alert is kept."""

    def setUp(self):
        super().setUp()
        password_path = self.root / "password"
        password_path.write_text("secret\n")
        self.configuration.update(use_starttls=False, smtp_username="alerts",
                                  smtp_password_file=str(password_path))

    def test_send_does_not_authenticate_and_spools(self):
        self.assertEqual(self.send(), 1)
        self.assertEqual(FakeRelay.login_calls, [])
        self.assertEqual(len(self.pending()), 1)

    def test_flush_stops_without_charging(self):
        path = self.spool()
        self.assertEqual(dispatcher.command_flush(self.configuration), 1)
        self.assertEqual(FakeRelay.login_calls, [])
        self.assertEqual(self.read(path)["attempt_count"], 1)


class PasswordFileProblems(ContractTestCase):
    """a password that cannot be read keeps the alert on disk."""

    def test_password_file_that_is_not_utf8_spools(self):
        password_path = self.root / "password"
        password_path.write_bytes(b"pa\xffss")
        self.configuration.update(smtp_username="alerts",
                                  smtp_password_file=str(password_path))
        self.assertEqual(self.send(), 1)
        self.assertEqual(len(self.pending()), 1)


class ExplicitModes(ContractTestCase):
    """modes do not follow the umask; symlinks are refused."""

    def test_created_directories_and_files_are_private_under_umask_000(self):
        fresh = self.root / "fresh"
        overrides = {
            "spool_directory": str(fresh / "spool" / "pending"),
            "dead_letter_directory": str(fresh / "spool" / "dead_letters"),
            "lock_file": str(fresh / "lock" / "dispatcher.lock"),
        }
        configuration_path = self.write_configuration(json.dumps(overrides))
        previous_umask = os.umask(0)
        try:
            with mock.patch.object(dispatcher, "CONFIGURATION_PATH", configuration_path):
                configuration = dispatcher.load_configuration()
            record_path = dispatcher.spool_message(
                configuration, dispatcher.build_message_record("s", "b"))
            record_mode = stat.S_IMODE(os.stat(record_path).st_mode)
            dispatcher.command_flush(configuration)
        finally:
            os.umask(previous_umask)

        def mode(path):
            return stat.S_IMODE(os.stat(path).st_mode)

        for directory in (fresh, fresh / "spool", fresh / "spool" / "pending",
                          fresh / "spool" / "dead_letters", fresh / "lock"):
            self.assertEqual(mode(directory), 0o700, str(directory))
        self.assertEqual(record_mode, 0o600)
        self.assertEqual(mode(fresh / "lock" / "dispatcher.lock"), 0o600)

    def test_symlink_at_the_lock_path_is_not_followed(self):
        victim = self.root / "victim"
        victim.write_text("untouched")
        os.symlink(victim, self.configuration["lock_file"])
        with self.assertRaises(OSError):
            dispatcher.command_flush(self.configuration)
        self.assertEqual(victim.read_text(), "untouched")

    def test_symlink_at_the_temporary_path_is_not_followed(self):
        victim = self.root / "victim"
        victim.write_text("untouched")
        final_path = self.pending_directory / "1_record.json"
        os.symlink(victim, final_path.with_suffix(".tmp"))
        with self.assertRaises(OSError):
            dispatcher.write_record_atomically(
                final_path, dispatcher.build_message_record("s", "b"))
        self.assertEqual(victim.read_text(), "untouched")


class LockLifetime(ContractTestCase):
    """the lock is released on every exit path, including a crash."""

    def test_lock_is_released_after_an_escaping_exception(self):
        self.spool()
        FakeRelay.send_error = smtplib.SMTPDataError(550, b"rejected")

        def fail(*args, **kwargs):
            raise OSError(errno.EIO, "simulated write failure")

        # The check runs inside the except block on purpose: the traceback
        # still references command_flush's frame there, so a handle the
        # function forgot to close is still open. assertRaises would clear
        # those frames and let garbage collection close it, hiding the leak.
        with mock.patch.object(dispatcher, "write_record_atomically", fail):
            try:
                dispatcher.command_flush(self.configuration)
            except OSError:
                with open(self.configuration["lock_file"], "a") as second_handle:
                    try:
                        fcntl.flock(second_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.fail("the lock was still held after flush raised")
            else:
                self.fail("the simulated write failure did not escape")


class StageAwareClassification(ContractTestCase):
    """a relay-wide failure stops the run without charging anyone."""

    SESSION_FAILURES = {
        "starttls_454": ("starttls_error",
                         smtplib.SMTPResponseException(454, b"TLS not available")),
        "auth_no_mechanism": ("login_error",
                              smtplib.SMTPException("No suitable authentication method found.")),
        "sender_refused": ("send_error",
                           smtplib.SMTPSenderRefused(550, b"sender rejected", "alerts@example.net")),
        "data_421": ("send_error", smtplib.SMTPDataError(421, b"shutting down")),
    }

    def test_session_failures_stop_without_charging(self):
        for label, (stage, error) in self.SESSION_FAILURES.items():
            with self.subTest(label):
                for path in self.pending():
                    path.unlink()
                FakeRelay.reset()
                self.configuration.update(smtp_username="", smtp_password_file="")
                if stage == "login_error":
                    password_path = self.root / "password"
                    password_path.write_text("secret")
                    self.configuration.update(smtp_username="alerts",
                                              smtp_password_file=str(password_path))
                setattr(FakeRelay, stage, error)
                first = self.spool("first")
                second = self.spool("second")
                self.assertEqual(dispatcher.command_flush(self.configuration), 1)
                self.assertEqual(self.read(first)["attempt_count"], 1)
                self.assertEqual(self.read(second)["attempt_count"], 1)

    def test_quit_failure_after_acceptance_is_still_delivered(self):
        FakeRelay.quit_code = 451
        self.assertEqual(self.send(), 0)
        self.assertEqual(self.pending(), [])

    def test_smtputf8_refusal_is_charged_to_the_message(self):
        self.spool("first")
        self.spool("second")
        FakeRelay.send_error = smtplib.SMTPNotSupportedError("SMTPUTF8 not supported")
        dispatcher.command_flush(self.configuration)
        counts = [self.read(path)["attempt_count"] for path in self.pending()]
        self.assertEqual(counts, [2, 2])


class ReportingAfterDelivery(ContractTestCase):
    """what happens after the relay accepted."""

    def test_broken_stdout_after_delivery_is_not_a_delivery_failure(self):
        class BrokenStream(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        with mock.patch.object(sys, "stdout", BrokenStream()):
            self.assertEqual(self.send(), 0)
        self.assertEqual(self.pending(), [], "a delivered alert was spooled again")

    def test_failed_removal_after_delivery_is_reported_as_such(self):
        self.spool()
        real_unlink = Path.unlink

        def refuse_unlink(path, *args, **kwargs):
            if path.suffix == ".json":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", refuse_unlink), \
             mock.patch.object(dispatcher.os, "unlink", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            self.assertEqual(dispatcher.command_flush(self.configuration), 3)
        self.assertNotIn("relay unreachable", self.standard_error.getvalue())
        self.assertIn("delivered", self.standard_error.getvalue())


class FlushExitCodes(ContractTestCase):
    """the flush exit code describes the spool."""

    def test_lock_held_with_pending_records_returns_one(self):
        self.spool()
        with open(self.configuration["lock_file"], "a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(dispatcher.command_flush(self.configuration), 1)

    def test_lock_held_with_an_empty_spool_returns_zero(self):
        with open(self.configuration["lock_file"], "a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(dispatcher.command_flush(self.configuration), 0)

    def test_failed_dead_letter_sync_exits_three(self):
        self.spool(attempts=2)
        FakeRelay.send_error = smtplib.SMTPDataError(550, b"rejected")
        real_sync = dispatcher.fsync_directory

        def fail_on_dead_letters(directory_path):
            if Path(directory_path) == self.dead_directory:
                raise OSError(errno.EIO, "Input/output error")
            return real_sync(directory_path)

        configuration_text = json.dumps({
            "spool_directory": str(self.pending_directory),
            "dead_letter_directory": str(self.dead_directory),
            "lock_file": self.configuration["lock_file"],
            "max_delivery_attempts": 3,
        })
        with mock.patch.object(dispatcher, "fsync_directory", fail_on_dead_letters):
            self.assertEqual(self.run_main(["flush"], configuration_text), 3)


class DeadLetterIntegrity(ContractTestCase):
    """retirement never destroys or follows anything."""

    def test_a_name_collision_keeps_both_dead_letters(self):
        (self.dead_directory / "x.json").write_text('{"subject": "older alert"}')
        (self.pending_directory / "x.json").write_text("{")
        dispatcher.command_flush(self.configuration)
        contents = sorted(path.read_text() for path in self.dead())
        self.assertEqual(contents, ["{", '{"subject": "older alert"}'])

    def test_a_symlinked_spool_entry_is_not_delivered(self):
        target = self.root / "private.json"
        target.write_text(json.dumps({"subject": "private", "body": "b",
                                      "attempt_count": 1}))
        os.symlink(target, self.pending_directory / "1_link.json")
        dispatcher.command_flush(self.configuration)
        self.assertEqual(FakeRelay.accepted, [])
        self.assertTrue(target.exists())
        self.assertEqual([path.name for path in self.dead()], ["1_link.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
