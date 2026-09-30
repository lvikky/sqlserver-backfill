from dataclasses import dataclass
from datetime import date

from .config import Environment, Table, quote
from .metadata import Column, validate


@dataclass(frozen=True)
class Statement:
    sql: str
    parameters: tuple = ()


def predicate(table: Table, day: date, *, source: bool) -> Statement:
    parts = [f"{quote(table.date_column)} = ?"]
    params = [day]
    operators = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
    for item in table.source_filters if source else ():
        if item.operator in {"is_null", "is_not_null"}:
            parts.append(f"{quote(item.column)} IS {'NOT ' if item.operator == 'is_not_null' else ''}NULL")
        else:
            parts.append(f"{quote(item.column)} {operators[item.operator]} ?")
            params.append(item.value)
    return Statement(" AND ".join(parts), tuple(params))


def count_sql(environment: Environment, table: Table, day: date, *, source: bool,
              lock: bool = False) -> Statement:
    where = predicate(table, day, source=source)
    hint = " WITH (TABLOCKX, HOLDLOCK)" if lock else ""
    return Statement(f"SELECT COUNT_BIG(*) FROM {table.qualified(environment)}{hint}\nWHERE {where.sql};",
                     where.parameters)


def insert_sql(environment: Environment, table: Table, columns: tuple[Column, ...],
               source: date, target: date) -> Statement:
    columns = validate(columns, table)
    names, expressions, params = [], [], []
    for col in columns:
        mode = table.regenerate.get(col.name)
        if col.server_generated or col.name in table.exclude_columns or mode == "default":
            continue
        names.append(quote(col.name))
        if col.name == table.date_column:
            expressions.append(f"CAST(? AS date) AS {quote(col.name)}")
            params.append(target)
        elif mode:
            expression = {"utc_now": "SYSUTCDATETIME()", "new_uuid": "NEWID()"}[mode]
            expressions.append(f"{expression} AS {quote(col.name)}")
        else:
            expressions.append(quote(col.name))
    where = predicate(table, source, source=True)
    params.extend(where.parameters)
    sql = (f"INSERT INTO {table.qualified(environment)} (\n    " + ",\n    ".join(names) +
           "\n)\nSELECT\n    " + ",\n    ".join(expressions) +
           f"\nFROM {table.qualified(environment)}\nWHERE {where.sql};")
    return Statement(sql, tuple(params))
