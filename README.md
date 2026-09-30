# Controlled SQL Server backfill

Python 3.13 utility that copies rows from one business date into the same approved
table for another date. Preview is always the default. Database writes require
`--execute`; production additionally requires typing exactly `EXECUTE PROD` in an
interactive terminal.

This implementation targets **Microsoft SQL Server**, following the supplied
connection code and SQL screenshots. It does not use Snowflake drivers or syntax.
The first approved alias is `banking_sfts`, resolving to
`[<environment DATABASE>].[LST_SF_EXTRACT].[BANKING_SFTS]`. No server or database
names have been guessed. `LIQUIDITY` from the screenshots is not hard-coded.

## Setup

From this project directory, using Python 3.13:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python backfill.py --help
python -m unittest discover -s tests -v
```

On Windows, use `py -3.13 -m venv .venv` and activate with
`.venv\Scripts\Activate.ps1` in PowerShell. You can invoke
`.venv\Scripts\python.exe` directly without activation.

Install Microsoft ODBC Driver 18 for SQL Server (17 is also accepted) on the
execution machine. Installing `pyodbc` alone does not install this system driver.
macOS/Linux also need an ODBC driver manager such as unixODBC. Follow Microsoft's
[ODBC installation instructions](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server).
Integrated authentication uses the execution identity; on macOS/Linux this needs
your organization's Kerberos setup. Do not weaken TLS to work around deployment
configuration issues.

Tests are offline and need neither pyodbc nor a database. On the development
machine, Python 3.13.7 is available and the pyodbc 5.3.0 wheel installs, but loading
the driver currently fails because the system unixODBC library is absent.
No live SQL Server connection or database modification was performed during development.

## Connection configuration and environment resolution

Copy `config/connections.example.ini` to an external, access-controlled file and
replace the placeholders. The application requires all three environment sections
so it can reject duplicated server/database pairs. It opens **only** the selected
environment's connection. Other sections in an existing application INI are ignored.
Unsupported keys within these three sections are rejected; use a dedicated INI if
your existing sections also hold unrelated settings.

| CLI environment | Exact INI section |
| --- | --- |
| `dev` | `[SQLSERVER_DEV]` |
| `uat` | `[SQLSERVER_UAT]` |
| `prod` | `[SQLSERVER_PRD]` |

No `prd`, uppercase environment names, default environment, or fallback profile is
accepted. `prod` deliberately maps to `PRD`, matching your existing comment.

Each section has this shape (placeholders are intentionally not usable):

```ini
[SQLSERVER_PRD]
DRIVER = {ODBC Driver 18 for SQL Server}
SERVER = REPLACE_PRD_SERVER
DATABASE = REPLACE_PRD_DATABASE
Trusted_Connection = yes
Encrypt = yes
TrustServerCertificate = no
```

The server and database come solely from the selected section. The resolved
database is also used in every table reference and checked against `DB_NAME()`
after connection and again during execution. Connection settings are not inherited
from `[DEFAULT]`. Two environments cannot have the same configured server/database
pair. Schemas and table overrides cannot change the server/database.

Configuration still defines the trust boundary: software cannot infer that a
server labeled DEV is actually your production server. Distinct DNS aliases for
one physical server are not detected by the duplicate-config check. Review profile
assignments and use credentials whose database permissions enforce environment
isolation. Restrict write access to the allowlist and connection configuration.

Connection-string values are ODBC-brace escaped. No raw connection string is
printed or logged. Integrated authentication follows the supplied application
pattern. Optional SQL authentication uses **references** to environment variables:

```ini
Trusted_Connection = no
username_env = BACKFILL_PRD_USERNAME
password_env = BACKFILL_PRD_PASSWORD
```

Keep these lines in the corresponding section, replacing `Trusted_Connection=yes`.
Set actual values via an enterprise secret manager/process environment; never put
them in the INI, table configuration, CLI arguments, or repository. `.env.example`
lists variable names only; `.env` files are not automatically loaded. The parser
does not interpolate `%` characters.

## Table allowlist

`config/tables.toml` contains the table supplied in your screenshots:

```toml
[tables.banking_sfts]
schema = "LST_SF_EXTRACT"
table = "BANKING_SFTS"
date_column = "AS_OF_DATE"
exclude_columns = []
```

TOML uses Python's standard-library parser, eliminating a YAML runtime dependency.
It serves the same configuration-driven allowlist role. Use `--tables-config` for
a different trusted allowlist. Only approved aliases are accepted; a fully qualified
name passed to `--table` is rejected. Add the remaining approved tables once their
actual identifiers and policies are known; there are no invented aliases.

Identifiers in configuration must be simple unquoted names of at most 128
characters, starting with a letter or underscore. Every identifier is bracket
quoted. Actual column names come from metadata and are safely quoted even if they
contain `]`. Configured column spelling must match metadata exactly.

Optional policies are demonstrated in `config/table-policies.example.toml`:

| Policy | Behavior |
| --- | --- |
| `exclude_columns` | Omit the column; allow only nullable, defaulted, or server-generated columns. |
| `regenerate.COLUMN = "default"` | Omit the column so SQL Server applies its existing default/sequence/identity behavior. |
| `regenerate.COLUMN = "utc_now"` | Use `SYSUTCDATETIME()` for an approved datetime column. |
| `regenerate.COLUMN = "new_uuid"` | Use `NEWID()` for an approved uniqueidentifier column. |
| `source_filters` | AND together typed, parameter-bound source predicates. |
| `overrides.dev/uat/prod` | Override only `schema`, `table`, or `date_column`. |

Filters support `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `is_null`, and `is_not_null`.
No raw SQL expression or raw WHERE clause is accepted. **Target-date checks always
cover the entire date**, even when source filters restrict the copied rows.
Filter values are bound but omitted from console/audit output to avoid logging
sensitive business values. Review the trusted allowlist for their exact values.

