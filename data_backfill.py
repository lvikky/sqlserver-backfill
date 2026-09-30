#!/usr/bin/env python3

import argparse
import configparser
import datetime as dt
import logging
import os
import sys
import traceback

APPROVED_TABLES = [
    "BANKING_COMMERCIAL_PAPER",
    "BANKING_DEPOSITS",
    "BANKING_LOANS",
    "BANKING_SFTS",
    "CM_DERIVS_CASHFLOWS",
    "CM_NOTES",
    "CM_SFTS",
    "CM_SLEEPER_COLLATERAL",
    "CM_TENDER_OPTION_BONDS",
    "CM_TOTAL_RETURN_SWAPS",
    "DERIVS_COLLATERAL",
    "NET_LIABILITIES",
]

DATABASE_NAME = "LIQUIDITY"
SCHEMA_NAME = "LST_SF_EXTRACT"
DEFAULT_DATE_COLUMN = "AS_OF_DATE"


def setup_logger():
    logger = logging.getLogger("data-backfill")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if not logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG)
        logger.addHandler(console_handler)
    return logger


logger = setup_logger()


def get_db_connect_str(config_file, environment):
    import pyodbc

    logger.info("Reading DB config from %s for environment %s", config_file, environment)

    config = configparser.ConfigParser()
    config.read(config_file)

    section = f"SQLSERVER_{environment.upper()}"
    if section not in config:
        raise Exception(f"Section '{section}' not found in config file: {config_file}")

    db = config[section]
    configured_driver = db["DRIVER"]
    server = db["SERVER"]
    database = db["DATABASE"]
    trusted_conn = db.get("Trusted_Connection", "yes")
    encrypt = db.get("Encrypt", "no")
    trust_server_certificate = db.get("TrustServerCertificate", "yes")

    installed_drivers = pyodbc.drivers()
    configured_driver_name = configured_driver.strip().strip("{}")
    installed_driver_map = {d.lower(): d for d in installed_drivers}

    resolved_driver = installed_driver_map.get(configured_driver_name.lower())

    if resolved_driver is None:
        for candidate in [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "SQL Server",
        ]:
            if candidate.lower() in installed_driver_map:
                resolved_driver = installed_driver_map[candidate.lower()]
                logger.warning(
                    "Configured driver '%s' not found. Falling back to '%s'.",
                    configured_driver,
                    resolved_driver,
                )
                break

    if resolved_driver is None:
        raise Exception(
            "No supported SQL Server ODBC driver found. Installed drivers: "
            + str(installed_drivers)
        )

    connstr = (
        f"Driver={{{resolved_driver}}};"
        f"Server={server};"
        f"Database={database};"
        f"Trusted_Connection={trusted_conn};"
        f"Encrypt={encrypt};"
        f"TrustServerCertificate={trust_server_certificate};"
    )
    return connstr


def parse_date_value(value, name):
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{name} must be in YYYY-MM-DD format: {value}"
        ) from exc


def table_full_name(table_name):
    return f"{DATABASE_NAME}.{SCHEMA_NAME}.{table_name}"


def get_date_column_for_table(table_name):
    if table_name == "NET_LIABILITIES":
        return "POSDATE"
    return DEFAULT_DATE_COLUMN


def validate_environment(environment):
    environment = environment.upper()
    if environment not in {"DEV", "UAT", "PROD"}:
        raise ValueError(f"Unsupported ENVIRONMENT: {environment}")
    return environment


def check_table_exists(conn, table_name):
    sql = """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (SCHEMA_NAME, table_name))
        row_count = cursor.fetchone()[0]
        return row_count > 0
    finally:
        cursor.close()


def get_column_metadata(conn, table_name):
    sql = """
        SELECT
            c.name AS COLUMN_NAME,
            c.column_id AS ORDINAL_POSITION,
            t.name AS DATA_TYPE,
            c.is_computed AS IS_COMPUTED,
            c.is_identity AS IS_IDENTITY,
            CASE WHEN t.name = 'timestamp' THEN 1 ELSE 0 END AS IS_ROWVERSION
        FROM sys.columns c
        INNER JOIN sys.tables tbl ON tbl.object_id = c.object_id
        INNER JOIN sys.schemas s ON s.schema_id = tbl.schema_id
        INNER JOIN sys.types t ON t.user_type_id = c.user_type_id
        WHERE s.name = ?
          AND tbl.name = ?
        ORDER BY c.column_id
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (SCHEMA_NAME, table_name))
        rows = cursor.fetchall()
        return [
            {
                "COLUMN_NAME": row[0],
                "ORDINAL_POSITION": row[1],
                "DATA_TYPE": row[2],
                "IS_COMPUTED": bool(row[3]),
                "IS_IDENTITY": bool(row[4]),
                "IS_ROWVERSION": bool(row[5]),
            }
            for row in rows
        ]
    finally:
        cursor.close()


