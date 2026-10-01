"""
Offline tests for the Tamil Nadu, Karnataka and Maharashtra scrapers and the shared
pipeline. Fixtures under tests/fixtures/ are trimmed copies of real portal
pages saved on 2026-09-30.
No test touches the network.

    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tamilnadu"), str(ROOT / "karnataka"), str(ROOT / "maharashtra")]

import scrape_karnataka as ka  # noqa: E402
import scrape_maharera as mh  # noqa: E402
import scrape_tn as tn  # noqa: E402
from common import rera_common as rc  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Unit tests must never reach the real database configured in .env.
from common import db  # noqa: E402
db.database_url = lambda: None


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# ─── Shared helpers ─────────────────────────────────────────────────────────

class DateParsingTests(unittest.TestCase):
    def test_formats_used_by_the_portals(self):
        cases = {
            "dated 02-01-2026": "2026-01-02",
            "03/01/2019": "2019-01-03",
            "21.12.2030": "2030-12-21",
            "01-Sep-2020": "2020-09-01",
            "2026-01-02": "2026-01-02",
            "18 May 2018": "2018-05-18",
        }
        for text, expected in cases.items():
            self.assertEqual(rc.parse_date(text), expected, text)

    def test_invalid_or_missing_dates_give_empty_string(self):
        for text in ("", None, "December 2022", "31/02/2024", "N/A"):
            self.assertEqual(rc.parse_date(text), "", text)

    def test_first_date_in_text_wins(self):
        self.assertEqual(rc.parse_date("from 01/01/2020 to 31/12/2021"), "2020-01-01")


class PromoterNormalisationTests(unittest.TestCase):
    def test_same_developer_different_spellings(self):
        variants = ["M/s. Newry Properties Pvt. Ltd.,", "NEWRY PROPERTIES PRIVATE LIMITED",
                    "m/s newry  properties pvt ltd"]
        self.assertEqual({rc.normalize_promoter(v) for v in variants},
                         {"NEWRY PROPERTIES PVT LTD"})

    def test_llp_and_empty(self):
        self.assertEqual(rc.normalize_promoter("Coorg Ventures L.L.P."), "COORG VENTURES LLP")
        self.assertEqual(rc.normalize_promoter(None), "")


class DatabaseConfigTests(unittest.TestCase):
    def test_credentials_are_redacted_from_errors(self):
        msg = ('invalid connection option ""postgresql://user:s3cret@host.cloud:26257/'
               'defaultdb?sslmode" password=hunter2')
        cleaned = db.redact(msg)
        self.assertNotIn("s3cret", cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertIn("host.cloud", cleaned)

    def test_quotes_from_docker_env_file_are_stripped(self):
        from importlib import reload
        import common.db as fresh
        reload(fresh)
        try:
            with mock.patch.dict("os.environ", {"DATABASE_URL": '"postgresql://u:p@h/d"'}):
                self.assertEqual(fresh.database_url(), "postgresql://u:p@h/d")
        finally:
            fresh.database_url = lambda: None


class PincodeTests(unittest.TestCase):
    def test_last_pin_in_state_range(self):
        self.assertEqual(rc.find_pincode("Chennai - 600 044, near 400001", "6"), "600044")
        self.assertEqual(rc.find_pincode("S.No 1234567 only"), "")


class PipelineTests(unittest.TestCase):
    def _rec(self, number, **extra):
        return {"state_code": "TN", "rera_number": number, "source_url": "u",
                "project_name": "P", "project_status": "registered", **extra}

    def test_duplicates_are_merged_and_reported(self):
        rows = [self._rec("A", registration_date="2020-01-01", source_url="old"),
                self._rec("A", registration_date="2021-01-01", source_url="new"),
                self._rec("B")]
        records, invalid, duplicates = rc.consolidate(rows, {"TN"})
        self.assertEqual(set(records), {"A", "B"})
        self.assertEqual(records["A"]["source_url"], "new")
        self.assertEqual(duplicates, [{"key": "A", "kept_source": "new", "other_source": "old"}])
        self.assertEqual(invalid, [])

    def test_invalid_rows_are_kept_not_dropped(self):
        rows = [self._rec(""), {**self._rec("C"), "state_code": "XX"},
                {**self._rec("D"), "source_url": ""}]
        records, invalid, _ = rc.consolidate(rows, {"TN"})
        self.assertEqual(records, {})
        self.assertEqual(len(invalid), 3)
        self.assertIn("no RERA", invalid[0]["problems"])
        self.assertIn("unexpected state", invalid[1]["problems"])
        self.assertIn("no source URL", invalid[2]["problems"])

    def test_change_detection(self):
        first = {"A": self._rec("A"), "B": self._rec("B")}
        rc.diff_and_stamp({}, first, "t1", complete=True)
        previous = json.loads(json.dumps(first))

        second = {"A": self._rec("A", project_status="completed"), "C": self._rec("C")}
        added, changed, removed, history = rc.diff_and_stamp(previous, second, "t2", complete=True)
        self.assertEqual(added, ["C"])
        self.assertEqual(removed, ["B"])
        self.assertEqual(changed[0]["changes"], {"project_status": ("registered", "completed")})
        self.assertEqual(history[0]["field"], "project_status")
        self.assertEqual(second["A"]["first_seen_at"], "t1")
        self.assertEqual(second["A"]["last_changed_at"], "t2")

    def test_unchanged_record_produces_no_history(self):
        previous = {"A": self._rec("A")}
        rc.diff_and_stamp({}, previous, "t1", complete=True)
        current = {"A": self._rec("A", scraped_at="later")}
        _, changed, _, history = rc.diff_and_stamp(previous, current, "t2", complete=True)
        self.assertEqual((changed, history), ([], []))
        self.assertEqual(current["A"]["last_changed_at"], "t1")

    def test_incomplete_run_keeps_unseen_projects(self):
        previous = {"A": self._rec("A"), "B": self._rec("B")}
        rc.diff_and_stamp({}, previous, "t1", complete=True)
        current = {"A": self._rec("A")}
        _, _, removed, _ = rc.diff_and_stamp(previous, current, "t2", complete=False)
        self.assertEqual(removed, [])
        self.assertIn("B", current)


class RunStoreTests(unittest.TestCase):
    def test_resume_skips_saved_sources_and_retries_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = rc.RunStore(tmp, "test")
            calls = []

            def fetch(name):
                calls.append(name)
                if name == "bad":
                    raise RuntimeError("HTTP 503")
                if name == "empty":
                    return []
                return [{"rera_number": name}]

            failures = rc.run_sources(store, ["a", "b", "bad", "empty"], fetch)
            self.assertEqual(set(failures), {"bad", "empty"})
            self.assertEqual(store.done_sources(), {"a", "b"})

            calls.clear()
            rc.run_sources(store, ["a", "b", "bad", "empty"], fetch)
            self.assertEqual(sorted(calls), ["bad", "empty"])
            self.assertEqual(len(store.load_partial_records()), 2)

    def test_http_errors_are_retried_then_raised(self):
        session = rc.PoliteSession(delay=0, attempts=3)
        response = mock.Mock(status_code=503)
        with mock.patch.object(session.session, "request", return_value=response) as req, \
                mock.patch("common.rera_common.time.sleep"):
            with self.assertRaises(RuntimeError):
                session.get("https://example.invalid")
        self.assertEqual(req.call_count, 3)

    def test_http_404_is_not_retried(self):
        session = rc.PoliteSession(delay=0, attempts=3)
        response = mock.Mock(status_code=404)
        with mock.patch.object(session.session, "request", return_value=response) as req:
            with self.assertRaises(RuntimeError):
                session.get("https://example.invalid")
        self.assertEqual(req.call_count, 1)

    def test_finish_run_writes_workbook_and_is_idempotent(self):
        rows = [{"state_code": "TN", "rera_number": "X/1", "source_url": "u",
                 "project_name": "One", "project_status": "registered"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.print"), \
                mock.patch.object(rc, "log") as log:
            store = rc.RunStore(tmp, "test")
            cols = [("rera_number", "RERA", 10)]
            from datetime import datetime, timezone
            started = datetime.now(timezone.utc)
            self.assertEqual(rc.finish_run(store, state_code="TN", raw_records=rows, failures={},
                                           started=started, project_columns=cols), 0)
            self.assertTrue(store.excel_file.exists())
            rc.finish_run(store, state_code="TN", raw_records=json.loads(json.dumps(rows)),
                          failures={}, started=started, project_columns=cols)
            messages = " ".join(str(c.args[0]) for c in log.call_args_list)
            self.assertIn("Already up to date", messages)


# ─── Tamil Nadu ─────────────────────────────────────────────────────────────

class TamilNaduTests(unittest.TestCase):
    def test_online_building_register(self):
        records = tn.parse_table(fixture("tn_online_building.html"), category="building",
                                 mode="online", source_url="u")
        self.assertEqual(len(records), 3)
        first = records[0]
        self.assertEqual(first["rera_number"], "TNRERA/29/BLG/0001/2026")
        self.assertEqual(first["project_name"], "Thiruvottiyur Scheme")
        self.assertEqual(first["promoter_name"], "TNUHDB")
        self.assertEqual(first["registration_date"], "2026-01-02")
        self.assertEqual(first["proposed_completion_date"], "2030-12-21")
        self.assertEqual(first["district"], "Chennai")
        # Project text has no PIN; the promoter office PIN (600019) must not leak in.
        self.assertEqual(first["pincode"], "")
        self.assertEqual(first["latitude"], "13.163286")
        self.assertTrue(first["detail_url"].startswith("https://rera.tn.gov.in/public-view2/"))
        self.assertTrue(first["promoter_url"].startswith("https://rera.tn.gov.in/public-view1/"))
        self.assertEqual(records[2]["district"], "Coimbatore")

    def test_offline_register_statuses_and_extensions(self):
        records = {r["rera_number"]: r for r in tn.parse_table(
            fixture("tn_offline_building_2019.html"), category="building",
            mode="offline", source_url="u")}
        self.assertEqual(records["TN/01/Building/001/2019"]["project_name"], "Nexterra")
        # Latest extension date is the effective completion date.
        self.assertEqual(records["TN/01/Building/001/2019"]["proposed_completion_date"], "2026-06-30")
        self.assertEqual(records["TN/01/Building/0086/2019"]["project_status"], "revoked")
        self.assertEqual(records["TN/01/Building/0089/2019"]["project_status"], "partially_cancelled")
        self.assertEqual(records["TN/01/Building/127/2019"]["project_status"], "completed")

    def test_regularisation_register_has_different_columns(self):
        records = tn.parse_table(fixture("tn_regularisation_online.html"),
                                 category="regularisation", mode="online", source_url="u")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["rera_number"], "TN/1/Regularisation-Layout/10001/2022")
        self.assertEqual(records[0]["project_name"], "VA GARDEN")
        self.assertEqual(records[0]["registration_date"], "2022-11-03")
        self.assertEqual(records[0]["project_type"], "Regularised Layout")
        self.assertEqual(records[1]["district"], "Namakkal")

    def test_discovery_helpers(self):
        html = fixture("tn_online_building.html")
        self.assertEqual(tn.online_years(html), ["2024", "2025", "2026"])
        self.assertEqual(tn.csrf_token(html), "TESTTOKEN123")
        self.assertEqual(tn.offline_years('<a href="/building/offline/2018"></a>'
                                          '<a href="/building/offline/2017"></a>'
                                          '<a href="/layout/offline/2019"></a>', "building"),
                         ["2017", "2018"])

    def test_district_inference(self):
        self.assertEqual(tn.infer_district("Melakottaiyur Village, Kancheepuram District. Chennai"),
                         "Kancheepuram")
        self.assertEqual(tn.infer_district("Trichy Road, Coimbatore"), "Coimbatore")
        self.assertEqual(tn.infer_district("", "Tuticorin - 628001"), "Thoothukudi")
        self.assertEqual(tn.infer_district("no place here"), "")

    def test_malformed_and_missing_fields(self):
        self.assertEqual(tn.parse_table("<html><body>maintenance</body></html>",
                                        category="building", mode="online", source_url="u"), [])
        broken = ("<table id='example1'><tr><th>S. No</th><th>Project Registration No.</th>"
                  "<th>Name and Address of the Promoter</th></tr>"
                  "<tr><td>1</td><td>TN/1/Building/0001/2017 dated 31/07/2017</td></tr>"
                  "<tr><td>2</td></tr></table>")
        records = tn.parse_table(broken, category="building", mode="offline", source_url="u")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["rera_number"], "TN/1/Building/0001/2017")
        self.assertEqual(records[0]["promoter_name"], "")
        self.assertEqual(records[0]["proposed_completion_date"], "")

    def test_fetch_source_routes_to_the_right_request(self):
        session = mock.Mock()
        session.get.return_value.text = fixture("tn_online_building.html")
        session.post.return_value.text = fixture("tn_online_building.html")
        scraper = tn.TamilNaduScraper(session)
        scraper._token = "T"
        scraper.fetch_source("building-online-2025")
        session.post.assert_called_once_with(tn.BASE + "/registered-building/tn",
                                             data={"_token": "T", "year": "2025"})
        scraper.fetch_source("layout-offline-2019")
        session.get.assert_called_with(tn.BASE + "/layout/offline/2019")


# ─── Karnataka ──────────────────────────────────────────────────────────────

class KarnatakaTests(unittest.TestCase):
    def setUp(self):
        self.records = ka.parse_results(fixture("ka_district_kodagu.html"), "Kodagu", "u")

    def test_district_results(self):
        self.assertEqual(len(self.records), 12)
        first = self.records[0]
        self.assertEqual(first["rera_number"], "PRM/KA/RERA/1264/437/PR/171103/001649")
        self.assertEqual(first["application_no"], "PR/KN/170830/001649")
        self.assertEqual(first["project_name"], "BHOOMI ROW HOUSE VILLA")
        self.assertEqual(first["taluka"], "Madikeri")
        self.assertEqual(first["registration_date"], "2017-11-03")
        self.assertEqual(first["proposed_completion_date"], "2024-05-29")
        self.assertEqual(first["completion_at_registration"], "2023-08-31")
        self.assertEqual(first["project_status"], "withdrawn")
        self.assertEqual(first["portal_project_id"], "2069")
        self.assertIn("/certificate?CER_NO=", first["certificate_url"])

    def test_rejected_application_without_registration_number(self):
        rejected = [r for r in self.records if r["project_status"] == "rejected"]
        self.assertTrue(rejected)
        self.assertTrue(all(r["rera_number"] == "" and r["application_no"] for r in rejected))
        records, invalid, _ = rc.consolidate(rejected, {"KA"})
        self.assertEqual(invalid, [])  # keyed by application number instead

    def test_status_mapping(self):
        self.assertEqual(ka._status("APPROVED"), "registered")
        self.assertEqual(ka._status("TRANSFERED to X"), "transferred")
        self.assertEqual(ka._status(""), "unknown")

    def test_district_list_and_autocomplete(self):
        self.assertEqual(ka.parse_districts(fixture("ka_district_kodagu.html")), ["Kodagu", "Udupi"])
        script = ("applicationNameList\n.push('ACK/1');\napplicationNameList2\n.push('PRM/1');"
                  "\napplicationNameList3\n.push('Proj');\napplicationNameList4\n.push('Prom');")
        self.assertEqual(ka.parse_autocomplete(script), [{
            "application_no": "ACK/1", "rera_number": "PRM/1",
            "project_name": "Proj", "promoter_name": "Prom"}])

    def test_autocomplete_gaps(self):
        scraper = ka.KarnatakaScraper(mock.Mock())
        scraper.autocomplete = [
            {"application_no": "PR/KN/170830/001649", "rera_number": "x", "project_name": "", "promoter_name": ""},
            {"application_no": "NEW", "rera_number": "PRM/NEW", "project_name": "N", "promoter_name": "P"},
        ]
        gaps = scraper.autocomplete_gaps(self.records)
        self.assertEqual([g["rera_number"] for g in gaps], ["PRM/NEW"])

    def test_page_without_results_table(self):
        self.assertEqual(ka.parse_results("<html><table><tr><td>x</td></tr></table></html>", "D", "u"), [])


# ─── Maharashtra ────────────────────────────────────────────────────────────

class MaharashtraTests(unittest.TestCase):
    def test_listing_cards(self):
        html = fixture("mh_listing_page1.html")
        records = mh.parse_cards(html, 1)
        self.assertEqual([r["rera_number"] for r in records],
                         ["P50500000005", "P51700000002", "P51700000001"])
        first = records[0]
        self.assertEqual(first["project_name"], "GREEN CITY 3")
        self.assertEqual(first["promoter_name"], "GREEN SPACE INFRA VENTURES")
        self.assertEqual((first["district"], first["pincode"]), ("Nagpur", "441108"))
        self.assertEqual(first["project_id"], "1")
        self.assertEqual(mh.parse_total(html), 49607)
        self.assertEqual(mh.parse_page_count(html), 4961)

    def test_mapping_to_database_shape(self):
        records = {r["rera_number"]: {**r, "status": "deregistered"} if i == 1 else r
                   for i, r in enumerate(mh.parse_cards(fixture("mh_listing_page1.html"), 1))}
        mapped = mh.to_common_records(records)
        self.assertEqual(mapped["P51700000002"]["project_status"], "deregistered")
        first = mapped["P50500000005"]
        self.assertEqual(first["state_code"], "MH")
        self.assertEqual(first["taluka"], "Nagpur (Rural)")
        self.assertEqual(first["promoter_normalized"], "GREEN SPACE INFRA VENTURES")
        cleaned, invalid, dupes = rc.consolidate(list(mapped.values()), {"MH"})
        self.assertEqual((len(cleaned), invalid, dupes), (3, [], []))

    def test_weekly_slices_cover_every_page_once(self):
        total = 4961
        slices = [mh.batch_pages(total, 0, 7, k) for k in range(7)]
        covered = [p for s in slices for p in s]
        self.assertEqual(sorted(covered), list(range(1, total + 1)))
        daily = mh.batch_pages(total, 30, 7, 0)
        self.assertIn(1, daily)
        self.assertEqual(daily[-30:], list(range(4932, 4962)))
        self.assertEqual(mh.batch_pages(5, 30, 7, 6), [1, 2, 3, 4, 5])

    def test_status_list_pattern_matches_old_and_new_numbers(self):
        text = "P51715176, P51900024490, PM1260002601983 and PR1330002602032; not P123"
        self.assertEqual(mh.RERA_PATTERN.findall(text),
                         ["P51715176", "P51900024490", "PM1260002601983", "PR1330002602032"])

    def test_empty_or_broken_page(self):
        self.assertEqual(mh.parse_cards("<html><body>Service unavailable</body></html>", 7), [])
        self.assertIsNone(mh.parse_total("<html></html>"))


if __name__ == "__main__":
    unittest.main()