Metadata comes from `sys.tables`, `sys.schemas`, `sys.columns`, and `sys.types`,
ordered by `column_id`. Both INSERT and SELECT use explicit column lists.
Identity, computed, generated-always, and rowversion columns are omitted so SQL
Server handles them. Normal columns with defaults are still copied unless a policy
explicitly requests regeneration. `DT_EL_EXECUTIONTIMESTAMP` is copied unchanged
with the supplied allowlist, just like the sample SQL.

## Commands

Replace `/secure/backfill-connections.ini` with your external config path. These
examples all use the approved alias and the dates supplied in your request.
The Bash line continuation below is `\`; in PowerShell use a single line instead.

DEV dry-run:

```bash
python backfill.py --config /secure/backfill-connections.ini \
  --environment dev --table banking_sfts \
  --source-date 2026-09-18 --target-date 2026-09-21
```

UAT dry-run:

```bash
python backfill.py --config /secure/backfill-connections.ini \
  --environment uat --table banking_sfts \
  --source-date 2026-09-18 --target-date 2026-09-21
```

PROD dry-run:

```bash
python backfill.py --config /secure/backfill-connections.ini \
  --environment prod --table banking_sfts \
  --source-date 2026-09-18 --target-date 2026-09-21
```

PROD execution:

```bash
python backfill.py --config /secure/backfill-connections.ini \
  --environment prod --table banking_sfts \
  --source-date 2026-09-18 --target-date 2026-09-21 --execute
```

Review the displayed environment, profile, server, table, dates, row count,
omitted columns, regeneration policies, and generated SQL. Then type exactly:

```text
EXECUTE PROD
```

Enter, `yes`, different capitalization, trailing spaces, and piped input are not
accepted. DEV/UAT execution requires `--execute` but does not prompt. No `--yes`,
environment-variable override, or non-interactive PROD bypass exists. A future
automation adapter can implement the executor's confirmation interface with a
reviewed authorization mechanism bound to the exact plan; the current CLI always
uses interactive confirmation for production.

Typical preview (illustrative count/database, not a live result):

```text
BACKFILL PLAN
Environment: PROD (SQLSERVER_PRD)
Configured server: <your configured server>
Table: [<your configured database>].[LST_SF_EXTRACT].[BANKING_SFTS]
Date column: AS_OF_DATE
Source date: 2026-09-18
Target date: 2026-09-21
Source rows: 18,542
Existing target rows: 0
...
MODE: DRY RUN
NO DATA HAS BEEN MODIFIED.
Run again with --execute to perform the backfill.
```

The actual plan lists every applicable column retrieved from the database.
For a hypothetical three-column metadata result, generated SQL is:

```sql
INSERT INTO [<configured database>].[LST_SF_EXTRACT].[BANKING_SFTS] (
    [ENTITY],
    [AS_OF_DATE],
    [DT_EL_EXECUTIONTIMESTAMP]
)
SELECT
    [ENTITY],
    CAST(? AS date) AS [AS_OF_DATE],
    [DT_EL_EXECUTIONTIMESTAMP]
