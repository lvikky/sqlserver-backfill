from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import Audit, safe_error
from .config import ENVIRONMENTS, load_environment, load_table
from .errors import BackfillError, CommitOutcomeUnknown
from .executor import Plan, run
from .sqlserver_client import SqlServerClient
from .validator import dates

ROOT = Path(__file__).resolve().parents[2]


def display(plan: Plan, execute: bool) -> None:
    print("\nBACKFILL PLAN")
    print(f"Environment: {plan.environment.name.upper()} ({plan.environment.section})")
    print(f"Configured server: {plan.environment.server}")
    print(f"Table: {plan.table.qualified(plan.environment)}\nDate column: {plan.table.date_column}")
    print(f"Source date: {plan.source}\nTarget date: {plan.target}")
    print(f"Source rows: {plan.source_rows:,}\nExisting target rows: {plan.target_rows:,}")
    omitted = [c.name for c in plan.columns if c.server_generated or c.name in plan.table.exclude_columns
               or plan.table.regenerate.get(c.name) == "default"]
    print(f"Omitted/default/generated columns: {', '.join(omitted) or '(none)'}")
    print(f"Configured regeneration: {json.dumps(plan.table.regenerate)}")
    print(f"Additional source filters: {len(plan.table.source_filters)} (values hidden)")
    print(f"\nGenerated SQL (ODBC parameter markers):\n{plan.statement.sql}")
    print(f"Date parameters: target={plan.target}, source={plan.source}; additional filter values are bound separately.")
    print("MODE: EXECUTE — PENDING VALIDATION/CONFIRMATION" if execute else
          "MODE: DRY RUN\nNO DATA HAS BEEN MODIFIED.\nRun again with --execute to perform the backfill.")


def confirm_production(plan: Plan, *, input_fn=None, stdin=None) -> bool:
    stdin = sys.stdin if stdin is None else stdin
    print("\nWARNING: PRODUCTION BACKFILL")
    print(f"Environment: PROD\nTable: {plan.table.qualified(plan.environment)}")
    print(f"Source Date: {plan.source}\nTarget Date: {plan.target}\nRows Expected: {plan.source_rows:,}")
    if not stdin.isatty():
        raise BackfillError("Production execution requires an interactive terminal. No automation bypass is enabled.")
    try:
        return (input if input_fn is None else input_fn)("Type exactly EXECUTE PROD to continue: ") == "EXECUTE PROD"
    except EOFError:
        return False


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Controlled SQL Server date backfill; preview is the default.")
    result.add_argument("--environment", required=True, choices=tuple(ENVIRONMENTS))
    result.add_argument("--table", required=True, help="Approved table alias, e.g. banking_sfts")
    result.add_argument("--source-date", required=True)
    result.add_argument("--target-date", required=True)
    result.add_argument("--config", type=Path, required=True, help="External connection INI file")
    result.add_argument("--tables-config", type=Path, default=ROOT / "config" / "tables.toml")
    result.add_argument("--audit-log", type=Path, default=ROOT / "logs" / "backfill.jsonl")
    result.add_argument("--execute", action="store_true", help="Enable writes; PROD additionally requires typed confirmation")
    return result


def main(argv=None, *, client_factory=SqlServerClient) -> int:
    args = parser().parse_args(argv)
    audit = client = None
    try:
        audit = Audit(args.audit_log)
        audit.context.update(environment=args.environment, table_alias=args.table, source_date=args.source_date,
                             target_date=args.target_date, mode="execute" if args.execute else "dry_run",
                             source_row_count=None, target_row_count_before=None, affected_row_count=None,
                             target_row_count_after=None, query_ids=None)
        audit.emit("requested")
        source, target = dates(args.source_date, args.target_date)
        environment = load_environment(args.config, args.environment)
        table = load_table(args.tables_config, args.table, args.environment)
        audit.context.update(database=environment.database, schema=table.schema, table=table.name,
                             connection_section=environment.section)
        print(f"\nENVIRONMENT: {environment.name.upper()} | PROFILE: {environment.section}")
        client = client_factory(environment, execute_enabled=args.execute)
        plan = run(client, environment, table, source, target, execute=args.execute, audit=audit,
                   display=display, confirm=confirm_production)
        if args.execute:
            print(f"BACKFILL COMMITTED: {plan.source_rows:,} rows copied to {plan.target}.")
        print(f"Audit run ID: {audit.run_id}")
        return 0
    except KeyboardInterrupt:
        print("BACKFILL INTERRUPTED. Check the audit outcome before retrying.", file=sys.stderr)
        return 130
    except Exception as exc:
        detail = safe_error(exc)
        if audit:
            try:
                audit.emit("cli_error", **detail)
            except Exception:
                pass
        print(f"BACKFILL STOPPED: {detail['error']}", file=sys.stderr)
        if detail["sqlstate"]:
            print(f"SQLSTATE: {detail['sqlstate']}", file=sys.stderr)
        if audit:
            print(f"Audit run ID: {audit.run_id}", file=sys.stderr)
        return 3 if isinstance(exc, CommitOutcomeUnknown) else 1
    finally:
        if client:
            try:
                client.close()
            except Exception:
                print("Connection cleanup failed; inspect the recorded execution outcome.", file=sys.stderr)
        if audit:
            try:
                audit.close()
            except Exception:
                print("Audit stream cleanup failed.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
