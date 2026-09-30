import re
from datetime import date

from .errors import BackfillError


def dates(source: str, target: str) -> tuple[date, date]:
    try:
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (source, target)):
            raise ValueError
        parsed = date.fromisoformat(source), date.fromisoformat(target)
    except ValueError:
        raise BackfillError("Dates must be valid ISO calendar dates: YYYY-MM-DD.") from None
    if parsed[0] == parsed[1]:
        raise BackfillError("Source and target dates must differ.")
    return parsed


def counts(source: int, target: int) -> None:
    if target != 0:
        raise BackfillError("Data already exists for the target date. Backfill aborted.")
    if source <= 0:
        raise BackfillError("No source-date records match the configured selection.")