FROM [<configured database>].[LST_SF_EXTRACT].[BANKING_SFTS]
WHERE [AS_OF_DATE] = ?;
```

The ODBC parameter tuple is `(date(2026, 9, 21), date(2026, 9, 18))`. Values are
not interpolated into SQL. `?` markers are executed by pyodbc, not directly by SSMS.
No INSERT is submitted during preview.

## Transaction and duplicate protection

1. Validate arguments, configuration, connected database, permissions, table
   features, metadata, source count, and the entire target-date count.
2. Show the plan. Reject a missing source or any existing target row. Require PROD
   confirmation before starting a transaction or holding an execution lock.
3. Enable `XACT_ABORT`, set a 15-second lock timeout, start an explicit transaction,
   and acquire `TABLOCKX, HOLDLOCK` through the target-date count.
4. Under the lock, repeat environment/metadata/count validation. Abort if the plan
   changed since confirmation. Ordinary writers to this table now wait.
5. Execute the parameterized INSERT once. Compare the ODBC affected-row count when
   available and the post-insert target count with the locked source count.
6. Flush a durable `commit_requested` audit event, check `XACT_STATE()` and
   `@@TRANCOUNT`, then explicitly COMMIT. Record the committed outcome.
7. Roll back on validation errors, insert failures, lock timeout, cancellation,
   or pre-commit audit failure. Close the dedicated connection on every CLI path.

This deliberately uses a **whole-table exclusive lock** for strong, simple
coordination with other writers without deploying a separate lock service or
requiring a particular index. Other writes, and some readers, may wait for the
transaction. Login timeout is 15 seconds; each statement has a 60-second timeout.
Large tables may require a reviewed timeout/indexing strategy; no partial work is
committed just to bypass a timeout.

A second concurrent utility run waits or times out. After the first commits, a
successful waiter sees the populated target and aborts. A later repeat also
aborts. There is no automatic append, delete, overwrite, truncate, or retry.
Other applications can still insert duplicates after this utility releases its
lock; a global invariant requires shared writer rules or suitable database
constraints. The tool cannot control future writes by unrelated applications.

If the COMMIT acknowledgement is lost, the result is **UNKNOWN**, not falsely
reported as rolled back. Exit code 3 and the audit record require reconciliation
before retrying. If audit logging fails after a known successful commit, the CLI
explicitly says **COMMITTED** and does not attempt to undo it. A process crash after
`commit_requested` can similarly require operator reconciliation. Identity and
sequence counters can have gaps even when an INSERT transaction rolls back.

## Supported schema and operational limits

- SQL Server 2016 or later, ordinary local disk-based tables.
- The business date column must currently be SQL `DATE`, verified from metadata.
  The screenshots do not establish its actual type. `datetime`, `datetime2`, text,
  and timezone-bearing business-date columns fail closed until a specific equality,
  whole-day, and time-of-day preservation policy is agreed and implemented.
- Triggers, active row-level security, temporal tables, memory-optimized tables,
  external tables, FileTables, masked/encrypted columns, column-set columns, and
  unsupported hidden columns are rejected. They need separate review so triggers,
  hidden rows, or transformation rules cannot silently invalidate the copy.
- Enabled triggers are rejected even if they are not INSERT triggers; the policy
  is intentionally conservative. Normal constraints remain enabled. Unique-key
  collisions or other constraint failures roll back the operation.
- All columns/configuration policies are validated before generating the INSERT.
  Preflight and metadata checks necessarily issue SELECTs to discover database state.
- No source/target record data is fetched into Python or logged; counts and metadata
  are used. The copy executes entirely within SQL Server.
- Preview counts are advisory because no long-lived execution lock is held in
  dry-run. Execution uses fresh locked counts rather than trusting a prior preview.

Use a principal with database `VIEW DEFINITION` for complete safety metadata and
`SELECT` on approved tables. Execution additionally requires `INSERT` on those
tables and any permissions needed by explicitly selected existing defaults.
Neither ownership, sysadmin, DDL, DELETE, nor UPDATE permissions are required.
A separate SELECT-only principal for preview provides another enforcement layer.

## Audit and troubleshooting

JSON Lines default to `logs/backfill.jsonl`; use `--audit-log` to choose a durable
protected location. New files use restrictive OS permissions where supported.
Protect and rotate the file through your deployment's logging system.

Events include UTC timestamp, unique run ID, environment, connection section,
database/schema/table, dates, source/target counts, affected count when available,
mode, status, and SQL Server session ID. Counts that cannot yet be obtained are
null. SQL Server/ODBC does not expose Snowflake-style query IDs, so `query_ids` is
null; the run ID and session ID support correlation. No DMV privileges are needed.
The `Audit.emit()` interface can later be replaced by a central audit service/table.

Driver exceptions are sanitized: only error class and a validated SQLSTATE are
logged, never the raw message or connection string, because messages can include
credentials or business values. Curated validation reasons remain readable. An
operator can use the session/run ID with DBA-side diagnostics. Failure to open an
audit sink prevents connecting; failures before commit prevent commit.

Exit codes: `0` successful preview or committed execution, `1` validation/runtime
failure (read the explicit outcome), `2` argument syntax error, `3` unknown commit
outcome, `130` interrupted execution. Argument-parser errors occur before audit
initialization. A successful count check is not a substitute for live DEV validation.

## Architecture and project structure

```text
sqlserver-backfill/
├── backfill.py                       # Source-checkout CLI entry point
├── src/backfill/
│   ├── main.py                       # CLI, plan display, exact PROD confirmation
│   ├── config.py                     # INI/TOML parsing, environment/table allowlists
│   ├── validator.py                  # ISO dates and row-count preconditions
│   ├── metadata.py                   # Column model and copy-policy validation
│   ├── sql_generator.py              # Quoted SQL and separate value bindings
│   ├── sqlserver_client.py            # pyodbc, catalog access, lock/transaction control
│   ├── executor.py                   # Preview/revalidation/insert/verify/commit workflow
│   ├── audit.py                      # JSONL events and secret-safe errors
│   └── errors.py                     # Safe errors and unknown commit outcome
├── config/
│   ├── connections.example.ini       # Full DEV/UAT/PRD external-INI template
│   ├── tables.toml                   # Active allowlist: banking_sfts
│   └── table-policies.example.toml   # Optional filters/regeneration/override examples
├── tests/                           # Offline unittest coverage
├── DEV_VALIDATION.md                 # Live validation checklist for your DEV environment
├── .env.example
├── requirements.txt
└── pyproject.toml
```

Pure configuration/validation/SQL generation is separated from database I/O.
The executor accepts an injected client, audit sink, display callback, and
confirmation callback, enabling realistic failure-path tests without database
access. All environment switching is configuration-driven, except the deliberate
PROD confirmation policy. No environment-specific SQL branches are needed.

Use the source-checkout CLI shown above. If you install the package and use the
`sqlserver-backfill` console entry point, supply explicit `--tables-config` and
`--audit-log` paths; configuration files are deployed separately from the wheel.

## Validation performed and next deployment step

The offline suite covers DEV/UAT/PROD resolution, missing/invalid settings,
environment isolation, SQL bindings, metadata, exclusions/regeneration, dry-run
write guards, production confirmation, race/repeat protection, verification
failures, rollback, commit ambiguity, audit failures, and an end-to-end mocked CLI.

The implementation has not been exercised against a live SQL Server. Supply the
actual INI, install the system driver on the execution host, and follow
`DEV_VALIDATION.md` before operational rollout. The utility will verify the actual
`AS_OF_DATE` type, table features, and permissions on its first dry-run.

Technical references:

- [SQL Server table hints](https://learn.microsoft.com/en-us/sql/t-sql/queries/hints-transact-sql-table)
  documents `TABLOCKX`/`HOLDLOCK` behavior and blocking implications.
- [sys.columns](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-columns-transact-sql)
  documents identity, computed, generated, hidden, and encrypted-column metadata.
- [SET XACT_ABORT](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-xact-abort-transact-sql)
  describes transaction behavior on runtime errors.
- [pyodbc connection API](https://github.com/mkleehammer/pyodbc/wiki/Connection)
  describes ODBC connection, timeout, and autocommit behavior.
