"""
Database integration test against the real DATABASE_URL (CockroachDB or
PostgreSQL). Everything happens in a throwaway schema that is dropped at the
end, so the real tables are never touched.

Skipped unless explicitly enabled:

    set RERA_DB_INTEGRATION=1            (PowerShell: $env:RERA_DB_INTEGRATION=1)
    python -m unittest tests.test_db_integration -v
"""

import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import db  # noqa: E402
from common.rera_common import normalize_promoter  # noqa: E402

# test_scrapers.py disables the database for unit tests; read .env directly here.
URL = os.environ.get("DATABASE_URL") or db._read_dotenv(ROOT / ".env").get("DATABASE_URL")


def record(number, **overrides):
    base = {
        "state_code": "TN", "rera_number": number, "project_name": "Test Towers",
        "promoter_name": "M/s. Test Builders Pvt. Ltd.", "project_type": "Building",
        "district": "Chennai", "taluka": "", "pincode": "600001",
        "registration_date": "2026-01-02", "proposed_completion_date": "2030-12-31",
        "project_status": "registered", "status_remarks": "", "detail_url": "",
        "source_url": "https://example.invalid/list", "latitude": "13.0",
    }
    base.update(overrides)
    base["promoter_normalized"] = normalize_promoter(base["promoter_name"])
    return base


@unittest.skipUnless(os.environ.get("RERA_DB_INTEGRATION") == "1" and URL,
                     "set RERA_DB_INTEGRATION=1 and DATABASE_URL to run")
class DatabaseSyncTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"rera_selftest_{uuid.uuid4().hex[:8]}"
        self.conn = db.connect(URL)
        self.conn.execute(f"CREATE SCHEMA {self.schema}")
        self.conn.execute(f"SET search_path = {self.schema}")
        db.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.execute("SET search_path = public")
        self.conn.execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.conn.close()

    def rows(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def test_insert_unchanged_update_history(self):
        first = {"A": record("A"), "B": record("B")}
        self.assertEqual(db.sync_projects(self.conn, "TN", first)["inserted"], 2)

        again = db.sync_projects(self.conn, "TN", {"A": record("A"), "B": record("B")})
        self.assertEqual((again["inserted"], again["updated"], again["unchanged"]), (0, 0, 2))

        changed = {"A": record("A", project_status="completed",
                               proposed_completion_date="2031-06-30")}
        counts = db.sync_projects(self.conn, "TN", changed)
        self.assertEqual((counts["updated"], counts["history_rows"]), (1, 2))

        history = self.rows("SELECT h.field_name, h.old_value, h.new_value FROM "
                            "project_change_history h JOIN rera_projects p ON p.id = h.project_id "
                            "WHERE p.rera_number = 'A' ORDER BY h.field_name")
        self.assertEqual(history, [("project_status", "registered", "completed"),
                                   ("proposed_completion_date", "2030-12-31", "2031-06-30")])
        status, first_seen, changed_at = self.rows(
            "SELECT project_status, first_seen_at, last_changed_at FROM rera_projects "
            "WHERE rera_number = 'A'")[0]
        self.assertEqual(status, "completed")
        self.assertLess(first_seen, changed_at)

    def test_no_duplicates_and_developer_links(self):
        db.sync_projects(self.conn, "TN", {"A": record("A")})
        db.sync_projects(self.conn, "TN", {"A": record("A", project_name="Renamed")})
        self.assertEqual(self.rows("SELECT count(*) FROM rera_projects")[0][0], 1)
        self.assertEqual(self.rows("SELECT normalized_name FROM developers"),
                         [("TEST BUILDERS PVT LTD",)])
        self.assertEqual(self.rows("SELECT count(*) FROM project_developers")[0][0], 1)

    def test_extra_fields_and_null_dates(self):
        db.sync_projects(self.conn, "TN", {"A": record("A", registration_date="",
                                                       proposed_completion_date="")})
        reg, extra = self.rows("SELECT registration_date, extra FROM rera_projects")[0]
        self.assertIsNone(reg)
        self.assertEqual(extra, {"latitude": "13.0"})

    def test_run_and_errors_are_recorded(self):
        from datetime import datetime, timezone
        run_id = db.record_run(self.conn, "KA", datetime.now(timezone.utc),
                               {"total_projects": 5}, {"inserted": 5},
                               {"Kodagu": "RuntimeError: HTTP 503"})
        status, failed = self.rows("SELECT status, records_failed FROM scrape_runs WHERE id = %s", run_id)[0]
        self.assertEqual((status, failed), ("completed_with_errors", 1))
        self.assertEqual(self.rows("SELECT source_url, error_type FROM scrape_errors"),
                         [("Kodagu", "RuntimeError")])


if __name__ == "__main__":
    unittest.main()
