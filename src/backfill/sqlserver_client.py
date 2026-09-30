from __future__ import annotations

from .config import Environment, Table, quote
from .errors import BackfillError, CommitOutcomeUnknown
from .metadata import Column, validate
from .sql_generator import Statement, count_sql


class SqlServerClient:
    """Dedicated connection. Preview has no public write capability."""

    def __init__(self, environment: Environment, *, execute_enabled: bool, connector=None):
        self.environment = environment
        self.execute_enabled = execute_enabled
        self.in_transaction = False
        self.session_id = None
        self.server_name = None
        if connector is None:
            try:
                import pyodbc
            except ImportError:
                raise BackfillError("Install requirements.txt, an ODBC manager (unixODBC on macOS/Linux), and the Microsoft SQL Server ODBC driver.") from None
            connector = pyodbc.connect
        # Autocommit applies only to SELECTs and explicit BEGIN/COMMIT transactions.
        # We never use pyodbc's connection context manager (implicit commit).
        self.connection = connector(environment.connection_string(), autocommit=True, timeout=15)
        try:
            self.connection.timeout = 60
        except BaseException:
            self.connection.close()
            raise

    def _query(self, statement: Statement, *, many: bool = False):
        cursor = self.connection.cursor()
        try:
            cursor.execute(statement.sql, *statement.parameters)
            return cursor.fetchall() if many else cursor.fetchone()
        finally:
            cursor.close()

    def _command(self, sql: str) -> None:
        if not self.execute_enabled:
            raise BackfillError("Database modifications are disabled in dry-run mode.")
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
        finally:
            cursor.close()

    def verify_environment(self) -> None:
        row = self._query(Statement("SELECT DB_NAME(), CONVERT(nvarchar(128), SERVERPROPERTY('ServerName')), "
                                    "@@SPID, CONVERT(int, SERVERPROPERTY('ProductMajorVersion')), "
                                    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION');"))
        if not row or row[0] != self.environment.database:
            raise BackfillError("Connected database does not match the selected environment.")
        if row[3] is None or int(row[3]) < 13:
            raise BackfillError("This adapter requires SQL Server 2016 or later.")
        if row[4] != 1:
            raise BackfillError("Database VIEW DEFINITION is required for complete safety metadata.")
        self.server_name, self.session_id = str(row[1]), int(row[2])

    def metadata(self, table: Table) -> tuple[Column, ...]:
        info = self._query(Statement("""
SELECT t.object_id, t.is_memory_optimized, t.temporal_type, t.is_filetable,
       (SELECT COUNT(*) FROM sys.triggers tr WHERE tr.parent_id = t.object_id AND tr.is_disabled = 0),
       (SELECT COUNT(*) FROM sys.security_predicates p
        JOIN sys.security_policies pol ON pol.object_id = p.object_id
        WHERE p.target_object_id = t.object_id AND pol.is_enabled = 1),
       (SELECT COUNT(*) FROM sys.external_tables e WHERE e.object_id = t.object_id)
FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name = ? AND t.name = ? AND t.is_ms_shipped = 0;
""", (table.schema, table.name)))
        if info is None:
            raise BackfillError("Approved table is missing or not visible in the selected database.")
        if any(info[i] for i in range(1, 7)):
            raise BackfillError("Memory-optimized, temporal, file, external, trigger-enabled, or RLS tables require a reviewed adapter.")
        object_name = f"{quote(table.schema)}.{quote(table.name)}"
        permissions = self._query(Statement(
            "SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'SELECT'), HAS_PERMS_BY_NAME(?, 'OBJECT', 'INSERT');",
            (object_name, object_name)))
        if permissions is None or permissions[0] != 1 or (self.execute_enabled and permissions[1] != 1):
            raise BackfillError("Required table SELECT/INSERT permissions are missing.")
        rows = self._query(Statement("""
SELECT c.name, c.column_id, COALESCE(base_ty.name, ty.name), c.is_nullable,
       CONVERT(bit, CASE WHEN c.default_object_id <> 0 THEN 1 ELSE 0 END),
       c.is_identity, c.is_computed, c.generated_always_type, c.is_hidden,
       CONVERT(bit, CASE WHEN c.encryption_type IS NOT NULL THEN 1 ELSE 0 END),
       c.is_column_set,
       CONVERT(bit, CASE WHEN EXISTS (SELECT 1 FROM sys.masked_columns m
                     WHERE m.object_id = c.object_id AND m.column_id = c.column_id AND m.is_masked = 1)
                        THEN 1 ELSE 0 END)
FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.types base_ty ON base_ty.user_type_id = c.system_type_id
WHERE c.object_id = ? ORDER BY c.column_id;
""", (info[0],)), many=True)
        return validate(tuple(Column(str(r[0]), int(r[1]), str(r[2]), *(bool(v) for v in r[3:]))
                              for r in rows), table)

    def count(self, table: Table, day, *, source: bool, lock: bool = False) -> int:
        if lock and (not self.execute_enabled or not self.in_transaction):
            raise BackfillError("An execution transaction is required to lock the table.")
        row = self._query(count_sql(self.environment, table, day, source=source, lock=lock))
        if row is None:
            raise BackfillError("No count was returned by SQL Server.")
        return int(row[0])

    def begin(self) -> None:
        if self.in_transaction:
            raise BackfillError("A transaction is already active.")
        # Mark first so a partially successful BEGIN is still rolled back on error.
        if not self.execute_enabled:
            raise BackfillError("Database modifications are disabled in dry-run mode.")
        self.in_transaction = True
        self._command("SET IMPLICIT_TRANSACTIONS OFF; SET XACT_ABORT ON; SET LOCK_TIMEOUT 15000; "
                      "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE; BEGIN TRANSACTION;")

    def insert(self, statement: Statement) -> int | None:
        if not self.execute_enabled or not self.in_transaction:
            raise BackfillError("INSERT requires --execute and an active transaction.")
        cursor = self.connection.cursor()
        try:
            cursor.execute(statement.sql, *statement.parameters)
            count = cursor.rowcount
            return int(count) if count is not None and count >= 0 else None
        finally:
            cursor.close()

    def commit(self) -> None:
        state = self._query(Statement("SELECT XACT_STATE(), @@TRANCOUNT;"))
        if not self.in_transaction or state is None or tuple(state) != (1, 1):
            raise BackfillError("Transaction is not committable or its nesting changed.")
        try:
            self._command("COMMIT TRANSACTION;")
        except BaseException:
            raise CommitOutcomeUnknown("Commit acknowledgement failed. Outcome UNKNOWN; reconcile the target date before retrying.") from None
        self.in_transaction = False

    def rollback(self) -> None:
        if self.in_transaction:
            self._command("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;")
            self.in_transaction = False

    def close(self) -> None:
        self.connection.close()
