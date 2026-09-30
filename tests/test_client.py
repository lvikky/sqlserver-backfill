import unittest
from unittest.mock import Mock

from tests.support import COLUMNS, SOURCE, TABLE, TARGET, environment
from backfill.errors import BackfillError, CommitOutcomeUnknown
from backfill.sql_generator import insert_sql
from backfill.sqlserver_client import SqlServerClient


def make_client(execute=False):
    connection = Mock()
    cursor = Mock()
    connection.cursor.return_value = cursor
    connector = Mock(return_value=connection)
    db = SqlServerClient(environment(), execute_enabled=execute, connector=connector)
    return db, connection, cursor, connector


class ClientTests(unittest.TestCase):
    def test_database_identity_verified(self):
        db, _, cursor, _ = make_client()
        cursor.fetchone.return_value = ("TEST_PROD", "server", 42, 16, 1)
        with self.assertRaisesRegex(BackfillError, "does not match"):
            db.verify_environment()

    def test_driver_connection_and_session(self):
        db, connection, cursor, connector = make_client()
        cursor.fetchone.return_value = ("TEST_DEV", "server", 42, 16, 1)
        db.verify_environment()
        self.assertEqual(db.session_id, 42)
        self.assertEqual(connection.timeout, 60)
        self.assertEqual(connector.call_args.kwargs, {"autocommit": True, "timeout": 15})

    def test_incomplete_metadata_permissions_rejected(self):
        db, _, cursor, _ = make_client()
        cursor.fetchone.return_value = ("TEST_DEV", "server", 42, 16, 0)
        with self.assertRaisesRegex(BackfillError, "VIEW DEFINITION"):
            db.verify_environment()

    def test_metadata_order_and_types(self):
        db, _, cursor, _ = make_client()
        cursor.fetchone.side_effect = [(123, 0, 0, 0, 0, 0, 0), (1, 0)]
        cursor.fetchall.return_value = [
            ("ENTITY", 1, "varchar", 0, 0, 0, 0, 0, 0, 0, 0, 0),
            ("AS_OF_DATE", 2, "date", 0, 0, 0, 0, 0, 0, 0, 0, 0),
            ("DT_EL_EXECUTIONTIMESTAMP", 3, "datetime2", 0, 0, 0, 0, 0, 0, 0, 0, 0),
        ]
        self.assertEqual(db.metadata(TABLE), COLUMNS)
        sql_calls = [call.args[0].lstrip() for call in cursor.execute.call_args_list]
        self.assertTrue(all(sql.startswith("SELECT") for sql in sql_calls))

    def test_missing_table(self):
        db, _, cursor, _ = make_client()
        cursor.fetchone.return_value = None
        with self.assertRaisesRegex(BackfillError, "missing"):
            db.metadata(TABLE)

    def test_unsafe_table_features_rejected(self):
        for position in range(1, 7):
            db, _, cursor, _ = make_client()
            row = [123, 0, 0, 0, 0, 0, 0]
            row[position] = 1
            cursor.fetchone.return_value = row
            with self.subTest(position=position), self.assertRaises(BackfillError):
                db.metadata(TABLE)

    def test_dry_run_write_methods_guarded(self):
        db, _, cursor, _ = make_client()
        for action in (db.begin, lambda: db.insert(insert_sql(environment(), TABLE, COLUMNS, SOURCE, TARGET)),
                       lambda: db.count(TABLE, TARGET, source=False, lock=True), lambda: db._command("DELETE FROM X")):
            with self.assertRaises(BackfillError):
                action()
        cursor.execute.assert_not_called()

    def test_transaction_settings_and_lock(self):
        db, _, cursor, _ = make_client(execute=True)
        db.begin()
        settings = cursor.execute.call_args.args[0]
        self.assertIn("SET XACT_ABORT ON", settings)
        self.assertIn("SET LOCK_TIMEOUT 15000", settings)
        self.assertIn("BEGIN TRANSACTION", settings)
        cursor.fetchone.return_value = (0,)
        db.count(TABLE, TARGET, source=False, lock=True)
        self.assertIn("TABLOCKX, HOLDLOCK", cursor.execute.call_args.args[0])

    def test_insert_requires_transaction(self):
        db, _, cursor, _ = make_client(execute=True)
        with self.assertRaises(BackfillError):
            db.insert(insert_sql(environment(), TABLE, COLUMNS, SOURCE, TARGET))
        cursor.execute.assert_not_called()

    def test_commit_rejects_uncommittable_state(self):
        db, _, cursor, _ = make_client(execute=True)
        db.begin()
        cursor.fetchone.return_value = (-1, 1)
        with self.assertRaises(BackfillError):
            db.commit()
        self.assertFalse(any(call.args[0] == "COMMIT TRANSACTION;" for call in cursor.execute.call_args_list))

    def test_commit_transport_failure_outcome_unknown(self):
        db, _, cursor, _ = make_client(execute=True)
        db.begin()
        cursor.fetchone.return_value = (1, 1)
        cursor.execute.side_effect = [None, OSError("lost connection")]
        with self.assertRaises(CommitOutcomeUnknown):
            db.commit()

    def test_rollback_explicit_and_idempotent(self):
        db, _, cursor, _ = make_client(execute=True)
        db.begin()
        db.rollback()
        db.rollback()
        self.assertEqual(sum("ROLLBACK" in call.args[0] for call in cursor.execute.call_args_list), 1)
