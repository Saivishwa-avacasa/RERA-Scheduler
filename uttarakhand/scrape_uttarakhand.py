#!/usr/bin/env python3
"""
Uttarakhand RERA (UKRERA) project scraper.

Source: the authority's original portal, uttarakhandrera.uk.gov.in, which is
still updated (registrations up to the current month) and lists every
registered project on one public page, /viewRegisteredProjects - no login,
no CAPTCHA. The newer portal (ukrera.uk.gov.in) returns no projects to any
search, so it is not used.

  1. One request for the list: project name, address, RERA number, promoter,
     promoter type, property type, revocation reason, detail-page id.
  2. One request per project for its detail page: registration date, project
     end date, status, tehsil, land area.

Both hosts need legacy TLS renegotiation, which OpenSSL 3 refuses by default;
it is allowed for these two hosts only. Personal data on the detail pages
(applicant e-mail and mobile) is deliberately not collected.

Verified 2026-10-21: 480 projects.

Usage:
    python scrape_uttarakhand.py              # full run (~10 minutes)
    python scrape_uttarakhand.py --limit 2    # smoke test: first 2 batches of 50
    python scrape_uttarakhand.py --fresh      # discard an unfinished run
"""

import argparse
import re
import ssl
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.rera_common import (  # noqa: E402
    PoliteSession, RunStore, clean, finish_run, log, normalize_promoter,
    now_utc, run_sources,
)

BASE = "https://uttarakhandrera.uk.gov.in"
LISTING = f"{BASE}/viewRegisteredProjects"
DETAIL = f"{BASE}/viewProjectDetailPage"
STATE_CODE = "UK"
DATA_DIR = Path(__file__).parent / "data"
BATCH = 50  # projects per saved source (resume unit)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class LegacyTLS(HTTPAdapter):
    """The UKRERA servers still need legacy TLS renegotiation."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def make_session(delay):
    session = PoliteSession(delay=delay, timeout=180)
    session.session.mount(BASE, LegacyTLS())
    return session


# ─── Parsing ────────────────────────────────────────────────────────────────

def parse_uk_date(text):
    """'Wed Oct 07 14:50:01 IST 2026' or '04-09-2031' -> ISO date, else ''."""
    text = clean(text)
    m = re.search(r"\b([A-Za-z]{3})\s+(\d{1,2})\s+[\d:]+\s+\w+\s+(\d{4})\b", text)
    try:
        if m and m.group(1).lower() in _MONTHS:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))).isoformat()
        m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        pass
    return ""


def parse_listing(html):
    """Every project block on /viewRegisteredProjects."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for block in soup.select("div.defalter_result_list"):
        heading = block.select_one("h1")
        name = clean(heading.get_text(" ")) if heading else ""
        name = re.sub(r"^Project:\s*", "", name)
        paragraphs = block.select("div.no_pad_lft > p")
        address_lines, rera_number = [], ""
        for p in paragraphs:
            text = p.get_text("\n")
            m = re.search(r"Reg No\.\s*:\s*([A-Z0-9]+)", text)
            if m:
                rera_number = m.group(1)
            elif not p.select("a"):
                address_lines = [clean(x).strip(" ,") for x in text.split("\n") if clean(x).strip(" ,")]
        cells = [clean(td.get_text(" ")) for td in block.select("table tbody tr td")]
        link = block.select_one('a[href*="viewProjectDetailPage"]')
        project_id = ""
        if link:
            m = re.search(r"projectID=(\d+)", link["href"])
            project_id = m.group(1) if m else ""
        # Address ends with "<city>,<district>," then "Uttarakhand".
        city = district = ""
        if len(address_lines) >= 2:
            parts = [x.strip() for x in address_lines[-2].split(",") if x.strip()]
            if parts:
                city, district = parts[0], parts[-1]
        out.append({
            "rera_number": rera_number,
            "project_name": name,
            "address": re.sub(r"\s*,[\s,]*", ", ", ", ".join(address_lines)),
            "city": city,
            "district": district,
            "promoter_name": cells[0] if len(cells) > 0 else "",
            "promoter_type": cells[1] if len(cells) > 1 else "",
            "project_type": cells[2] if len(cells) > 2 else "",
            "revoke_reason": cells[3] if len(cells) > 3 else "",
            "portal_project_id": project_id,
        })
    return out


def parse_detail(html):
    """'Label : value' pairs from a project detail page."""
    soup = BeautifulSoup(html, "lxml")
    fields = {}
    for label_p in soup.select("p.text-right"):
        label = clean(label_p.get_text(" ")).rstrip(":").strip()
        col = label_p.find_parent("div")
        nxt = col.find_next_sibling("div") if col else None
        value = clean(nxt.get_text(" ")) if nxt else ""
        if label and label not in fields:
            fields[label] = value
    for span in soup.select("span.reg"):
        m = re.match(r"\s*([^:]+?)\s*:\s*(.*)$", span.get_text(" "))
        if m:
            fields.setdefault(clean(m.group(1)), clean(m.group(2)))
    return fields


