from __future__ import annotations

import configparser
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BackfillError

ENVIRONMENTS = {"dev": "SQLSERVER_DEV", "uat": "SQLSERVER_UAT", "prod": "SQLSERVER_PRD"}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_@$#]{0,127}\Z")
ALIAS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise BackfillError("Invalid configured SQL identifier; use a simple unquoted name.")
    if value.upper().startswith("REPLACE_"):
        raise BackfillError("Replace configuration placeholders before connecting.")
    return value


def quote(value: str) -> str:
    # Also safe for column names returned by metadata, including embedded brackets.
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise BackfillError("Invalid metadata identifier.")
    return "[" + value.replace("]", "]]") + "]"


def _odbc(value: str) -> str:
    if not value or any(ord(c) < 32 for c in value):
        raise BackfillError("Invalid ODBC configuration value.")
    return "{" + value.replace("}", "}}") + "}"


@dataclass(frozen=True)
class Environment:
    name: str
    section: str
    server: str
    database: str
    driver: str
    trusted_connection: str = "yes"
    encrypt: str = "yes"
    trust_certificate: str = "no"
    username_env: str | None = None
    password_env: str | None = None

    def connection_string(self) -> str:
        parts = {"Driver": self.driver, "Server": self.server, "Database": self.database,
                 "Encrypt": self.encrypt, "TrustServerCertificate": self.trust_certificate,
                 "APP": "ControlledHistoricalBackfill"}
        if self.trusted_connection == "yes":
            parts["Trusted_Connection"] = "yes"
        else:
            for key, env_name in (("UID", self.username_env), ("PWD", self.password_env)):
                value = os.environ.get(env_name or "")
                if not value:
                    raise BackfillError("A configured authentication environment variable is missing.")
                parts[key] = value
        return ";".join(f"{key}={_odbc(value)}" for key, value in parts.items()) + ";"


def load_environment(path: Path, name: str) -> Environment:
    if name not in ENVIRONMENTS:
        raise BackfillError("Environment must be dev, uat, or prod.")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error):
        raise BackfillError("Cannot read a valid connection INI file.") from None
    if parser.defaults():
        raise BackfillError("INI DEFAULT values are forbidden to avoid environment inheritance.")
    resolved = {}
    for env, section in ENVIRONMENTS.items():
        if section not in parser:
            raise BackfillError(f"Required connection section [{section}] is missing.")
        cfg = parser[section]
        allowed = {"driver", "server", "database", "trusted_connection", "encrypt",
                   "trustservercertificate", "username_env", "password_env"}
        if set(cfg) - allowed:
            raise BackfillError(f"Unsupported connection option in [{section}]. No inline credentials allowed.")
        if not all(cfg.get(key, "").strip() for key in ("driver", "server", "database")):
            raise BackfillError(f"DRIVER, SERVER, and DATABASE are required in [{section}].")
        driver = cfg["driver"].strip()
        if driver.startswith("{") and driver.endswith("}"):
            driver = driver[1:-1]
        if driver not in {"ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"}:
            raise BackfillError("Use Microsoft ODBC Driver 17 or 18 for SQL Server.")
        server = cfg["server"].strip()
        if "REPLACE_" in server.upper():
            raise BackfillError("Replace connection placeholders before connecting.")
        _odbc(server)
        trusted = cfg.get("trusted_connection", "yes").lower().strip()
        encrypt = cfg.get("encrypt", "yes").lower().strip()
        cert = cfg.get("trustservercertificate", "no").lower().strip()
        if trusted not in {"yes", "no"} or encrypt != "yes" or cert != "no":
            raise BackfillError("Use Trusted_Connection=yes/no, Encrypt=yes, TrustServerCertificate=no.")
        user_env, pass_env = cfg.get("username_env"), cfg.get("password_env")
        if trusted == "yes" and (user_env or pass_env):
            raise BackfillError("Integrated authentication cannot include SQL credential references.")
        if trusted == "no" and (not user_env or not pass_env):
            raise BackfillError("SQL authentication requires username_env and password_env references.")
        for ref in (user_env, pass_env):
            if ref and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref):
                raise BackfillError("Invalid authentication environment variable name.")
        resolved[env] = Environment(env, section, server, identifier(cfg["database"].strip()),
                                    driver, trusted, encrypt, cert, user_env, pass_env)
    endpoints = {(v.server.casefold(), v.database.casefold()) for v in resolved.values()}
    if len(endpoints) != len(resolved):
        raise BackfillError("Environment isolation failure: environments share a server/database pair.")
    return resolved[name]


