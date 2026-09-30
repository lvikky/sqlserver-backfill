from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from .audit import safe_error
from .config import Environment, Table
from .errors import BackfillError, CommitOutcomeUnknown
from .metadata import Column
from .sql_generator import Statement, insert_sql
from .validator import counts


@dataclass(frozen=True)
class Plan:
    environment: Environment
    table: Table
    source: date
    target: date
    columns: tuple[Column, ...]
    source_rows: int
    target_rows: int
    statement: Statement


def prepare(client, environment: Environment, table: Table, source: date, target: date,
            audit, *, lock: bool = False) -> Plan:
    if source == target:
        raise BackfillError("Source and target dates must differ.")
    client.verify_environment()
    # The initial count acquires the transaction-wide table lock before re-reading metadata.
    target_rows = client.count(table, target, source=False, lock=True) if lock else None
    columns = client.metadata(table)
    if target_rows is None:
        target_rows = client.count(table, target, source=False)
    source_rows = client.count(table, source, source=True)
    audit.context.update(source_row_count=source_rows, target_row_count_before=target_rows,
                         session_id=client.session_id)
    audit.emit("validation", locked=lock)
    counts(source_rows, target_rows)
    return Plan(environment, table, source, target, columns, source_rows, target_rows,
                insert_sql(environment, table, columns, source, target))


def run(client, environment: Environment, table: Table, source: date, target: date,
        *, execute: bool, audit, display: Callable, confirm: Callable) -> Plan:
    committed = False
    outcome_unknown = False
    try:
        plan = prepare(client, environment, table, source, target, audit)
        display(plan, execute)
        audit.emit("plan_ready", status="preview" if not execute else "awaiting_execution")
        if not execute:
            audit.emit("completed", status="dry_run_success", affected_row_count=0,
                       target_row_count_after=plan.target_rows)
            return plan
        if environment.name == "prod" and not confirm(plan):
            raise BackfillError("Production confirmation was not supplied. No data modified.")
        client.begin()
        current = prepare(client, environment, table, source, target, audit, lock=True)
        if current != plan:
            raise BackfillError("The plan changed after preview/confirmation. Run again to review the new plan.")
        audit.emit("execution_started", status="transaction_open")
        affected = client.insert(current.statement)
        after = client.count(table, target, source=False)
        audit.context.update(affected_row_count=affected, target_row_count_after=after)
        if (affected is not None and affected != current.source_rows) or after != current.source_rows:
            raise BackfillError("Post-insert row-count verification failed.")
        audit.emit("commit_requested", status="validated_pending_commit")
        client.commit()
        committed = True
        try:
            audit.emit("completed", status="committed")
        except Exception:
            raise BackfillError("Backfill COMMITTED, but completion audit failed. Reconcile logs; do not rerun blindly.") from None
        return current
    except BaseException as exc:
        outcome_unknown = isinstance(exc, CommitOutcomeUnknown)
        rollback_failed = False
        if not committed:
            try:
                client.rollback()
            except BaseException:
                rollback_failed = True
        status = ("commit_outcome_unknown" if outcome_unknown else "committed_audit_failed" if committed
                  else "rollback_unconfirmed" if rollback_failed else "aborted")
        try:
            audit.emit("failed", status=status, **safe_error(exc))
        except Exception:
            pass
        if rollback_failed and not outcome_unknown:
            raise BackfillError("Rollback could not be confirmed. Connection will close; reconcile the target date before retrying.") from None
        raise