def determine_insertable_columns(columns, date_column, table_name):
    usable = []
    identity_columns = []
    for col in columns:
        name = col["COLUMN_NAME"]
        if col["IS_COMPUTED"] or col["IS_ROWVERSION"]:
            continue
        if col["IS_IDENTITY"]:
            identity_columns.append(name)
            continue
        usable.append(name)

    if date_column not in usable:
        raise ValueError(
            f"Expected date column '{date_column}' was not found among usable table metadata for {table_full_name(table_name)}."
        )

    if identity_columns:
        logger.warning(
            "Identity column(s) detected for %s: %s. They will be excluded from the INSERT statement. "
            "This means SQL Server will generate new identity values rather than copying the source values.",
            table_full_name(table_name),
            identity_columns,
        )

    return usable, identity_columns


def build_insert_sql(table_name, date_column, insertable_columns):
    full_name = table_full_name(table_name)
    insert_columns = ",\n    ".join(insertable_columns)
    select_parts = []
    for col_name in insertable_columns:
        if col_name == date_column:
            select_parts.append(f"? AS {col_name}")
        else:
            select_parts.append(col_name)
    select_columns = ",\n    ".join(select_parts)
    sql = f"""INSERT INTO {full_name}
(
    {insert_columns}
)
SELECT
    {select_columns}
FROM {full_name}
WHERE {date_column} = ?;"""
    return sql


def get_row_count(conn, table_name, date_column, target_date):
    sql = f"SELECT COUNT(*) FROM {table_full_name(table_name)} WHERE {date_column} = ?"
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (target_date,))
        return cursor.fetchone()[0]
    finally:
        cursor.close()


def ensure_environment_config(config_file, environment):
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file not found: {config_file}")
    get_db_connect_str(config_file, environment)


def print_preview(environment, table_name, date_column, source_date, target_date, source_count, target_count, insertable_columns, generated_sql):
    print("")
    print("=" * 80)
    print("Backfill Preview")
    print("=" * 80)
    print(f"Environment       : {environment}")
    print(f"Database          : {DATABASE_NAME}")
    print(f"Schema            : {SCHEMA_NAME}")
    print(f"Table             : {table_name}")
    print(f"Date Column       : {date_column}")
    print("")
    print(f"Source Date       : {source_date}")
    print(f"Target Date       : {target_date}")
    print("")
    print(f"Source Row Count  : {source_count:,}")
    print(f"Target Row Count  : {target_count:,}")
    print(f"Insertable Columns: {len(insertable_columns)}")
    print("")
    print("Generated SQL:")
    print(generated_sql)
    print("")
    print("Parameters:")
    print(f"Target Date = {target_date}")
    print(f"Source Date = {source_date}")
    print("")
    print("MODE: PREVIEW")
    print("NO DATABASE CHANGES HAVE BEEN MADE.")
    print("# Use --EXECUTE to perform the backfill.")
    print("=" * 80)


