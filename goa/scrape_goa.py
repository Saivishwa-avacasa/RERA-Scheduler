#!/usr/bin/env python3
"""
Goa RERA project scraper - Auto-solves the CAPTCHA using ddddocr.

Goa's project register is only shown through the portal's search
(rera.goa.gov.in/reraApp), which asks for a CAPTCHA. This script attempts
to read the CAPTCHA automatically using a local OCR library (ddddocr). 
If it fails, it falls back to saving the image, opening it for you, and waiting
for you to type it. It then walks through every results page in the same
session and only asks again if the portal rejects the code.

Usage (from this folder, with the repo's .env holding DATABASE_URL):
    python scrape_goa.py                    # full run
    python scrape_goa.py --max-pages 1      # quick test: first results page only
"""

import argparse
import os
import re
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
import ddddocr

# Adjust the import path based on your project structure
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.rera_common import (  # noqa: E402
    PoliteSession, RunStore, clean, finish_run, log, normalize_promoter,
    now_utc, parse_date,
)

BASE = "https://rera.goa.gov.in/reraApp"
STATE_CODE = "GA"
DATA_DIR = Path(__file__).parent / "data"
GOA_RERA_NO = re.compile(r"\bPRGO\s*\d{6,}\b", re.I)
WRONG_CAPTCHA = "correct captcha"
PUBLIC_LISTS = {
    "withdrawn": "/projectsWithdrawn",
    "rejected": "/rejectedProjects",
    "cancelled": "/cancelledproject",
}


def _html(response):
    # Some pages are UTF-8, others Windows-1252, and none say which.
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode("cp1252", errors="replace")


# ─── Parsing ────────────────────────────────────────────────────────────────

def _record(name, number, promoter="", project_type="", address="", taluka="",
            village="", reg_date="", completion="", detail_url="", source_url="",
            extra=None):
    return {
        "state_code": STATE_CODE,
        "rera_number": re.sub(r"\s+", "", number.upper()),
        "project_name": name,
        "promoter_name": promoter,
        "promoter_normalized": normalize_promoter(promoter),
        "project_type": project_type,
        "district": "",
        "taluka": taluka,
        "pincode": "",
        "registration_date": parse_date(reg_date),
        "proposed_completion_date": parse_date(completion),
        "project_status": "registered",
        "status_remarks": "",
        "detail_url": detail_url,
        "source_url": source_url,
        "address": address,
        "village": village,
        **(extra or {}),
        "scraped_at": now_utc(),
    }


def parse_blocks(html, source_url):
    soup = BeautifulSoup(html, "lxml")
    records = []
    for heading in soup.find_all("h1"):
        if "project" not in heading.get_text(" ").lower():
            continue
        block = heading.find_parent("div")
        if block is None:
            continue
        text = block.get_text("\n")
        number = GOA_RERA_NO.search(text)
        if not number:
            continue
        name = re.sub(r"^\s*Project\s*:\s*", "", clean(heading.get_text(" ")), flags=re.I)
        address = ""
        for p in block.find_all("p", recursive=False) or block.find_all("p"):
            t = clean(p.get_text(" "))
            if t and not GOA_RERA_NO.search(t) and not p.find("a"):
                address = re.sub(r"\s*,[\s,]*", ", ", t).strip(" ,")
                break
        cells = [clean(td.get_text(" ")) for td in block.select("table tbody tr td")]
        link = block.select_one('a[href*="ProjectDetail"], a[href*="projectID"]')
        records.append(_record(
            name, number.group(0), promoter=cells[0] if cells else "",
            project_type=cells[2] if len(cells) > 2 else "", address=address,
            detail_url=(BASE + "/" + link["href"].lstrip("/")) if link else "",
            source_url=source_url,
            extra={"promoter_type": cells[1] if len(cells) > 1 else "",
                   "revoke_reason": cells[3] if len(cells) > 3 else ""}))
    return records


_HEADERS = [  
    ("registration no", "number"), ("reg. no", "number"), ("reg no", "number"),
    ("project name", "name"), ("promoter", "promoter"), ("developer", "promoter"),
    ("taluka", "taluka"), ("village", "village"), ("type", "project_type"),
    ("address", "address"), ("completion", "completion"), ("date", "reg_date"),
]


