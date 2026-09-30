import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.support import COLUMNS, SOURCE, TABLE, TARGET, client, environment, ini
from backfill.audit import Audit, safe_error
from backfill.errors import BackfillError
from backfill.executor import Plan
from backfill.main import confirm_production, main, parser
from backfill.sql_generator import insert_sql


class MainAuditTests(unittest.TestCase):
    def plan(self):
        return Plan(environment("prod"), TABLE, SOURCE, TARGET, COLUMNS, 10, 0,
                    insert_sql(environment("prod"), TABLE, COLUMNS, SOURCE, TARGET))

    def test_confirmation_requires_exact_phrase(self):
        for phrase in ("", "yes", "execute prod", "EXECUTE PROD ", "EXECUTE PROD"):
            with self.subTest(phrase=phrase), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(confirm_production(self.plan(), stdin=Mock(isatty=lambda: True),
                                                   input_fn=lambda _: phrase), phrase == "EXECUTE PROD")

    def test_piped_confirmation_is_not_accepted(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(BackfillError, "interactive"):
            confirm_production(self.plan(), stdin=io.StringIO("EXECUTE PROD\n"), input_fn=lambda _: "EXECUTE PROD")

    def test_audit_structured_and_driver_secrets_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            audit = Audit(path)
            audit.context.update(environment="dev", mode="dry_run")
            audit.emit("failed", **safe_error(RuntimeError("28000", "PWD=secret")))
            audit.close()
            record = json.loads(path.read_text())
            self.assertEqual(record["sqlstate"], "28000")
            self.assertEqual(record["environment"], "dev")
            self.assertIn("timestamp", record)
            self.assertNotIn("secret", path.read_text())

    def test_execute_default_false(self):
        args = parser().parse_args(["--environment", "prod", "--table", "banking_sfts", "--source-date",
                                    "2026-09-18", "--target-date", "2026-09-21", "--config", "unused.ini"])
        self.assertFalse(args.execute)

    def test_mock_cli_dry_run_full_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "connections.ini"
            config.write_text(ini())
            db = client((0, 10))
            factory = Mock(return_value=db)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--environment", "prod", "--table", "banking_sfts", "--source-date", "2026-09-18",
                               "--target-date", "2026-09-21", "--config", str(config),
                               "--audit-log", str(path / "audit.jsonl")], client_factory=factory)
            self.assertEqual(result, 0)
            self.assertIn("MODE: DRY RUN", output.getvalue())
            self.assertIn("SQLSERVER_PRD", output.getvalue())
            self.assertFalse(factory.call_args.kwargs["execute_enabled"])
            db.insert.assert_not_called()
            db.close.assert_called_once()
            last = json.loads((path / "audit.jsonl").read_text().splitlines()[-1])
            self.assertEqual(last["status"], "dry_run_success")

    def test_invalid_configuration_cannot_open_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = Mock()
            with contextlib.redirect_stderr(io.StringIO()):
                result = main(["--environment", "dev", "--table", "banking_sfts", "--source-date", "2026-09-18",
                               "--target-date", "2026-09-21", "--config", str(Path(directory) / "missing.ini"),
                               "--audit-log", str(Path(directory) / "audit.jsonl")], client_factory=factory)
            self.assertEqual(result, 1)
            factory.assert_not_called()