def _status(listing, detail_status):
    if listing.get("revoke_reason"):
        return "revoked"
    s = (detail_status or "").lower()
    if "complet" in s:
        return "completed"
    if "lapse" in s:
        return "lapsed"
    return "registered"


def build_record(listing, detail, source_url):
    tehsil = detail.get("Tehsil/Sub District", "")
    return {
        "state_code": STATE_CODE,
        "rera_number": listing["rera_number"],
        "project_name": listing["project_name"],
        "promoter_name": listing["promoter_name"],
        "promoter_normalized": normalize_promoter(listing["promoter_name"]),
        "project_type": listing["project_type"] or detail.get("Project Type", ""),
        "district": listing["district"],
        "taluka": tehsil or listing["city"],
        "pincode": "",
        "registration_date": parse_uk_date(detail.get("Date of Registration", "")),
        "proposed_completion_date": parse_uk_date(detail.get("Project End Date", "")),
        "project_status": _status(listing, detail.get("Project Status")),
        "status_remarks": listing["revoke_reason"] or detail.get("Project Status", ""),
        "detail_url": f"{DETAIL}?projectID={listing['portal_project_id']}" if listing["portal_project_id"] else "",
        "source_url": source_url,
        # Uttarakhand specific (no applicant e-mail / mobile)
        "address": listing["address"],
        "city": listing["city"],
        "promoter_type": listing["promoter_type"],
        "registration_type": detail.get("Reg Type", ""),
        "land_area": detail.get("Total Area of Project Land", ""),
        "map_validity_date": parse_uk_date(detail.get("Approved Map Validity Date", "")),
        "revoke_reason": listing["revoke_reason"],
        "portal_project_id": listing["portal_project_id"],
        "scraped_at": now_utc(),
    }


# ─── Scraping ───────────────────────────────────────────────────────────────

class UttarakhandScraper:
    def __init__(self, session):
        self.session = session
        self.listing = {}

    def discover_sources(self):
        html = self.session.get(LISTING).text
        rows = [r for r in parse_listing(html) if r["rera_number"]]
        total = re.search(r"Total Records:\s*</?[^>]*>?\s*(\d+)", html)
        log(f"List: {len(rows)} projects"
            + (f" (portal says {total.group(1)})" if total else "") + ".")
        if not rows:
            raise RuntimeError("no projects found on the list page")
        self.listing = {r["rera_number"]: r for r in rows}
        keys = sorted(self.listing)
        self.batches = {f"details-{i // BATCH:03d}": keys[i:i + BATCH]
                        for i in range(0, len(keys), BATCH)}
        return list(self.batches)

    def fetch_source(self, name):
        records = []
        for number in self.batches[name]:
            row = self.listing[number]
            detail = {}
            if row["portal_project_id"]:
                try:
                    detail = parse_detail(self.session.get(
                        DETAIL, params={"projectID": row["portal_project_id"]}).text)
                except Exception as exc:  # noqa: BLE001 - keep the list data
                    log(f"  {number}: detail page failed ({type(exc).__name__}) - list data only")
            records.append(build_record(row, detail, LISTING))
        return records


# ─── Main ───────────────────────────────────────────────────────────────────

PROJECT_COLUMNS = [
    ("rera_number", "RERA Number", 20), ("project_name", "Project Name", 36),
    ("promoter_name", "Promoter", 36), ("project_type", "Type", 22),
    ("project_status", "Status", 12), ("district", "District", 14),
    ("taluka", "Tehsil", 14), ("registration_date", "Registered On", 13),
    ("proposed_completion_date", "Project End Date", 14),
    ("status_remarks", "Status (as listed)", 24), ("address", "Address", 50),
    ("land_area", "Land Area", 16), ("promoter_type", "Promoter Type", 14),
    ("detail_url", "Detail URL", 50), ("promoter_normalized", "Promoter (normalised)", 36),
    ("first_seen_at", "First Seen", 21), ("last_changed_at", "Last Changed", 21),
    ("scraped_at", "Scraped At", 21),
]


def main():
    parser = argparse.ArgumentParser(description="Scrape Uttarakhand RERA projects.")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="minimum seconds between requests (default 1)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N batches of 50 (0 = all); smoke tests")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    args = parser.parse_args()

    store = RunStore(args.data_dir, "uttarakhand")
    if args.fresh:
        store.clear_partial()
        log("Fresh run: unfinished-run state cleared.")

    started = datetime.now(timezone.utc)
    scraper = UttarakhandScraper(make_session(args.delay))
    log("Loading the registered-projects list...")
    sources = scraper.discover_sources()
    if args.limit:
        sources = sources[:args.limit]
    failures = run_sources(store, sources, scraper.fetch_source, args.workers)

    return finish_run(
        store, state_code=STATE_CODE, failures=failures, started=started,
        project_columns=PROJECT_COLUMNS,
        flag=lambda r: r.get("project_status") in ("revoked", "lapsed"),
        extra_summary={"listed_projects": len(scraper.listing),
                       "limited_to_batches": args.limit or "all"},
        sources_total=len(sources), partial_scope=bool(args.limit),
    )


if __name__ == "__main__":
    sys.exit(main())