@dataclass(frozen=True)
class SourceFilter:
    column: str
    operator: str
    value: str | int | float | bool | None = None


@dataclass(frozen=True)
class Table:
    alias: str
    schema: str
    name: str
    date_column: str
    exclude_columns: tuple[str, ...] = ()
    regenerate: dict[str, str] = field(default_factory=dict)
    source_filters: tuple[SourceFilter, ...] = ()

    def qualified(self, environment: Environment) -> str:
        return ".".join(quote(x) for x in (environment.database, self.schema, self.name))


def load_table(path: Path, alias: str, environment: str) -> Table:
    if environment not in ENVIRONMENTS:
        raise BackfillError("Environment must be dev, uat, or prod.")
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise BackfillError("Cannot read a valid table allowlist TOML file.") from None
    if set(document) != {"tables"} or not isinstance(document["tables"], dict):
        raise BackfillError("Table configuration must contain only a tables mapping.")
    if not ALIAS.fullmatch(alias) or alias not in document["tables"]:
        raise BackfillError("Table alias is not in the approved allowlist.")
    raw = document["tables"][alias]
    if not isinstance(raw, dict):
        raise BackfillError("Invalid table configuration.")
    raw = dict(raw)
    overrides = raw.pop("overrides", {})
    if not isinstance(overrides, dict) or set(overrides) - set(ENVIRONMENTS):
        raise BackfillError("Invalid environment overrides.")
    for override in overrides.values():
        if not isinstance(override, dict) or set(override) - {"schema", "table", "date_column"}:
            raise BackfillError("Overrides may only change schema, table, and date_column.")
        for value in override.values():
            identifier(value)
    raw.update(overrides.get(environment, {}))
    if set(raw) - {"schema", "table", "date_column", "exclude_columns", "regenerate", "source_filters"}:
        raise BackfillError("Unsupported table configuration option.")
    for key in ("schema", "table", "date_column"):
        identifier(raw.get(key))
    excluded = raw.get("exclude_columns", [])
    regenerated = raw.get("regenerate", {})
    if not isinstance(excluded, list) or not isinstance(regenerated, dict):
        raise BackfillError("Invalid column policy configuration.")
    for column in [*excluded, *regenerated]:
        identifier(column)
    if len(set(excluded)) != len(excluded) or set(excluded) & set(regenerated):
        raise BackfillError("Column policies overlap or contain duplicates.")
    if raw["date_column"] in set(excluded) | set(regenerated):
        raise BackfillError("The business date column cannot be excluded or regenerated.")
    if any(value not in {"default", "utc_now", "new_uuid"} for value in regenerated.values()):
        raise BackfillError("Regeneration supports only default, utc_now, and new_uuid.")
    filters = raw.get("source_filters", [])
    if not isinstance(filters, list):
        raise BackfillError("source_filters must be a list.")
    result = []
    for item in filters:
        if not isinstance(item, dict) or set(item) - {"column", "operator", "value"}:
            raise BackfillError("Invalid source filter.")
        identifier(item.get("column"))
        op = item.get("operator")
        if op not in {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "is_not_null"}:
            raise BackfillError("Unsupported filter operator.")
        if op in {"is_null", "is_not_null"}:
            if "value" in item:
                raise BackfillError("NULL filters must not specify a value.")
        elif "value" not in item or type(item["value"]) not in {str, int, float, bool}:
            raise BackfillError("Filter values must be scalar strings, numbers, or booleans.")
        result.append(SourceFilter(item["column"], op, item.get("value")))
    return Table(alias, raw["schema"], raw["table"], raw["date_column"], tuple(excluded),
                 dict(regenerated), tuple(result))
