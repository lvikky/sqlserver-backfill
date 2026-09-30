from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .errors import BackfillError


class Audit:
    """JSONL sink; replace emit() with another durable sink for centralized auditing."""

    def __init__(self, path: Path):
        self.run_id = str(uuid4())
        self.context = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.stream = os.fdopen(fd, "a", encoding="utf-8")

    def emit(self, event: str, **fields) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
                  **self.context, "event": event, **fields}
        self.stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self) -> None:
        self.stream.close()


def safe_error(exc: BaseException) -> dict:
    # Driver messages can contain usernames, data values, or connection strings.
    # Only our own curated messages and a validated SQLSTATE are exported.
    state = exc.args[0] if exc.args else None
    return {"error_type": type(exc).__name__,
            "error": str(exc) if isinstance(exc, BackfillError) else "Operation failed; raw exception text suppressed.",
            "sqlstate": state if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state) else None}