def parse_tables(html, source_url):
    soup = BeautifulSoup(html, "lxml")
    records = []
    for table in soup.select("table"):
        rows = table.select("tr")
        if len(rows) < 2:
            continue
        header = [clean(c.get_text(" ")).lower() for c in rows[0].select("th,td")]
        cols = {}
        for i, h in enumerate(header):
            for needle, field in _HEADERS:
                if needle in h and field not in cols:
                    cols[field] = i
                    break
        if "name" not in cols and "number" not in cols:
            continue
        for row in rows[1:]:
            cells = row.select("td")
            if not cells:
                continue
            raw = {f: clean(cells[i].get_text(" ")) if i < len(cells) else "" for f, i in cols.items()}
            blob = " ".join(clean(c.get_text(" ")) for c in cells)
            number = GOA_RERA_NO.search(raw.get("number", "") or blob)
            if not number:
                continue
            link = row.select_one("a[href]")
            href = link["href"] if link else ""
            records.append(_record(
                raw.get("name", ""), number.group(0), promoter=raw.get("promoter", ""),
                project_type=raw.get("project_type", ""), address=raw.get("address", ""),
                taluka=raw.get("taluka", ""), village=raw.get("village", ""),
                reg_date=raw.get("reg_date", ""), completion=raw.get("completion", ""),
                detail_url=(BASE + "/" + href.lstrip("/")) if href and not href.startswith(("http", "javascript", "#")) else "",
                source_url=source_url))
    return records


def parse_results(html, source_url):
    return parse_blocks(html, source_url) or parse_tables(html, source_url)


def next_offsets(html):
    """startFrom values offered by the pager links: pagging(<n>)."""
    return sorted({int(n) for n in re.findall(r"pagging\((\d+)\)", html)})


def parse_public_list(html, list_name):
    """Withdrawn / rejected / cancelled lists -> {normalised project name: list}."""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    rows = soup.select("table tr")
    if not rows:
        return out
    header = [clean(c.get_text(" ")).lower() for c in rows[0].select("th,td")]
    if "project name" not in header:
        return out
    idx = header.index("project name")
    for row in rows[1:]:
        cells = row.select("td")
        if len(cells) > idx:
            name = normalize_promoter(cells[idx].get_text(" "))
            if name:
                out[name] = list_name
    return out


# ─── Scraping ───────────────────────────────────────────────────────────────

