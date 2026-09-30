import unittest
from dataclasses import replace

from tests.support import COLUMNS, SOURCE, TABLE, TARGET, environment
from backfill.config import SourceFilter
from backfill.errors import BackfillError
from backfill.metadata import Column, validate
from backfill.sql_generator import count_sql, insert_sql


class SqlTests(unittest.TestCase):
    def test_explicit_columns_order_and_date_replacement(self):
        statement = insert_sql(environment(), TABLE, tuple(reversed(COLUMNS)), SOURCE, TARGET)
        self.assertIn("INSERT INTO [TEST_DEV].[LST_SF_EXTRACT].[BANKING_SFTS]", statement.sql)
        self.assertIn("[ENTITY],\n    [AS_OF_DATE],\n    [DT_EL_EXECUTIONTIMESTAMP]", statement.sql)
        self.assertIn("CAST(? AS date) AS [AS_OF_DATE]", statement.sql)
        self.assertIn("WHERE [AS_OF_DATE] = ?;", statement.sql)
        self.assertNotIn("SELECT *", statement.sql)
        self.assertNotIn("2026-09", statement.sql)
        self.assertEqual(statement.parameters, (TARGET, SOURCE))

    def test_execution_timestamp_copied_by_default(self):
        sql = insert_sql(environment(), TABLE, COLUMNS, SOURCE, TARGET).sql
        self.assertEqual(sql.count("[DT_EL_EXECUTIONTIMESTAMP]"), 2)
        self.assertNotIn("SYSUTCDATETIME", sql)

    def test_identity_computed_rowversion_omitted(self):
        columns = COLUMNS + (Column("ID", 4, "int", identity=True),
                             Column("COMPUTED", 5, "int", computed=True), Column("VERSION", 6, "timestamp"))
        sql = insert_sql(environment(), TABLE, columns, SOURCE, TARGET).sql
        for omitted in ("[ID]", "[COMPUTED]", "[VERSION]"):
            self.assertNotIn(omitted, sql)

    def test_excluded_nullable_and_default_columns(self):
        columns = COLUMNS + (Column("OPTIONAL", 4, "int", nullable=True),
                             Column("SEQ", 5, "int", has_default=True))
        table = replace(TABLE, exclude_columns=("OPTIONAL",), regenerate={"SEQ": "default"})
        sql = insert_sql(environment(), table, columns, SOURCE, TARGET).sql
        self.assertNotIn("[OPTIONAL]", sql)
        self.assertNotIn("[SEQ]", sql)

    def test_generated_expressions_are_allowlisted(self):
        table = replace(TABLE, regenerate={"DT_EL_EXECUTIONTIMESTAMP": "utc_now", "ROW_ID": "new_uuid"})
        columns = COLUMNS + (Column("ROW_ID", 4, "uniqueidentifier"),)
        sql = insert_sql(environment(), table, columns, SOURCE, TARGET).sql
        self.assertIn("SYSUTCDATETIME() AS [DT_EL_EXECUTIONTIMESTAMP]", sql)
        self.assertIn("NEWID() AS [ROW_ID]", sql)

    def test_missing_date_column_and_other_policy_column(self):
        for table, columns in ((TABLE, (COLUMNS[0],)), (replace(TABLE, exclude_columns=("MISSING",)), COLUMNS)):
            with self.assertRaises(BackfillError):
                validate(columns, table)

    def test_unsafe_metadata_rejected(self):
        for col in (replace(COLUMNS[1], data_type="datetime2"), replace(COLUMNS[1], computed=True),
                    replace(COLUMNS[1], masked=True), replace(COLUMNS[1], encrypted=True),
                    replace(COLUMNS[1], hidden=True), replace(COLUMNS[1], column_set=True)):
            with self.subTest(column=col), self.assertRaises(BackfillError):
                validate((COLUMNS[0], col), TABLE)

    def test_cannot_omit_required_column(self):
        with self.assertRaisesRegex(BackfillError, "required"):
            validate(COLUMNS, replace(TABLE, exclude_columns=("ENTITY",)))

    def test_metadata_column_identifiers_escaped(self):
        columns = COLUMNS + (Column("odd]name", 4, "varchar"),)
        self.assertIn("[odd]]name]", insert_sql(environment(), TABLE, columns, SOURCE, TARGET).sql)

    def test_source_filters_bound_target_count_unfiltered(self):
        value = "x'; DROP TABLE data;--"
        table = replace(TABLE, source_filters=(SourceFilter("ENTITY", "eq", value),))
        statement = insert_sql(environment(), table, COLUMNS, SOURCE, TARGET)
        self.assertNotIn(value, statement.sql)
        self.assertEqual(statement.parameters, (TARGET, SOURCE, value))
        source = count_sql(environment(), table, SOURCE, source=True)
        target = count_sql(environment(), table, TARGET, source=False)
        self.assertIn("[ENTITY] = ?", source.sql)
        self.assertNotIn("[ENTITY]", target.sql)
        self.assertEqual(target.parameters, (TARGET,))

    def test_null_filters(self):
        table = replace(TABLE, source_filters=(SourceFilter("ENTITY", "is_null"),))
        statement = insert_sql(environment(), table, COLUMNS, SOURCE, TARGET)
        self.assertIn("[ENTITY] IS NULL", statement.sql)
        self.assertEqual(statement.parameters, (TARGET, SOURCE))

    def test_lock_only_on_requested_count(self):
        self.assertIn("WITH (TABLOCKX, HOLDLOCK)", count_sql(environment(), TABLE, TARGET, source=False, lock=True).sql)
        self.assertNotIn("TABLOCKX", count_sql(environment(), TABLE, TARGET, source=False).sql)

    def test_wrong_regeneration_type(self):
        with self.assertRaises(BackfillError):
            validate(COLUMNS, replace(TABLE, regenerate={"ENTITY": "utc_now"}))
