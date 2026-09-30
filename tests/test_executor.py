import unittest
from dataclasses import replace
from unittest.mock import Mock

from tests.support import COLUMNS, MemoryAudit, SOURCE, TABLE, TARGET, client, environment
from backfill.errors import BackfillError, CommitOutcomeUnknown
from backfill.executor import run


class ExecutorTests(unittest.TestCase):
    def invoke(self, db=None, env="dev", execute=True, audit=None, confirmation=True):
        db = db or client()
        audit = audit or MemoryAudit()
        return run(db, environment(env), TABLE, SOURCE, TARGET, execute=execute,
                   audit=audit, display=Mock(), confirm=Mock(return_value=confirmation))

    def test_dry_run_never_begins_inserts_or_commits(self):
        for env in ("dev", "uat", "prod"):
            db = client((0, 10))
            self.invoke(db, env, execute=False)
            for method in (db.begin, db.insert, db.commit, db.rollback):
                method.assert_not_called()

    def test_successful_execution_commits_after_verified_count(self):
        db, audit = client(), MemoryAudit()
        self.invoke(db, audit=audit)
        db.commit.assert_called_once()
        db.rollback.assert_not_called()
        calls = [call[0] for call in db.method_calls]
        self.assertLess(calls.index("begin"), calls.index("insert"))
        self.assertLess(calls.index("insert"), calls.index("commit"))
        self.assertEqual(db.count.call_args_list[2].kwargs, {"source": False, "lock": True})
        self.assertEqual(audit.events[-1]["status"], "committed")
        self.assertEqual(audit.events[-1]["target_row_count_after"], 10)

    def test_repeat_execution_fails_without_inserting(self):
        db = client((10, 10))
        with self.assertRaisesRegex(BackfillError, "already exists"):
            self.invoke(db)
        db.begin.assert_not_called()
        db.insert.assert_not_called()

    def test_race_between_preview_and_lock_rejected(self):
        db = client((0, 10, 10, 10))
        with self.assertRaisesRegex(BackfillError, "already exists"):
            self.invoke(db)
        db.insert.assert_not_called()
        db.rollback.assert_called_once()

    def test_source_changes_after_preview_requires_new_plan(self):
        db = client((0, 10, 0, 11))
        with self.assertRaisesRegex(BackfillError, "plan changed"):
            self.invoke(db)
        db.insert.assert_not_called()
        db.rollback.assert_called_once()

    def test_metadata_changes_after_preview_rejected(self):
        db = client()
        db.metadata.side_effect = [COLUMNS, (*COLUMNS, replace(COLUMNS[0], name="NEW_COLUMN", ordinal=4))]
        with self.assertRaisesRegex(BackfillError, "plan changed"):
            self.invoke(db)
        db.insert.assert_not_called()

    def test_insert_failure_rolls_back(self):
        db = client()
        db.insert.side_effect = RuntimeError("database detail containing secrets")
        audit = MemoryAudit()
        with self.assertRaises(RuntimeError):
            self.invoke(db, audit=audit)
        db.rollback.assert_called_once()
        db.commit.assert_not_called()
        self.assertNotIn("secrets", str(audit.events))

    def test_post_count_mismatch_rolls_back(self):
        db = client((0, 10, 0, 10, 9))
        with self.assertRaisesRegex(BackfillError, "verification"):
            self.invoke(db)
        db.rollback.assert_called_once()
        db.commit.assert_not_called()

    def test_affected_count_mismatch_rolls_back(self):
        db = client()
        db.insert.return_value = 9
        with self.assertRaises(BackfillError):
            self.invoke(db)
        db.commit.assert_not_called()

    def test_unknown_driver_rowcount_uses_locked_target_count(self):
        db = client()
        db.insert.return_value = None
        self.invoke(db)
        db.commit.assert_called_once()

    def test_prod_rejected_confirmation_never_starts_transaction(self):
        db = client()
        with self.assertRaisesRegex(BackfillError, "confirmation"):
            self.invoke(db, env="prod", confirmation=False)
        db.begin.assert_not_called()
        db.insert.assert_not_called()

    def test_prod_explicit_confirmation_succeeds(self):
        db = client()
        self.invoke(db, env="prod", confirmation=True)
        db.commit.assert_called_once()

    def test_keyboard_interrupt_rolls_back(self):
        db = client()
        db.insert.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(db)
        db.rollback.assert_called_once()

    def test_commit_failure_is_reported_unknown(self):
        db, audit = client(), MemoryAudit()
        db.commit.side_effect = CommitOutcomeUnknown("Outcome UNKNOWN")
        with self.assertRaises(CommitOutcomeUnknown):
            self.invoke(db, audit=audit)
        self.assertEqual(audit.events[-1]["status"], "commit_outcome_unknown")

    def test_audit_failure_before_commit_rolls_back(self):
        db, audit = client(), MemoryAudit()
        original = audit.emit

        def fail(event, **fields):
            if event == "commit_requested":
                raise OSError("disk full")
            original(event, **fields)

        audit.emit = fail
        with self.assertRaises(OSError):
            self.invoke(db, audit=audit)
        db.commit.assert_not_called()
        db.rollback.assert_called_once()

    def test_audit_failure_after_commit_reports_committed(self):
        db, audit = client(), MemoryAudit()
        original = audit.emit

        def fail(event, **fields):
            if fields.get("status") == "committed":
                raise OSError("disk full")
            original(event, **fields)

        audit.emit = fail
        with self.assertRaisesRegex(BackfillError, "COMMITTED"):
            self.invoke(db, audit=audit)
        db.rollback.assert_not_called()

    def test_rollback_failure_is_not_reported_as_rolled_back(self):
        db, audit = client(), MemoryAudit()
        db.insert.side_effect = RuntimeError("failed")
        db.rollback.side_effect = OSError("connection lost")
        with self.assertRaisesRegex(BackfillError, "Rollback could not"):
            self.invoke(db, audit=audit)
        self.assertEqual(audit.events[-1]["status"], "rollback_unconfirmed")
