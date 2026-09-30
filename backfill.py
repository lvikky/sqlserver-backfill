#!/usr/bin/env python3
import sys
from pathlib import Path

if sys.version_info < (3, 13):
    raise SystemExit("Python 3.13 or later is required.")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from backfill.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