def run_backfill(args):
    environment = validate_environment(args.ENVIRONMENT)
    table_name = args.TABLE
    source_date = parse_date_value(args.SOURCE_DATE, "SOURCE_DATE")
    target_date = parse_date_value(args.TARGET_DATE, "TARGET_DATE")
    config_file = args.CONFIG_FILE

    if source_date == target_date:
        raise ValueError("SOURCE_DATE and TARGET_DATE must be different dates.")

    ensure_environment_config(config_file, environment)

    import pyodbc

    connect_str = get_db_connect_str(config_file, environment)
    conn = pyodbc.connect(connect_str, autocommit=False, timeout=120)
    cursor = conn.cursor()
    try:
        logger.info("Environment=%s Table=%s DateColumn=%s SourceDate=%s TargetDate=%s",
                    environment, table_name, get_date_column_for_table(table_name),
                    source_date.isoformat(), target_date.isoformat())

        if not check_table_exists(conn, table_name):
            raise ValueError(f"Table does not exist: {table_full_name(table_name)}")

        columns = get_column_metadata(conn, table_name)
        if not columns:
            raise ValueError(f"No metadata found for table: {table_full_name(table_name)}")

        date_column = get_date_column_for_table(table_name)
        if not any(item["COLUMN_NAME"] == date_column for item in columns):
            raise ValueError(f"Required date column '{date_column}' does not exist in {table_full_name(table_name)}.")

        insertable_columns, identity_columns = determine_insertable_columns(columns, date_column, table_name)
        if not insertable_columns:
            raise ValueError(f"No insertable columns were found for {table_full_name(table_name)}")

        generated_sql = build_insert_sql(table_name, date_column, insertable_columns)

        source_count = get_row_count(conn, table_name, date_column, source_date.isoformat())
        target_count = get_row_count(conn, table_name, date_column, target_date.isoformat())

        logger.info("source_count=%s target_count=%s insertable_columns=%s",
                    source_count, target_count, len(insertable_columns))

        if source_count == 0:
            raise ValueError(
                f"Source date {source_date.isoformat()} has zero rows in {table_full_name(table_name)}. "
                "No valid backfill operation can be generated."
            )

        if target_count > 0:
            raise ValueError(
                f"Target date {target_date.isoformat()} already has {target_count} rows in {table_full_name(table_name)}. "
                "Aborting to avoid duplicate inserts."
            )

        if not args.EXECUTE:
            print_preview(
                environment,
                table_name,
                date_column,
                source_date.isoformat(),
                target_date.isoformat(),
                source_count,
                target_count,
                insertable_columns,
                generated_sql,
            )
            return 0

        if environment == "PROD":
            print("")
            print("Table       : " + table_full_name(table_name))
            print(f"Source Date : {source_date.isoformat()}")
            print(f"Target Date : {target_date.isoformat()}")
            print(f"Rows        : {source_count:,}")
            print("")
            print("# This operation will INSERT records into PRODUCTION.")
            confirmation = input("Type EXECUTE PROD to continue: ").strip()
            if confirmation != "EXECUTE PROD":
                print("Production confirmation failed. Operation cancelled safely.")
                return 1

        logger.info("Executing backfill for %s in %s mode", table_name, environment)
        sql = generated_sql
        params = []
        for col_name in insertable_columns:
            if col_name == date_column:
                params.append(target_date.isoformat())
            else:
                pass
        params.append(source_date.isoformat())

        cursor.execute("BEGIN TRANSACTION")
        cursor.execute(sql, tuple(params))
        inserted_target_count = get_row_count(conn, table_name, date_column, target_date.isoformat())

        if inserted_target_count < source_count:
            raise ValueError(
                f"Backfill validation failed: expected at least {source_count} rows for {target_date.isoformat()}, "
                f"but found {inserted_target_count}."
            )

        conn.commit()
        logger.info("Backfill committed successfully. Final target count=%s", inserted_target_count)
        print(f"Backfill completed successfully. Final target count: {inserted_target_count:,}")
        return 0
    except Exception as exc:
        logger.error("Backfill failed: %s", exc)
        logger.error(traceback.format_exc())
        try:
            conn.rollback()
            logger.warning("Transaction rolled back.")
        except Exception:
            pass
        print(f"Operational error: {exc}")
        return 1
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def build_parser():
    parser = argparse.ArgumentParser(description="Controlled date-based data backfill for LIQUIDITY.LST_SF_EXTRACT tables.")
    parser.add_argument("--ENVIRONMENT", required=True, choices=["DEV", "UAT", "PROD"], help="Target environment")
    parser.add_argument("--TABLE", required=True, choices=APPROVED_TABLES, help="Approved table name")
    parser.add_argument("--SOURCE_DATE", required=True, help="Source business date in YYYY-MM-DD format")
    parser.add_argument("--TARGET_DATE", required=True, help="Target business date in YYYY-MM-DD format")
    parser.add_argument("--CONFIG_FILE", default="dbconfig.ini", help="Database config file (default: dbconfig.ini)")
    parser.add_argument("--EXECUTE", action="store_true", help="Execute the INSERT only when this flag is supplied")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        return run_backfill(args)
    except Exception as exc:
        logger.error("Fatal error: %s", exc)
        logger.error(traceback.format_exc())
        print(f"Fatal error: {exc}")
        return 1


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
