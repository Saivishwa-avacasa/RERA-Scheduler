#!/usr/bin/env python3
"""
TNRERA (Tamil Nadu) project scraper.

Tamil Nadu publishes registered projects as one server-rendered table per
category per year, so the whole register is ~30 requests with no browser:

    Online registrations   POST /registered-building/tn   year=2024..2026
                           POST /registered-layout/tn     year=2024..2026
                           GET  /registered_reglayout      (regularised layouts)
    Offline registrations  GET  /building/offline/<year>   2017..
                           GET  /layout/offline/<year>
                           GET  /regularisation/offline/<year>

The year lists are discovered from the portal on each run, so a new year shows
up without a code change. The online pages use a Laravel CSRF token, which is
read from the same public form a browser would submit - no login, no CAPTCHA.

Verified 2026-09-30.

Usage:
    python scrape_tn.py                  # full run (~5 minutes)
    python scrape_tn.py --limit 3        # smoke test: first 3 sources only
    python scrape_tn.py --fresh          # discard an unfinished run, start over
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.rera_common import (  # noqa: E402
    PoliteSession, RunStore, clean, find_pincode, finish_run, log,
    normalize_promoter, now_utc, parse_date, run_sources,
)

BASE = "https://rera.tn.gov.in"
STATE_CODE = "TN"
DATA_DIR = Path(__file__).parent / "data"

# Online, year-selectable registers (POST year=...).
ONLINE = {
    "building": "/registered-building/tn",
    "layout": "/registered-layout/tn",
}
# Online register with no year filter.
ONLINE_SINGLE = {"regularisation": "/registered_reglayout"}
# Landing pages that link to /<category>/offline/<year>.
OFFLINE_INDEX = {
    "building": "/building/list-project",
    "layout": "/layout/list-project",
    "regularisation": "/regularisation/list-project",
}
# Reference lists (identified by file number, not RERA number).
REFERENCE_LISTS = {"withdrawn": "/withdrawn-projects", "rejected": "/rejected-projects"}

PROJECT_TYPES = {
    "building": "Building",
    "layout": "Layout",
    "regularisation": "Regularised Layout",
}

# The 38 districts, with the spellings the portal actually uses.
DISTRICTS = {
    "Ariyalur": ["ariyalur"], "Chengalpattu": ["chengalpattu", "chengalpet"],
    "Chennai": ["chennai"], "Coimbatore": ["coimbatore"], "Cuddalore": ["cuddalore"],
    "Dharmapuri": ["dharmapuri"], "Dindigul": ["dindigul"], "Erode": ["erode"],
    "Kallakurichi": ["kallakurichi"],
    "Kancheepuram": ["kancheepuram", "kanchipuram", "kancheepuram"],
    "Kanniyakumari": ["kanniyakumari", "kanyakumari"], "Karur": ["karur"],
    "Krishnagiri": ["krishnagiri"], "Madurai": ["madurai"],
    "Mayiladuthurai": ["mayiladuthurai"], "Nagapattinam": ["nagapattinam"],
    "Namakkal": ["namakkal"], "Nilgiris": ["nilgiris", "udhagamandalam", "ooty"],
    "Perambalur": ["perambalur"], "Pudukkottai": ["pudukkottai", "pudukottai"],
    "Ramanathapuram": ["ramanathapuram"], "Ranipet": ["ranipet"], "Salem": ["salem"],
    "Sivaganga": ["sivaganga", "sivagangai"], "Tenkasi": ["tenkasi"],
    "Thanjavur": ["thanjavur"], "Theni": ["theni"],
    "Thoothukudi": ["thoothukudi", "tuticorin"],
    "Tiruchirappalli": ["tiruchirappalli", "tiruchirapalli", "trichy", "tiruchy"],
    "Tirunelveli": ["tirunelveli"], "Tirupathur": ["tirupathur", "tirupattur"],
    "Tiruppur": ["tiruppur", "tirupur"],
    "Tiruvallur": ["tiruvallur", "thiruvallur"],
    "Tiruvannamalai": ["tiruvannamalai", "thiruvannamalai"],
    "Tiruvarur": ["tiruvarur", "thiruvarur"], "Vellore": ["vellore"],
    "Viluppuram": ["viluppuram", "villupuram"], "Virudhunagar": ["virudhunagar"],
}
_DISTRICT_RE = re.compile(
    r"\b(" + "|".join(sorted({a for v in DISTRICTS.values() for a in v}, key=len, reverse=True)) + r")\b",
    re.I)
_ALIAS = {a: name for name, aliases in DISTRICTS.items() for a in aliases}

RERA_NO = re.compile(r"\b(TN(?:RERA)?/[^\s,|]+/\d{4})\b", re.I)


# ─── Parsing ────────────────────────────────────────────────────────────────

def infer_district(*texts):
    """District named in the address text. Prefers an explicit '<X> District',
    otherwise the last district name mentioned (addresses run small-to-large).
    Derived, not a portal field - the listing has no district column."""
    for text in texts:
        if not text:
            continue
        explicit = re.findall(_DISTRICT_RE.pattern + r"\s+(?:district|dist\.?)\b", text, re.I)
        if explicit:
            return _ALIAS[explicit[-1].lower()]
        hits = _DISTRICT_RE.findall(text)
        if hits:
            return _ALIAS[hits[-1].lower()]
    return ""


def _column_map(header_cells):
    """Map our field names to column indexes by header text, so a reordered
    or extra column does not silently shift every value."""
    mapping = {}
    for index, text in enumerate(header_cells):
        t = text.lower()
        if "registration no" in t:
            mapping["reg"] = index
        elif "promoter" in t:
            mapping["promoter"] = index
        elif t.startswith("project details"):
            mapping["details"] = index
        elif t.startswith("approval"):
            mapping["approval"] = index
        elif "completion" in t:
            mapping["completion"] = index
        elif "other details" in t:
            mapping["other"] = index
        elif "location" in t:
            mapping["location"] = index
        elif "status" in t:
            mapping["status"] = index
    return mapping


def _project_name(details):
    match = re.search(r"Project\s*Name\s*:?\s*(.+?)(?:\s\|\s|$)", details, re.I)
    if not match:
        return ""
    name = match.group(1)
    # Offline rows run the name into the description: '"Nexterra" - Construction of...'
    name = re.split(r"\s[-–]\s", name, maxsplit=1)[0]
    return clean(name.strip(" “”\"'.,:-"))


def _status(text):
    t = text.lower()
    if re.search(r"cancell?ed for|revoked for", t):
        return "partially_cancelled"
    if "revok" in t or "cancel" in t:
        return "revoked"
    if "lapse" in t:
        return "lapsed"
    if "completed" in t:
        return "completed"
    return "registered"


def parse_table(html, *, category, mode, source_url):
    """Rows of one TNRERA register table -> normalised records."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#example1") or soup.select_one("table")
    if table is None:
        return []
    rows = table.select("tr")
    if not rows:
        return []
    columns = _column_map([clean(c.get_text(" ")) for c in rows[0].select("th,td")])
    if "reg" not in columns:
        return []
    scraped_at = now_utc()
    records = []

    def cell(cells, key, sep=" | "):
        index = columns.get(key)
        if index is None or index >= len(cells):
            return ""
        return clean(cells[index].get_text(sep))

    for row in rows[1:]:
        cells = row.select("td")
        if len(cells) <= columns["reg"]:
            continue
        reg_text = cell(cells, "reg", " ")
        # The portal occasionally has stray spaces inside the number ("Layout/ 595/2024").
        match = RERA_NO.search(re.sub(r"\s*/\s*", "/", reg_text))
        rera_number = match.group(1) if match else ""
        dated = reg_text.split("dated", 1)[1] if "dated" in reg_text.lower() else reg_text
        promoter_text = cell(cells, "promoter")
        details = cell(cells, "details")
        completion_raw = cell(cells, "completion")
        status_raw = clean(re.sub(r"(\s*\|\s*)+", " | ", cell(cells, "status")).strip(" |"))
        completion_dates = [parse_date(part) for part in re.split(r"\||;|upto|up to", completion_raw)]
        completion_dates = [d for d in completion_dates if d]

        links = {}
        for anchor in row.select("a[href]"):
            href = anchor["href"]
            if "/public-view2/" in href:
                links["detail_url"] = href
            elif "/public-view1/" in href:
                links["promoter_url"] = href
            elif "Current_Status" in href or "formcqr" in href:
                links.setdefault("status_document_url", href)
            elif "Project_Details" in href:
                links.setdefault("detail_url", href)

        other = cell(cells, "other", " ") + " " + cell(cells, "location", " ")
        lat = re.search(r"Latitude\s*[-:]?\s*([0-9][0-9.°'\"NS ]*)", other, re.I)
        lng = re.search(r"Longitude\s*[-:]?\s*([0-9][0-9.°'\"EW ]*)", other, re.I)
        promoter_name = clean(re.split(r",|\|", promoter_text, maxsplit=1)[0])

        records.append({
            "state_code": STATE_CODE,
            "rera_number": rera_number,
            "project_name": _project_name(details),
            "promoter_name": promoter_name,
            "promoter_normalized": normalize_promoter(promoter_name),
            "project_type": PROJECT_TYPES[category],
            "district": infer_district(details, promoter_text),
            "taluka": "",
            # Project-site PIN only; the promoter's office PIN would mislead.
            "pincode": find_pincode(details, "6"),
            "registration_date": parse_date(dated),
            "proposed_completion_date": max(completion_dates) if completion_dates else "",
            "project_status": _status(status_raw),
            "status_remarks": status_raw,
            "detail_url": links.get("detail_url", ""),
            "source_url": source_url,
            # Tamil Nadu specific
            "registration_mode": mode,
            "registration_raw": reg_text,
            "promoter_address": promoter_text,
            "project_description": details,
            "approval_details": cell(cells, "approval"),
            "completion_raw": completion_raw,
            "latitude": clean(lat.group(1)) if lat else "",
            "longitude": clean(lng.group(1)) if lng else "",
            "promoter_url": links.get("promoter_url", ""),
            "status_document_url": links.get("status_document_url", ""),
            "scraped_at": scraped_at,
        })
    return records