class GoaScraper:
    def __init__(self, session, data_dir):
        self.session = session
        self.data_dir = Path(data_dir)
        self.captcha = None

    def ask_captcha(self):
        # 1. Download the image
        image = self.session.get(f"{BASE}/captcha.jpg")
        path = self.data_dir / "captcha.jpg"
        path.write_bytes(image.content)
        
        # 2. Try to solve it automatically with ddddocr
        try:
            ocr = ddddocr.DdddOcr(show_ad=False)
            with open(path, 'rb') as f:
                img_bytes = f.read()
            captcha_text = ocr.classification(img_bytes)
            print(f"🤖 Auto-solved CAPTCHA: {captcha_text}")
            
            # If OCR returns empty or fails, raise an exception
            if not captcha_text:
                raise ValueError("OCR returned empty string")
                
            return captcha_text
        except Exception as e:
            print(f"⚠️ Auto-solver failed: {e}.")
            
            # If running in GitHub Actions (no human), fail the script immediately
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError("CAPTCHA OCR failed and no user is available to type it.") from e
            
            # Fallback to manual for local PC runs
            try:
                if hasattr(os, "startfile"):
                    os.startfile(path)
                else:
                    webbrowser.open(path.as_uri())
            except OSError:
                pass
            print(f"\nThe CAPTCHA image is open (also saved at {path}).", flush=True)
            return input("Type the 6 characters and press Enter (blank = stop): ").strip()

    def search_page(self, offset):
        return _html(self.session.post(f"{BASE}/search", data={
            "Regtype": "Project", "projectDist": "0", "subDistrictId": "0",
            "villageId": "0", "typeOfLand": "0", "startFrom": str(offset),
            "searchTxt": "", "captcha": self.captcha or "",
        }))

    def crawl(self, max_pages=0):
        self.session.get(f"{BASE}/home")  # the CAPTCHA belongs to this session
        records, offset, page, seen = {}, 0, 0, set()
        while not max_pages or page < max_pages:
            if not self.captcha:
                self.captcha = self.ask_captcha()
                if not self.captcha:
                    log("No CAPTCHA entered - stopping.")
                    break
            html = self.search_page(offset)
            if WRONG_CAPTCHA in html.lower():
                if page == 0:
                    log("The portal says the CAPTCHA was wrong - trying again.")
                else:
                    log("The portal wants a new CAPTCHA to continue.")
                self.captcha = None
                continue
            page += 1
            seen.add(offset)
            (self.data_dir / f"search_page_{page}.html").write_text(html, encoding="utf-8")
            rows = parse_results(html, f"{BASE}/search?startFrom={offset}")
            for r in rows:
                records[r["rera_number"]] = r
            log(f"  page {page} (startFrom={offset}): {len(rows)} projects, {len(records)} total")
            if not rows:
                if page == 1:
                    log("ERROR: the first results page had no projects the parser recognises. "
                        f"Send {self.data_dir / 'search_page_1.html'} to the developer.")
                break
            upcoming = [o for o in next_offsets(html) if o > offset and o not in seen]
            if not upcoming:
                break
            offset = upcoming[0]
        return list(records.values())

    def negative_lists(self):
        names = {}
        for list_name, path in PUBLIC_LISTS.items():
            try:
                names.update(parse_public_list(_html(self.session.get(BASE + path)), list_name))
            except Exception as exc:  
                log(f"  {list_name} list failed: {exc}")
        return names


# ─── Main ───────────────────────────────────────────────────────────────────

PROJECT_COLUMNS = [
    ("rera_number", "RERA Number", 18), ("project_name", "Project Name", 36),
    ("promoter_name", "Promoter", 36), ("project_type", "Type", 20),
    ("project_status", "Status", 12), ("taluka", "Taluka", 14), ("village", "Village", 16),
    ("registration_date", "Registered On", 13),
    ("proposed_completion_date", "Completion", 13), ("address", "Address", 50),
    ("detail_url", "Detail URL", 50), ("promoter_normalized", "Promoter (normalised)", 36),
    ("first_seen_at", "First Seen", 21), ("last_changed_at", "Last Changed", 21),
    ("scraped_at", "Scraped At", 21),
]


def main():
    parser = argparse.ArgumentParser(description="Scrape Goa RERA projects (auto-solves CAPTCHA).")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="stop after N results pages (0 = all); for a quick test")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    args = parser.parse_args()

    store = RunStore(args.data_dir, "goa")
    started = datetime.now(timezone.utc)
    scraper = GoaScraper(PoliteSession(delay=args.delay, timeout=120), args.data_dir)

    log("Reading the public withdrawn / rejected / cancelled lists...")
    negative = scraper.negative_lists()
    log(f"  {len(negative)} projects on those lists.")

    log("Starting the project search - Auto-solving CAPTCHAs.")
    records = scraper.crawl(args.max_pages)
    if not records:
        log("No projects collected - nothing saved.")
        return 1
    for r in records:
        hit = negative.get(normalize_promoter(r["project_name"]))
        if hit:
            r["project_status"] = hit
            r["status_remarks"] = f"on the portal's {hit} list"

    store.save_source("search", records)
    return finish_run(
        store, state_code=STATE_CODE, failures={}, started=started,
        project_columns=PROJECT_COLUMNS,
        flag=lambda r: r.get("project_status") != "registered",
        extra_summary={"mode": "auto (ddddocr)", "negative_list_names": len(negative),
                       "limited_to_pages": args.max_pages or "all"},
        sources_total=1, partial_scope=bool(args.max_pages),
    )


if __name__ == "__main__":
    sys.exit(main())
