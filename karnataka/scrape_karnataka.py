#!/usr/bin/env python3
"""
Karnataka RERA project scraper.

The public "View All Projects" search (rera.karnataka.gov.in/viewAllProjects)
takes a district and returns every application filed in it - approved,
rejected, withdrawn, revoked - as one server-rendered table with status,
taluk, project type, approval date and all completion/extension dates. So the
whole register is one POST per district (31 districts), no browser, no CAPTCHA.

The search page also embeds an autocomplete list (application no, registration
no, project, promoter). It is capped at 10,000 entries, so it is only used as a
cross-check: anything in it that no district search returned is added as a
minimal row and counted in the run summary.

Verified 2026-09-30: Bengaluru Urban alone returns ~4,400 rows (~32 MB, ~25 s).

Usage:
    python scrape_karnataka.py                  # full run (~5 minutes)
    python scrape_karnataka.py --limit 3        # smoke test: first 3 districts
    python scrape_karnataka.py --district Kodagu --district Udupi
    python scrape_karnataka.py --fresh          # discard an unfinished run
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.rera_common import (  # noqa: E402
    PoliteSession, RunStore, clean, finish_run, log, normalize_promoter,
    now_utc, parse_date, run_sources,
)

BASE = "https://rera.karnataka.gov.in"
SEARCH_PAGE = f"{BASE}/viewAllProjects"
SEARCH_POST = f"{BASE}/projectViewDetails"
STATE_CODE = "KA"
DATA_DIR = Path(__file__).parent / "data"

STATUS_MAP = {
    "APPROVED": "registered",
    "REJECTED": "rejected",
    "WITHDRAWN": "withdrawn",
    "REVOKED": "revoked",
    "TRANSFERED": "transferred",
    "TRANSFERRED": "transferred",
    "LAPSED": "lapsed",
    "COMPLETED": "completed",
}
NEGATIVE = {"rejected", "withdrawn", "revoked", "lapsed"}

AUTOCOMPLETE = re.compile(
    r"applicationNameList\s*\.push\('([^']*)'\);\s*"
    r"applicationNameList2\s*\.push\('([^']*)'\);\s*"
    r"applicationNameList3\s*\.push\('([^']*)'\);\s*"
    r"applicationNameList4\s*\.push\('([^']*)'\);")


# ─── Parsing ────────────────────────────────────────────────────────────────

def parse_districts(html):
    soup = BeautifulSoup(html, "lxml")
    select = soup.select_one("select[name=district]")
    if select is None:
        return []
    return [o.get("value") for o in select.select("option")
            if o.get("value") and o.get("value") != "0"]


def parse_autocomplete(html):
    return [{"application_no": clean(a), "rera_number": clean(r),
             "project_name": clean(p), "promoter_name": clean(m)}
            for a, r, p, m in AUTOCOMPLETE.findall(html)]


_HEADERS = {
    "acknowledgement no": "application_no",
    "registration no": "rera_number",
    "promoter name": "promoter_name",
    "project name": "project_name",
    "status": "status_remarks",
    "district": "district",
    "taluk": "taluka",
    "project type": "project_type",
    "approved on": "approved_on",
    "proposed completion date": "completion_current",
    "proposed completion date at the time of registration": "completion_at_registration",
    "covid-19 extension date": "covid_extension",
    "section 6 extension date": "section6_extension",
    "further extension date": "further_extension",
}


def _status(text):
    first = (text.split() or [""])[0].upper()
    return STATUS_MAP.get(first, first.lower() or "unknown")


def parse_results(html, district, source_url):
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#approvedTable")
    if table is None:
        return []
    rows = table.select("tr")
    if not rows:
        return []
    header = [clean(c.get_text(" ")).lower() for c in rows[0].select("th,td")]
    columns = {_HEADERS[h]: i for i, h in enumerate(header) if h in _HEADERS}
    if "application_no" not in columns and "rera_number" not in columns:
        return []
    scraped_at = now_utc()
    records = []
    for row in rows[1:]:
        cells = row.select("td")
        if not cells:
            continue
        raw = {field: clean(cells[i].get_text(" ")) if i < len(cells) else ""
               for field, i in columns.items()}
        detail = row.select_one('a[onclick*="showFileApplicationPreview"]')
        certificate = row.select_one('a[href^="/certificate"]')
        rera_number = raw.get("rera_number", "")
        records.append({
            "state_code": STATE_CODE,
            "rera_number": rera_number,
            "application_no": raw.get("application_no", ""),
            "project_name": raw.get("project_name", ""),
            "promoter_name": raw.get("promoter_name", ""),
            "promoter_normalized": normalize_promoter(raw.get("promoter_name", "")),
            "project_type": raw.get("project_type", ""),
            "district": raw.get("district", "") or district,
            "taluka": raw.get("taluka", ""),
            "pincode": "",
            "registration_date": parse_date(raw.get("approved_on", "")),
            # The portal's own 'current' completion date already includes extensions.
            "proposed_completion_date": parse_date(raw.get("completion_current", "")),
            "project_status": _status(raw.get("status_remarks", "")),
            "status_remarks": raw.get("status_remarks", ""),
            "detail_url": "",
            "source_url": source_url,
            # Karnataka specific
            "completion_at_registration": parse_date(raw.get("completion_at_registration", "")),
            "covid_extension": raw.get("covid_extension", ""),
            "section6_extension": raw.get("section6_extension", ""),
            "further_extension": raw.get("further_extension", ""),
            # Internal id for the portal's POST /projectDetails (action=<id>) popup.
            "portal_project_id": detail.get("id", "") if detail else "",
            "certificate_url": BASE + certificate["href"] if certificate else "",
            "scraped_at": scraped_at,
        })
    return records


# ─── Scraping ───────────────────────────────────────────────────────────────

class KarnatakaScraper:
    def __init__(self, session):
        self.session = session
        self.autocomplete = []

    def discover_sources(self):
        html = self.session.get(SEARCH_PAGE).text
        districts = parse_districts(html)
        if not districts:
            raise RuntimeError("district list not found on the search page")
        self.autocomplete = parse_autocomplete(html)
        log(f"Search page lists {len(districts)} districts; "
            f"autocomplete carries {len(self.autocomplete):,} entries.")
        return districts

    def fetch_source(self, district):
        html = self.session.post(SEARCH_POST, data={
            "project": "", "firm": "", "appNo": "", "regNo": "",
            "district": district, "subdistrict": "0", "btn1": "Search",
        }).text
        return parse_results(html, district, f"{SEARCH_POST}?district={district}")

    def autocomplete_gaps(self, records):
        """Entries in the embedded list that no district search returned."""
        seen = {r.get("rera_number") for r in records} | {r.get("application_no") for r in records}
        gaps = []
        for entry in self.autocomplete:
            if entry["rera_number"] in seen or entry["application_no"] in seen:
                continue
            gaps.append({
                **entry,
                "state_code": STATE_CODE,
                "promoter_normalized": normalize_promoter(entry["promoter_name"]),
                "project_status": "registered" if entry["rera_number"] else "unknown",
                "status_remarks": "listed only in the search autocomplete (no district result)",
                "source_url": SEARCH_PAGE + "#autocomplete",
                "scraped_at": now_utc(),
            })
        return gaps


# ─── Main ───────────────────────────────────────────────────────────────────

PROJECT_COLUMNS = [
    ("rera_number", "Registration No", 42), ("application_no", "Acknowledgement No", 42),
    ("project_name", "Project Name", 36), ("promoter_name", "Promoter", 36),
    ("project_status", "Status", 12), ("project_type", "Type", 24),
    ("district", "District", 16), ("taluka", "Taluk", 16),
    ("registration_date", "Approved On", 12),
    ("proposed_completion_date", "Completion (current)", 14),
    ("completion_at_registration", "Completion (at registration)", 14),
    ("covid_extension", "COVID Extension", 24), ("section6_extension", "Sec 6 Extension", 24),
    ("further_extension", "Further Extension", 24), ("status_remarks", "Status Remarks", 50),
    ("certificate_url", "Certificate", 50), ("portal_project_id", "Portal ID", 10),
    ("promoter_normalized", "Promoter (normalised)", 36), ("source_url", "Source", 50),
    ("first_seen_at", "First Seen", 21), ("last_changed_at", "Last Changed", 21),
    ("scraped_at", "Scraped At", 21),
]


def main():
    parser = argparse.ArgumentParser(description="Scrape Karnataka RERA projects.")
    parser.add_argument("--workers", type=int, default=2,
                        help="concurrent requests (default 2)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="minimum seconds between requests (default 2)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N districts (0 = all); for smoke tests")
    parser.add_argument("--district", action="append",
                        help="only these districts (repeatable), exact portal spelling")
    parser.add_argument("--fresh", action="store_true",
                        help="discard an unfinished run's saved districts")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    args = parser.parse_args()

    store = RunStore(args.data_dir, "karnataka")
    if args.fresh:
        store.clear_partial()
        log("Fresh run: unfinished-run state cleared.")

    started = datetime.now(timezone.utc)
    scraper = KarnatakaScraper(PoliteSession(delay=args.delay, timeout=300))

    log("Loading search page...")
    districts = scraper.discover_sources()
    if args.district:
        unknown = set(args.district) - set(districts)
        if unknown:
            log(f"Unknown district(s): {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(districts)}")
            return 1
        districts = [d for d in districts if d in args.district]
    if args.limit:
        districts = districts[:args.limit]
    partial_scope = bool(args.district or args.limit)

    failures = run_sources(store, districts, scraper.fetch_source, args.workers)
    records = store.load_partial_records()

    if not partial_scope and len(records) % 10_000 == 0:
        # On 2026-09-30 the district results summed to exactly 10,000, the same
        # cap as the autocomplete. Every registration number on the portal's
        # renewal, expired, rejected and defaulter lists was present, so this
        # looked like the true total - but a round number deserves a look.
        log(f"NOTE: district results total exactly {len(records):,}. If this stays "
            f"flat while the portal adds projects, the search may be capped.")
    gaps = [] if partial_scope else scraper.autocomplete_gaps(records)
    if gaps:
        log(f"{len(gaps):,} autocomplete entries were missing from district results - added.")
    records += gaps

    return finish_run(
        store, state_code=STATE_CODE, raw_records=records, failures=failures,
        started=started, project_columns=PROJECT_COLUMNS,
        flag=lambda r: r.get("project_status") in NEGATIVE,
        extra_summary={
            "districts": len(districts),
            "autocomplete_entries": len(scraper.autocomplete),
            "added_from_autocomplete": len(gaps),
            "limited_to": ", ".join(args.district) if args.district else (args.limit or "all"),
        },
        sources_total=len(districts), partial_scope=partial_scope,
    )


if __name__ == "__main__":
    sys.exit(main())