def parse_reference_list(html, list_name, source_url):
    """Withdrawn / rejected lists: file number + promoter + site, no RERA no."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("table tr")
    if not rows:
        return []
    header = [clean(c.get_text(" ")).lower() for c in rows[0].select("th,td")]
    out = []
    for row in rows[1:]:
        cells = [clean(c.get_text(" ")) for c in row.select("td")]
        if not any(cells):
            continue
        entry = dict(zip(header, cells))
        out.append({
            "list": list_name,
            "file_no": entry.get("file no.", entry.get("file no", "")),
            "promoter": next((v for k, v in entry.items() if "promoter" in k), ""),
            "site_address": entry.get("site address", ""),
            "remarks": entry.get("remarks", ""),
            "source_url": source_url,
        })
    return out


def offline_years(html, category):
    years = re.findall(rf"/{category}/offline/(\d{{4}})", html)
    return sorted(set(years))


def online_years(html):
    soup = BeautifulSoup(html, "lxml")
    return sorted({o.get("value") for o in soup.select("select[name=year] option")
                   if (o.get("value") or "").isdigit()})


def csrf_token(html):
    node = BeautifulSoup(html, "lxml").select_one("input[name=_token]")
    return node["value"] if node else ""


# ─── Scraping ───────────────────────────────────────────────────────────────

class TamilNaduScraper:
    def __init__(self, session):
        self.session = session
        self._token = None
        self._token_lock = Lock()

    def token(self):
        # Laravel binds the CSRF token to the session cookie, so one token
        # serves every POST made with this session.
        with self._token_lock:
            if not self._token:
                html = self.session.get(BASE + ONLINE["building"]).text
                self._token = csrf_token(html)
                if not self._token:
                    raise RuntimeError("CSRF token not found on the online register page")
            return self._token

    def discover_sources(self):
        """Every (category, mode, year) table the portal currently offers."""
        sources = []
        for category, path in ONLINE.items():
            html = self.session.get(BASE + path).text
            if category == "building" and not self._token:
                self._token = csrf_token(html)
            years = online_years(html)
            if not years:
                raise RuntimeError(f"no year options found on {path}")
            sources += [f"{category}-online-{y}" for y in years]
        sources += [f"{c}-online" for c in ONLINE_SINGLE]
        for category, path in OFFLINE_INDEX.items():
            html = self.session.get(BASE + path).text
            sources += [f"{category}-offline-{y}" for y in offline_years(html, category)]
        return sources

    def fetch_source(self, name):
        parts = name.split("-")
        category, mode = parts[0], parts[1]
        year = parts[2] if len(parts) > 2 else None
        if mode == "online" and year:
            url = BASE + ONLINE[category]
            html = self.session.post(url, data={"_token": self.token(), "year": year}).text
            source_url = f"{url}?year={year}"
            # A stale token redirects back to the default year; catch that
            # rather than filing 2026's rows under 2024.
            selected = BeautifulSoup(html, "lxml").select_one("select[name=year] option[selected]")
            if selected is not None and selected.get("value") not in (year, None):
                raise RuntimeError(f"portal served year {selected.get('value')} instead of {year}")
        elif mode == "online":
            url = BASE + ONLINE_SINGLE[category]
            html = self.session.get(url).text
            source_url = url
        else:
            url = f"{BASE}/{category}/offline/{year}"
            html = self.session.get(url).text
            source_url = url
        return parse_table(html, category=category, mode=mode, source_url=source_url)

    def fetch_reference_lists(self):
        out = []
        for name, path in REFERENCE_LISTS.items():
            try:
                out += parse_reference_list(self.session.get(BASE + path).text, name, BASE + path)
            except Exception as exc:  # noqa: BLE001 - reference data is optional
                log(f"  {name} list failed: {exc}")
        return out


# ─── Main ───────────────────────────────────────────────────────────────────

PROJECT_COLUMNS = [
    ("rera_number", "RERA Number", 30), ("project_name", "Project Name", 36),
    ("promoter_name", "Promoter", 36), ("project_type", "Type", 16),
    ("project_status", "Status", 12), ("district", "District (derived)", 16),
    ("pincode", "Pincode", 9), ("registration_date", "Registered On", 13),
    ("proposed_completion_date", "Completion (latest)", 14),
    ("registration_mode", "Online/Offline", 10), ("registration_raw", "Registration (as listed)", 34), ("status_remarks", "Status Remarks", 40),
    ("completion_raw", "Completion (as listed)", 30),
    ("project_description", "Project Details", 70), ("promoter_address", "Promoter Address", 50),
    ("approval_details", "Approval Details", 50), ("latitude", "Latitude", 14),
    ("longitude", "Longitude", 14), ("detail_url", "Detail URL", 50),
    ("promoter_url", "Promoter URL", 50), ("status_document_url", "Status Document", 50),
    ("promoter_normalized", "Promoter (normalised)", 36), ("source_url", "Source", 50),
    ("first_seen_at", "First Seen", 21), ("last_changed_at", "Last Changed", 21),
    ("scraped_at", "Scraped At", 21),
]

REFERENCE_COLUMNS = [
    ("list", "List", 12), ("file_no", "File No", 20), ("promoter", "Promoter", 50),
    ("site_address", "Site Address", 60), ("remarks", "Remarks", 50),
    ("source_url", "Source", 40),
]


def main():
    parser = argparse.ArgumentParser(description="Scrape TNRERA registered projects.")
    parser.add_argument("--workers", type=int, default=2,
                        help="concurrent requests (default 2)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="minimum seconds between requests (default 2)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N sources (0 = all); for smoke tests")
    parser.add_argument("--fresh", action="store_true",
                        help="discard an unfinished run's saved sources")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    args = parser.parse_args()

    store = RunStore(args.data_dir, "tamilnadu")
    if args.fresh:
        store.clear_partial()
        log("Fresh run: unfinished-run state cleared.")

    started = datetime.now(timezone.utc)
    scraper = TamilNaduScraper(PoliteSession(delay=args.delay))

    log("Discovering registers...")
    sources = scraper.discover_sources()
    if args.limit:
        sources = sources[:args.limit]
    log(f"{len(sources)} sources: {', '.join(sources)}")

    failures = run_sources(store, sources, scraper.fetch_source, args.workers)
    log("Fetching withdrawn / rejected reference lists...")
    reference = scraper.fetch_reference_lists()

    return finish_run(
        store, state_code=STATE_CODE, raw_records=store.load_partial_records(),
        failures=failures, started=started, project_columns=PROJECT_COLUMNS,
        flag=lambda r: r.get("project_status") in ("revoked", "lapsed"),
        extra_sheets=[("Withdrawn & Rejected", REFERENCE_COLUMNS, reference)],
        extra_summary={"limited_to_sources": args.limit or "all"},
        sources_total=len(sources), partial_scope=bool(args.limit),
    )


if __name__ == "__main__":
    sys.exit(main())
