from dataclasses import dataclass

from .config import Table, quote
from .errors import BackfillError


@dataclass(frozen=True)
class Column:
    name: str
    ordinal: int
    data_type: str
    nullable: bool = False
    has_default: bool = False
    identity: bool = False
    computed: bool = False
    generated: bool = False
    hidden: bool = False
    encrypted: bool = False
    column_set: bool = False
    masked: bool = False

    @property
    def server_generated(self) -> bool:
        return self.identity or self.computed or self.generated or self.data_type in {"timestamp", "rowversion"}


def validate(columns: tuple[Column, ...], table: Table) -> tuple[Column, ...]:
    if not columns or len({c.name for c in columns}) != len(columns):
        raise BackfillError("Missing or ambiguous table metadata.")
    if len({c.ordinal for c in columns}) != len(columns):
        raise BackfillError("Ambiguous column ordering.")
    by_name = {c.name: c for c in columns}
    requested = {table.date_column, *table.exclude_columns, *table.regenerate,
                 *(f.column for f in table.source_filters)}
    if requested - set(by_name):
        raise BackfillError("A configured column is absent from metadata (names are case-sensitive).")
    date_col = by_name[table.date_column]
    if table.date_column in table.exclude_columns or table.date_column in table.regenerate:
        raise BackfillError("The business date column cannot be excluded or regenerated.")
    if date_col.data_type != "date" or date_col.server_generated:
        raise BackfillError("The business date column must be a writable SQL DATE. Timestamp/text dates need an explicit policy.")
    for col in columns:
        quote(col.name)
        if col.encrypted or col.column_set or col.masked or (col.hidden and not col.server_generated):
            raise BackfillError("Encrypted, masked, column-set, or unsupported hidden columns require a reviewed adapter.")
        mode = table.regenerate.get(col.name)
        if col.server_generated and mode not in {None, "default"}:
            raise BackfillError("Server-generated columns can only use default regeneration.")
        if col.name in table.exclude_columns or mode == "default":
            if not (col.nullable or col.has_default or col.server_generated):
                raise BackfillError("An omitted required column has no default or generation policy.")
        if mode == "utc_now" and col.data_type not in {"datetime", "datetime2", "smalldatetime"}:
            raise BackfillError("utc_now requires a datetime, datetime2, or smalldatetime column.")
        if mode == "new_uuid" and col.data_type != "uniqueidentifier":
            raise BackfillError("new_uuid requires a uniqueidentifier column.")
    return tuple(sorted(columns, key=lambda c: c.ordinal))
