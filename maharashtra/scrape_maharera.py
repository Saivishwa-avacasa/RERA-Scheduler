#!/usr/bin/env python3
"""
MahaRERA project scraper.

Pulls every registered project in Maharashtra from the MahaRERA public listing,
tags each one with any negative status found on the authority's separate status
lists, and writes the result to Excel.

The listing is plain server-rendered HTML behind GET pagination, so no browser
is needed. Verified 2026-09-09: 49,287 registered projects across 4,929 pages,
10 per page, ~2.75s per request.

Re-running is safe and cheap to reason about: every page is checkpointed, so an
interrupted run resumes where it stopped, and a completed run that finds no
changes says so instead of rewriting the same data.

Usage:
    python scrape_maharera.py                 # full run (~1 hour with 4 workers)
    python scrape_maharera.py --pages 5       # smoke test
    python scrape_maharera.py --status-only   # refresh status lists only (~1 min)
    python scrape_maharera.py --fresh         # ignore checkpoint, start over
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.rera_common import PARTIAL_EXIT, normalize_promoter, sync_to_database  # noqa: E402

# ─── Configuration ──────────────────────────────────────────────────────────

BASE = "https://maharera.maharashtra.gov.in"
LISTING = f"{BASE}/projects-search-result"
MAHARASHTRA_STATE_CODE = "27"

# Status lists. Verified 2026-09-09: `deregistered` and `nclt` render their
# tables server-side and parse cleanly. The other three build their tables with
# JavaScript and return nothing over plain HTTP, so they are reported as
# INCOMPLETE rather than silently counted as zero — an empty status list would
# wrongly mark a revoked project as clean.
STATUS_LISTS = {
    "deregistered": "/list_of_projects_deregistered",
    "nclt": "/nclt-projects",
    "revoked": "/projects-registration-revoked-initio-void",
    "dereg_notice": "/project-de-registration-notices",
    "lapsed": "/lapsed-project-underconstruction",
}
RELIABLE_STATUS_LISTS = {"deregistered", "nclt"}

# RERA numbers run from 9 to 12 characters: P51715176 and P51900024490 are both real.
RERA_PATTERN = re.compile(r"\bP[A-Z]?\d{7,}\b")  # 2025+ numbers: PM1260002601983, PR..., PC...

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

DATA_DIR = Path(__file__).parent / "data"
RAW_FILE = DATA_DIR / "raw.ndjson"
CHECKPOINT_FILE = DATA_DIR / "checkpoint.json"
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
EXCEL_FILE = DATA_DIR / "maharashtra_rera.xlsx"
RUNLOG_FILE = DATA_DIR / "run_log.json"

# Fields compared when deciding whether a record actually changed. Volatile
# bookkeeping (scraped_at, source_url) is excluded so an unchanged project is
# not reported as modified on every run.
COMPARED_FIELDS = [
    "project_name", "promoter_name", "district", "taluka", "state",
    "pincode", "last_modified", "extension_certificate", "status", "detail_url",
]

print_lock = Lock()


def log(msg):
    with print_lock:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ─── HTTP ───────────────────────────────────────────────────────────────────

def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch(session, url, params=None, attempts=4):
    """GET with backoff. Raises after the final attempt so callers can record
    a failed page rather than treating it as an empty one."""
    delay = 2
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=60)
            if response.status_code == 200:
                return response.text
            # 429/5xx are worth waiting out; 4xx generally are not.
            if response.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {response.status_code}")
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - retry on any transport error
            last_error = exc
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise last_error


def listing_params(page):
    return {
        "project_name": "",
        "project_location": "",
        "project_completion_date": "",
        "project_state": MAHARASHTRA_STATE_CODE,
        "project_district": "0",
        "carpetAreas": "",
        "completionPercentages": "",
        "project_division": "",
        "op": "",
        "page": page,
    }


# ─── Parsing ────────────────────────────────────────────────────────────────

def parse_total(html):
    """The listing prints its own total: 'Showing Final <span>49287</span> Result'."""
    soup = BeautifulSoup(html, "lxml")
    span = soup.select_one("span.colorBlue")
    if span:
        digits = re.sub(r"[^\d]", "", span.get_text())
        if digits:
            return int(digits)
    match = re.search(r"Showing Final\s*([\d,]+)", soup.get_text())
    return int(match.group(1).replace(",", "")) if match else None


def parse_page_count(html):
    match = re.search(r"of\s*([\d,]+)", BeautifulSoup(html, "lxml").get_text())
    return int(match.group(1).replace(",", "")) if match else None


def _labelled(card, label):
    """Card fields are a `div.greyColor` label followed by its value element."""
    for node in card.select("div.greyColor"):
        if node.get_text(strip=True).lower() == label.lower():
            value = node.find_next_sibling(["p", "a", "div", "span"])
            return value.get_text(" ", strip=True) if value else ""
    return ""



def _extension_status(card):
    """The card shows literal "N/A" when no extension exists, and an icon link
    (title="View Extension Certificate", no text) when one was granted - which
    reads as an empty string. Normalise both into a usable value."""
    for node in card.select("div.greyColor"):
        if node.get_text(strip=True).lower() == "extension certificate":
            value = node.find_next_sibling(["p", "a", "div", "span"])
            if value is None:
                return "unknown"
            text = value.get_text(" ", strip=True)
            if text.upper() == "N/A":
                return "none"
            title = (value.get("title") or "") if value.name == "a" else ""
            if "extension certificate" in title.lower():
                return "granted"
            return text or "unknown"
    return "unknown"


def parse_cards(html, page):
    soup = BeautifulSoup(html, "lxml")
    records = []

    for card in soup.select("div.row.shadow"):
        number_node = card.select_one("p.p-0")
        rera_number = ""
        if number_node:
            rera_number = number_node.get_text(strip=True).lstrip("#").strip()
        if not rera_number:
            continue

        title = card.select_one("h4.title4")
        promoter = card.select_one("p.darkBlue")
        location = card.select_one("ul.listingList li a")
        detail = card.select_one('a[title="View Details"]')
        detail_url = detail["href"] if detail and detail.has_attr("href") else ""
        project_id = ""
        if detail_url:
            match = re.search(r"/view/(\d+)", detail_url)
            project_id = match.group(1) if match else ""

        records.append({
            "rera_number": rera_number,
            "project_name": title.get_text(" ", strip=True) if title else "",
            "promoter_name": promoter.get_text(" ", strip=True) if promoter else "",
            "district": _labelled(card, "District"),
            "taluka": location.get_text(" ", strip=True) if location else "",
            "state": _labelled(card, "State"),
            "pincode": _labelled(card, "Pincode"),
            "last_modified": _labelled(card, "Last Modified"),
            "extension_certificate": _extension_status(card),
            "project_id": project_id,
            "detail_url": detail_url,
            "status": "registered",
            "source_page": page,
            "source_url": f"{LISTING}?project_state={MAHARASHTRA_STATE_CODE}&page={page}",
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    return records


# ─── Status lists ───────────────────────────────────────────────────────────

def scrape_status_lists(session):
    """Returns (status_by_rera, report). A list that cannot be parsed is marked
    INCOMPLETE so its absence is never mistaken for a clean record."""
    status_by_rera = {}
    report = []

    for name, path in STATUS_LISTS.items():
        url = BASE + path
        try:
            html = fetch(session, url)
            numbers = sorted(set(RERA_PATTERN.findall(html)))
            rows = len(BeautifulSoup(html, "lxml").select("tr"))
        except Exception as exc:  # noqa: BLE001
            report.append({"list": name, "url": url, "found": 0,
                           "state": "FAILED", "detail": str(exc)[:120]})
            log(f"  {name:14s} FAILED — {exc}")
            continue

        reliable = name in RELIABLE_STATUS_LISTS
        if numbers:
            for number in numbers:
                status_by_rera[number] = name
        state = "OK" if reliable and numbers else "INCOMPLETE"
        detail = "" if state == "OK" else (
            "table is JavaScript-rendered; plain HTTP returns no RERA numbers. "
            "Projects on this list will NOT be flagged."
        )
        report.append({"list": name, "url": url, "found": len(numbers),
                       "rows_in_html": rows, "state": state, "detail": detail})
        log(f"  {name:14s} {len(numbers):5d} numbers  [{state}]")

    return status_by_rera, report


# ─── Checkpointing ──────────────────────────────────────────────────────────

def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt checkpoint should not be fatal
            log("Checkpoint unreadable — starting fresh.")
    return {"completed_pages": [], "failed_pages": [], "total_pages": None}


def save_checkpoint(checkpoint):
    CHECKPOINT_FILE.write_text(json.dumps(checkpoint), encoding="utf-8")


def load_raw_records():
    """Rebuild the record set from the append-only NDJSON written during scraping."""
    records = {}
    if not RAW_FILE.exists():
        return records
    with RAW_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                records[record["rera_number"]] = record
            except Exception:  # noqa: BLE001 - skip a truncated final line
                continue
    return records


# ─── Scraping ───────────────────────────────────────────────────────────────

def scrape_listing(session, workers, page_limit, checkpoint):
    total_pages = checkpoint.get("total_pages")
    if not total_pages:
        log("Fetching page 1 to determine size...")
        html = fetch(session, LISTING, listing_params(1))
        total_pages = parse_page_count(html)
        total_records = parse_total(html)
        checkpoint["total_pages"] = total_pages
        checkpoint["total_records_reported"] = total_records
        log(f"Portal reports {total_records:,} projects across {total_pages:,} pages.")

    last_page = min(total_pages, page_limit) if page_limit else total_pages
    done = set(checkpoint.get("completed_pages", []))
    todo = [p for p in range(1, last_page + 1) if p not in done]

    if not todo:
        log(f"All {last_page:,} pages already scraped in a previous run.")
        return checkpoint

    # Everything not completed is in `todo`, including earlier failures, so the
    # failure list is rebuilt from this attempt only.
    checkpoint["failed_pages"] = []
    log(f"{len(todo):,} pages to fetch ({len(done):,} already done), {workers} workers.")
    started = time.time()
    raw_handle = RAW_FILE.open("a", encoding="utf-8")
    write_lock = Lock()
    completed = 0

    def work(page):
        html = fetch(session, LISTING, listing_params(page))
        return page, parse_cards(html, page)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, page): page for page in todo}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    page, records = future.result()
                except Exception as exc:  # noqa: BLE001
                    checkpoint.setdefault("failed_pages", []).append(page)
                    log(f"  page {page} FAILED: {str(exc)[:80]}")
                    continue

                if not records:
                    # HTTP 200 with no cards is a transient portal glitch, not an
                    # empty page. Recording it as complete silently loses 10 rows.
                    checkpoint.setdefault("failed_pages", []).append(page)
                    log(f"  page {page} returned 0 records - will retry on next run")
                    continue

                with write_lock:
                    for record in records:
                        raw_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    raw_handle.flush()
                    checkpoint["completed_pages"].append(page)
                    completed += 1
                    if completed % 25 == 0:
                        save_checkpoint(checkpoint)
                        elapsed = time.time() - started
                        rate = completed / elapsed if elapsed else 0
                        remaining = (len(todo) - completed) / rate if rate else 0
                        log(f"  {completed:,}/{len(todo):,} pages "
                            f"({rate:.1f}/s, ~{remaining/60:.0f} min left)")
    finally:
        raw_handle.close()
        save_checkpoint(checkpoint)

    log(f"Listing done in {(time.time() - started)/60:.1f} min.")
    return checkpoint


# ─── Diffing ────────────────────────────────────────────────────────────────

def load_snapshot():
    if SNAPSHOT_FILE.exists():
        try:
            return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def diff_records(previous, current):
    added, changed, removed = [], [], []
    for number, record in current.items():
        old = previous.get(number)
        if old is None:
            added.append(number)
            continue
        deltas = {
            field: (old.get(field), record.get(field))
            for field in COMPARED_FIELDS
            if old.get(field) != record.get(field)
        }
        if deltas:
            changed.append({"rera_number": number, "changes": deltas})
    for number in previous:
        if number not in current:
            removed.append(number)
    return added, changed, removed


# ─── Excel ──────────────────────────────────────────────────────────────────

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
FLAG_FILL = PatternFill("solid", fgColor="FCE4E4")

SHEET_COLUMNS = [
    ("rera_number", "RERA Number", 20),
    ("project_name", "Project Name", 42),
    ("promoter_name", "Promoter", 38),
    ("status", "Status", 15),
    ("district", "District", 16),
    ("taluka", "Taluka", 22),
    ("pincode", "Pincode", 10),
    ("state", "State", 14),
    ("last_modified", "Last Modified", 14),
    ("extension_certificate", "Extension Cert", 15),
    ("project_id", "Project ID", 11),
    ("detail_url", "Detail URL", 46),
    ("scraped_at", "Scraped At", 22),
]


def _style_header(sheet, headers, widths):
    for index, (title, width) in enumerate(zip(headers, widths), start=1):
        cell = sheet.cell(row=1, column=index, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"


def write_excel(records, status_report, run_summary, path):
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "Projects"
    _style_header(sheet,
                  [label for _, label, _ in SHEET_COLUMNS],
                  [width for _, _, width in SHEET_COLUMNS])
    for record in sorted(records.values(), key=lambda r: r["rera_number"]):
        row = [record.get(key, "") for key, _, _ in SHEET_COLUMNS]
        sheet.append(row)
        if record.get("status") != "registered":
            for column in range(1, len(SHEET_COLUMNS) + 1):
                sheet.cell(row=sheet.max_row, column=column).fill = FLAG_FILL
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(SHEET_COLUMNS))}{sheet.max_row}"

    flagged = workbook.create_sheet("Flagged")
    _style_header(flagged,
                  [label for _, label, _ in SHEET_COLUMNS],
                  [width for _, _, width in SHEET_COLUMNS])
    for record in sorted(records.values(), key=lambda r: r["rera_number"]):
        if record.get("status") != "registered":
            flagged.append([record.get(key, "") for key, _, _ in SHEET_COLUMNS])

    sources = workbook.create_sheet("Status Lists")
    _style_header(sources,
                  ["List", "URL", "Numbers Found", "Rows in HTML", "State", "Detail"],
                  [16, 62, 14, 14, 14, 70])
    for entry in status_report:
        sources.append([entry["list"], entry["url"], entry["found"],
                        entry.get("rows_in_html", ""), entry["state"], entry["detail"]])

    summary = workbook.create_sheet("Run Summary")
    _style_header(summary, ["Field", "Value"], [30, 60])
    for key, value in run_summary.items():
        summary.append([key, str(value)])

    workbook.save(path)


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape MahaRERA registered projects.")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent requests (default 4, ~1.5 req/s)")
    parser.add_argument("--pages", type=int, default=0,
                        help="stop after N pages (0 = all); use for smoke tests")
    parser.add_argument("--status-only", action="store_true",
                        help="refresh status lists and rebuild Excel, skip the listing")
    parser.add_argument("--fresh", action="store_true",
                        help="discard checkpoint and scraped rows, start over")
    parser.add_argument("--out", default=str(EXCEL_FILE), help="Excel output path")
    parser.add_argument("--batch", action="store_true",
                        help="daily batch: newest pages + one weekly slice, database only "
                             "(no checkpoint/Excel); every page is covered once a week")
    parser.add_argument("--newest", type=int, default=30,
                        help="--batch: how many of the last (newest) pages to include")
    parser.add_argument("--slices", type=int, default=7,
                        help="--batch: split the listing into this many slices")
    parser.add_argument("--slice", type=int, default=None,
                        help="--batch: which slice (0-based); default = UTC weekday")
    args = parser.parse_args()

    if args.batch:
        return run_batch(args)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.fresh:
        for path in (RAW_FILE, CHECKPOINT_FILE):
            path.unlink(missing_ok=True)
        log("Fresh run: checkpoint and raw rows cleared.")

    checkpoint = load_checkpoint()
    sweep_done = (checkpoint.get("total_pages")
                  and len(set(checkpoint.get("completed_pages", []))) >= checkpoint["total_pages"]
                  and not checkpoint.get("failed_pages"))
    if sweep_done and not args.status_only and not args.pages:
        # The previous full sweep finished. Start a new one so new and changed
        # projects are picked up; an *interrupted* sweep still resumes.
        for path in (RAW_FILE, CHECKPOINT_FILE):
            path.unlink(missing_ok=True)
        log("Previous sweep was complete - starting a new sweep.")

    started = datetime.now(timezone.utc)
    session = make_session()

    log("Fetching status lists...")
    status_by_rera, status_report = scrape_status_lists(session)

    checkpoint = load_checkpoint()  # re-read: the sweep reset above may have cleared it
    if not args.status_only:
        checkpoint = scrape_listing(session, args.workers, args.pages, checkpoint)
    else:
        log("--status-only: skipping the project listing.")

    records = load_raw_records()
    if not records:
        log("No project rows on disk. Run without --status-only first.")
        return 1

    for number, record in records.items():
        record["status"] = status_by_rera.get(number, "registered")

    previous = load_snapshot()
    added, changed, removed = diff_records(previous, records)

    flagged = sum(1 for r in records.values() if r["status"] != "registered")
    incomplete = [e["list"] for e in status_report if e["state"] != "OK"]

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    summary = {
        "run_started_utc": started.isoformat(timespec="seconds"),
        "duration_seconds": round(duration, 1),
        "total_projects": len(records),
        "portal_reported_total": checkpoint.get("total_records_reported", "unknown"),
        "pages_completed": len(checkpoint.get("completed_pages", [])),
        "pages_failed": len(checkpoint.get("failed_pages", [])),
        "flagged_projects": flagged,
        "new_since_last_run": len(added),
        "changed_since_last_run": len(changed),
        "disappeared_since_last_run": len(removed),
        "status_lists_incomplete": ", ".join(incomplete) or "none",
    }

    log("Writing Excel...")
    write_excel(records, status_report, summary, args.out)

    SNAPSHOT_FILE.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    history = []
    if RUNLOG_FILE.exists():
        try:
            history = json.loads(RUNLOG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            history = []
    history.append(summary)
    RUNLOG_FILE.write_text(json.dumps(history[-90:], indent=2), encoding="utf-8")

    print()
    if not previous:
        log(f"No local snapshot (first run, or a fresh container): {len(records):,} "
            f"projects scraped. The database line reports what was new or changed.")
    elif not (added or changed or removed):
        log(f"Already up to date - {len(records):,} projects, nothing changed.")
    else:
        log(f"Updated: {len(added):,} new, {len(changed):,} changed, "
            f"{len(removed):,} no longer listed.")
        for entry in changed[:5]:
            fields = ", ".join(entry["changes"])
            log(f"    {entry['rera_number']}: {fields}")

    log(f"{flagged:,} projects carry a negative status (deregistered / NCLT).")
    if incomplete:
        log(f"WARNING: status lists not parsed: {', '.join(incomplete)}. "
            f"Projects on those lists are NOT flagged.")
    if checkpoint.get("failed_pages"):
        log(f"WARNING: {len(checkpoint['failed_pages'])} pages failed — "
            f"re-run to retry them.")
    log(f"Excel: {args.out}")

    failures = {f"page {p}": "failed" for p in checkpoint.get("failed_pages", [])}
    db_error = sync_to_database("MH", to_common_records(records), started, summary, failures)
    return 3 if db_error else 0


def batch_pages(total_pages, newest, slices, slice_index):
    """Pages for one daily batch: the newest `newest` pages (new registrations
    are appended at the end of the listing) plus contiguous slice
    `slice_index` of `slices`, so every page is visited once per cycle."""
    size = -(-total_pages // slices)
    start = slice_index * size + 1
    pages = set(range(start, min(total_pages, start + size - 1) + 1))
    pages |= set(range(max(1, total_pages - newest + 1), total_pages + 1))
    return sorted(pages)


def run_batch(args):
    """Scrape one batch of pages and sync it to the database. Keeps nothing
    on disk: in CI every run starts in a fresh container anyway."""
    started = datetime.now(timezone.utc)
    slice_index = args.slice if args.slice is not None else started.weekday() % args.slices
    session = make_session()

    log("Fetching status lists...")
    status_by_rera, status_report = scrape_status_lists(session)

    first = fetch(session, LISTING, listing_params(1))
    total_pages = parse_page_count(first)
    if not total_pages:
        log("Could not read the page count from page 1.")
        return 1
    pages = batch_pages(total_pages, args.newest, args.slices, slice_index)
    log(f"Portal reports {parse_total(first):,} projects / {total_pages:,} pages. "
        f"Batch: slice {slice_index + 1}/{args.slices} + newest {args.newest} "
        f"= {len(pages):,} pages, {args.workers} workers.")

    def work(page):
        return parse_cards(fetch(session, LISTING, listing_params(page)), page)

    records, failed = {}, list(pages)
    for attempt in (1, 2):  # the second pass retries failed or empty pages once
        todo, failed = failed, []
        if attempt == 2:
            log(f"{len(todo)} pages failed - retrying once.")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(work, page): page for page in todo}
            for done, future in enumerate(as_completed(futures), start=1):
                page = futures[future]
                try:
                    rows = future.result()
                except Exception as exc:  # noqa: BLE001
                    log(f"  page {page} FAILED: {str(exc)[:80]}")
                    rows = []
                if not rows:
                    failed.append(page)
                for row in rows:
                    records[row["rera_number"]] = row
                if done % 100 == 0:
                    log(f"  {done:,}/{len(todo):,} pages")
        if not failed:
            break

    for number, record in records.items():
        record["status"] = status_by_rera.get(number, "registered")
    incomplete = [e["list"] for e in status_report if e["state"] != "OK"]
    summary = {
        "mode": "batch",
        "run_started_utc": started.isoformat(timespec="seconds"),
        "duration_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "slice": f"{slice_index + 1}/{args.slices}",
        "newest_pages": args.newest,
        "pages_requested": len(pages),
        "pages_failed": len(failed),
        "total_projects": len(records),
        "portal_total_pages": total_pages,
        "flagged_projects": sum(1 for r in records.values() if r["status"] != "registered"),
        "status_lists_incomplete": ", ".join(incomplete) or "none",
    }
    log(f"Scraped {len(records):,} projects from {len(pages) - len(failed):,} pages.")
    if not records:
        log("ERROR: no projects collected - is the portal reachable from this machine?")
    if failed:
        log(f"WARNING: {len(failed)} pages still failed: {sorted(failed)[:20]}")
    failures = {f"page {p}": "failed" for p in failed}
    db_error = sync_to_database("MH", to_common_records(records), started, summary, failures)
    if db_error:
        return 3
    if not records:
        return 1
    return PARTIAL_EXIT if failed else 0


def to_common_records(records):
    """MahaRERA rows in the shape common/db.py stores (same columns as the
    other states); MahaRERA-only fields go to the JSONB `extra` column."""
    out = {}
    for number, r in records.items():
        out[number] = {
            "state_code": "MH",
            "rera_number": number,
            "project_name": r.get("project_name", ""),
            "promoter_name": r.get("promoter_name", ""),
            "promoter_normalized": normalize_promoter(r.get("promoter_name", "")),
            "project_type": "",
            "district": r.get("district", ""),
            "taluka": r.get("taluka", ""),
            "pincode": r.get("pincode", ""),
            # Not on the listing card (see README "Known gaps").
            "registration_date": "",
            "proposed_completion_date": "",
            "project_status": r.get("status", "registered"),
            "status_remarks": "",
            "detail_url": r.get("detail_url", ""),
            "source_url": r.get("source_url", ""),
            "last_modified": r.get("last_modified", ""),
            "extension_certificate": r.get("extension_certificate", ""),
            "portal_project_id": r.get("project_id", ""),
            "scraped_at": r.get("scraped_at", ""),
        }
    return out


if __name__ == "__main__":
    sys.exit(main())
