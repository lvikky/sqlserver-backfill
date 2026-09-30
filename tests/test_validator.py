import unittest

from tests.support import SOURCE, TARGET
from backfill.errors import BackfillError
from backfill.validator import counts, dates


class ValidatorTests(unittest.TestCase):
    def test_iso_dates(self):
        self.assertEqual(dates("2026-09-18", "2026-09-21"), (SOURCE, TARGET))

    def test_same_date(self):
        with self.assertRaises(BackfillError):
            dates("2026-09-18", "2026-09-18")

    def test_invalid_dates(self):
        for value in ("2026-02-30", "2026-9-18", "20260918", "2026-09-18 OR 1=1", "2026-09-18T00:00:00"):
            with self.subTest(value=value), self.assertRaises(BackfillError):
                dates(value, "2026-09-21")

    def test_missing_source(self):
        with self.assertRaisesRegex(BackfillError, "No source"):
            counts(0, 0)

    def test_existing_target(self):
        with self.assertRaisesRegex(BackfillError, "already exists"):
            counts(10, 1)

    def test_valid_counts(self):
        counts(10, 0)
