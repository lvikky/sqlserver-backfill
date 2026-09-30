# Live DEV validation before rollout

No live database was available for development testing. The unit suite uses mocks;
it cannot prove the deployed driver's behavior, permissions, schema compatibility,
or SQL Server's actual locking behavior in your environment.

Perform these checks on an approved disposable DEV table with a trusted allowlist
entry and environment-scoped credentials. Do not use PROD to validate the tool.
Fixture creation and cleanup should follow your existing database change process;
the CLI intentionally provides no cleanup/delete capability.

1. Install Python 3.13, the pinned requirements, the ODBC manager where required,
   and the Microsoft SQL Server ODBC driver. Verify integrated authentication or
   environment-supplied SQL credentials and certificate trust.
2. Supply real DEV/UAT/PRD sections in the external INI. Review server/database
   assignments independently. Confirm the DEV principal cannot write to PROD.
3. Run the utility with `--environment dev` and no `--execute`. Confirm its profile,
   database, schema, and table match the intended DEV fixture. The first approved
   `banking_sfts` mapping uses your supplied schema/table and a metadata-verified
   DATE column; if this is actually DATETIME/text, stop and define the date policy.
4. Use fixture rows for `2026-09-18` and an empty `2026-09-21`. Compare the displayed
   count and explicit columns with a trusted SQL client. Verify excluded/identity/
   computed/default behavior and that ordinary execution timestamps are preserved.
5. Confirm dry-run leaves table contents unchanged and works with SELECT plus
   VIEW DEFINITION permissions, with no INSERT permission.
6. Run with DEV `--execute`; independently verify target count and all copied
   applicable values (including NULLs and duplicate source rows). Source rows
   must be unchanged. Review audit `commit_requested` and `committed` events.
7. Repeat the same command. It must fail without inserting additional rows.
8. Test an absent source, existing target, equal dates, unapproved alias, invalid
   config, required-column omission, and a constraint violation. Verify all fail
   and leave no target backfill rows committed.
9. On another disposable target date, run two DEV executions concurrently. At
   most one should commit; the other must see existing data or time out. Use an
   independent session to hold an incompatible lock and verify the 15-second lock
   timeout leaves no partial target rows. Also confirm the table-wide blocking
   window is operationally acceptable for your expected row counts.
10. Test a controlled interruption before commit and verify rollback/released
    locks. Treat connection loss around COMMIT as unknown; reconcile from the
    database and audit rather than relying solely on exit status or retrying.
11. Verify masked/RLS/trigger-enabled or otherwise unsupported fixtures are
    rejected. Review the policy before adding support for these features.
12. Confirm audit retention, file permissions, failure reporting, and log delivery.
    Only then move through your normal UAT/production deployment process.

PROD's exact interactive confirmation and refusal of piped confirmation are covered
offline. Testing those guards does not require running against a production database.
