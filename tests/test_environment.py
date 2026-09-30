import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import TABLE, environment, ini
from backfill.config import load_environment, load_table
from backfill.errors import BackfillError


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.ini"
        self.path.write_text(ini())
        self.tables = Path(__file__).resolve().parents[1] / "config" / "tables.toml"

    def test_all_environments_resolve_exact_sections_and_tables(self):
        for name in ("dev", "uat", "prod"):
            with self.subTest(name=name):
                env = load_environment(self.path, name)
                table = load_table(self.tables, "banking_sfts", name)
                self.assertEqual(env, environment(name))
                self.assertEqual(table.qualified(env), f"[TEST_{name.upper()}].[LST_SF_EXTRACT].[BANKING_SFTS]")
                self.assertIn(f"Server={{test-{name}}}", env.connection_string())

    def test_invalid_environment(self):
        for value in ("prd", "PROD", "production", "prod;DROP TABLE X"):
            with self.subTest(value=value), self.assertRaises(BackfillError):
                load_environment(self.path, value)

    def test_missing_section_no_fallback(self):
        self.path.write_text(ini().replace("SQLSERVER_PRD", "SQLSERVER_PROD"))
        with self.assertRaisesRegex(BackfillError, "SQLSERVER_PRD"):
            load_environment(self.path, "prod")

    def test_duplicate_environment_endpoint_rejected(self):
        self.path.write_text(ini().replace("test-uat", "test-dev").replace("TEST_UAT", "TEST_DEV"))
        with self.assertRaisesRegex(BackfillError, "isolation"):
            load_environment(self.path, "prod")

    def test_defaults_rejected(self):
        self.path.write_text("[DEFAULT]\npassword=secret\n" + ini())
        with self.assertRaises(BackfillError):
            load_environment(self.path, "dev")

    def test_inline_secrets_unknown_keys_and_insecure_tls_rejected(self):
        for change in ("PASSWORD=secret", "Encrypt=no", "TrustServerCertificate=yes"):
            content = ini()
            if change.startswith("Encrypt"):
                content = content.replace("Encrypt = yes", change)
            elif change.startswith("TrustServer"):
                content = content.replace("TrustServerCertificate = no", change)
            else:
                content += change
            self.path.write_text(content)
            with self.subTest(change=change), self.assertRaises(BackfillError):
                load_environment(self.path, "prod")

    def test_configured_database_injection_rejected(self):
        self.path.write_text(ini().replace("TEST_DEV", "DB]; DROP TABLE X;--"))
        with self.assertRaises(BackfillError):
            load_environment(self.path, "dev")

    def test_placeholder_config_fails_closed(self):
        path = self.tables.parent / "connections.example.ini"
        with self.assertRaisesRegex(BackfillError, "placeholders"):
            load_environment(path, "dev")

    def test_auth_values_are_odbc_escaped(self):
        content = ini().replace("Trusted_Connection = yes", "Trusted_Connection = no\nusername_env=TEST_USER\npassword_env=TEST_PASS")
        self.path.write_text(content)
        env = load_environment(self.path, "prod")
        with patch.dict(os.environ, {"TEST_USER": "user", "TEST_PASS": "a};Database=bad;{%"}):
            connection = env.connection_string()
            self.assertIn("PWD={a}};Database=bad;{%}", connection)
            self.assertIn("Database={TEST_PROD}", connection)

    def test_missing_auth_variable(self):
        self.path.write_text(ini().replace("Trusted_Connection = yes", "Trusted_Connection = no\nusername_env=MISSING_U\npassword_env=MISSING_P"))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(BackfillError):
            load_environment(self.path, "dev").connection_string()

    def test_invalid_alias(self):
        for alias in ("BANKING_SFTS", "unknown", "banking_sfts;DROP TABLE X"):
            with self.subTest(alias=alias), self.assertRaises(BackfillError):
                load_table(self.tables, alias, "dev")

    def test_environment_overrides_cannot_change_database(self):
        table_path = Path(self.temp.name) / "tables.toml"
        table_path.write_text(self.tables.read_text() + '\n[tables.banking_sfts.overrides.dev]\ndatabase="TEST_PROD"\n')
        with self.assertRaises(BackfillError):
            load_table(table_path, "banking_sfts", "dev")

    def test_schema_override_is_environment_local(self):
        table_path = Path(self.temp.name) / "tables.toml"
        table_path.write_text(self.tables.read_text() + '\n[tables.banking_sfts.overrides.uat]\nschema="UAT_SCHEMA"\n')
        self.assertEqual(load_table(table_path, "banking_sfts", "dev"), TABLE)
        self.assertEqual(load_table(table_path, "banking_sfts", "uat").schema, "UAT_SCHEMA")
