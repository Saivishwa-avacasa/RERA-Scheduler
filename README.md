# Scraper-for-RERA

Scrapes RERA registries into Excel for property listing verification.

```
Scraper-for-RERA/
├── requirements.txt
├── common/rera_common.py          # shared: polite HTTP, resume, diffing, Excel
├── maharashtra/scrape_maharera.py
├── tamilnadu/scrape_tn.py
├── karnataka/scrape_karnataka.py
└── tests/                         # offline tests + saved HTML fixtures
```

Each state writes to its own `data/` folder (created on first run):
`<state>_rera.xlsx` (the deliverable), `snapshot.json` (last run, for change
detection), `changes.ndjson` (field-level change history), `run_log.json`
(last 90 runs) and `partial/` (resume state of an unfinished run).

## Setup

```
pip install -r requirements.txt
```

## Database (CockroachDB / PostgreSQL)

All three scrapers (Maharashtra, Tamil Nadu, Karnataka) sync to a database when
`DATABASE_URL` is set, either as an environment variable or in `.env` at the
repo root (see `.env.example`). Without it they write Excel/JSON only.

- Tables are created on first run (`CREATE TABLE IF NOT EXISTS`), and missing
  columns are added to an existing `rera_projects` (`ADD COLUMN IF NOT EXISTS`).
  Nothing is dropped.
- Tables: `rera_projects` (unique on `state_code, rera_number`),
  `developers`, `project_developers`, `project_change_history`,
  `scrape_runs`, `scrape_errors`. State-specific fields go in `rera_projects.extra` (JSONB).
- The database decides what is new or changed, in batches of 500. Unchanged rows
  only get `last_scraped_at` updated. Each changed field is recorded in
  `project_change_history`.
- Karnataka applications that never got a registration number are stored with
  their acknowledgement number in `rera_number` (and in `application_no`).
- If the sync fails, the Excel file is still written and the exit code is `3`.
  Credentials are redacted from error messages.
- `certs/root.crt` is the public CA (ISRG Root X1) needed for `sslmode=verify-full`.

DB integration tests use a throwaway schema, which is dropped afterwards:
`RERA_DB_INTEGRATION=1 python -m unittest tests.test_db_integration -v`

## Docker

```
docker build -t rera-scraper .
docker run --rm --env-file .env rera-scraper tamilnadu/scrape_tn.py
docker run --rm --env-file .env rera-scraper karnataka/scrape_karnataka.py
docker run --rm --env-file .env rera-scraper maharashtra/scrape_maharera.py
```

`.env` is kept out of the image by `.dockerignore`; pass it with `--env-file`.
To keep the Excel output, mount a folder, for example
`-v %cd%\tamilnadu\data:/app/tamilnadu/data`. The database doesn't depend on those files.

## GitHub Actions (daily)

`.github/workflows/rera-sync.yml` runs every day at 00:30 UTC (06:00 IST).
Each state is a separate job that runs Python 3.12 directly on the runner (no Docker):

| Job | What | Time |
|---|---|---|
| tamilnadu | full refresh | ~3 min |
| karnataka | full refresh | ~3 min |
| maharashtra | `--batch`: newest 30 pages + 1/7 of the listing | ~15 min |

That's about 650 Actions minutes a month, inside the 2,000 free minutes for a
private repo. Setup:

1. Push this folder to a GitHub repository. `.env` and `*/data/` are gitignored.
2. Go to Settings → Secrets and variables → Actions → New repository secret, and
   add `DATABASE_URL` with the same value as `.env`.
3. Go to Actions → "RERA daily sync" → Run workflow to test it once. You can
   pick one state, or `full` for a complete Maharashtra sweep.

Exit code 4 (some pages failed) shows as a warning and the job stays green.
Any other non-zero code fails the job, and GitHub e-mails the repo
owner. `.github/workflows/tests.yml` runs the offline tests on every push.

## Running

### Tamil Nadu, Karnataka

```
cd tamilnadu && python scrape_tn.py                  # ~2 min, ~37,000 projects
cd karnataka && python scrape_karnataka.py           # ~3 min, ~10,000 projects/applications
```

Useful flags: `--limit N` (first N sources, smoke test), `--fresh` (discard an
unfinished run), `--workers`, `--delay` (seconds between requests, default 2).
Karnataka also takes `--district Kodagu --district Udupi`.

Exit codes: `0` ok, `1` nothing collected, `3` database sync failed, `4` finished but some sources failed
(re-run the same command; only the failed sources are fetched again).

Tests (no network, database tests skipped): `python -m unittest discover -s tests -v`

### Maharashtra

```
cd maharashtra
python scrape_maharera.py                 # full run, ~1 hour
python scrape_maharera.py --pages 5       # smoke test, ~10 seconds
python scrape_maharera.py --status-only   # refresh status lists only, ~1 minute
python scrape_maharera.py --fresh         # discard checkpoint, start over
python scrape_maharera.py --workers 6     # faster, higher block risk
python scrape_maharera.py --batch         # daily batch (database only), ~15 minutes
```

`--batch` scrapes the newest 30 pages, where new registrations land, plus one
seventh of the listing chosen by UTC weekday (`--slice`, `--slices`, `--newest`
override this). Every project is re-checked within 7 days. It writes to the
database only: no checkpoint, Excel or snapshot.

A run after a *completed* full sweep starts a new sweep, so new and changed
projects are picked up. An interrupted sweep still resumes. In the database,
Maharashtra rows have no registration or completion date, because the listing
card doesn't show them. `last_modified`, `extension_certificate` and the portal id
are stored in `extra`.

## What it collects

Maharashtra had **49,287 registered projects** across 4,929 listing pages when this
was written (the portal states its own total, which the scraper reads back and
records in the Run Summary sheet).

Per project: RERA number, project name, promoter, district, taluka, pincode,
state, last modified, extension certificate, project id, detail URL, status,
and the timestamp it was scraped.

`status` is `registered` unless the RERA number appears on one of the authority's
negative lists, in which case it becomes `deregistered` or `nclt`.

## Tamil Nadu and Karnataka - what is collected

Common columns in every workbook: RERA number, project name, promoter (as listed
and normalised), type, district, taluk, pincode, registration date, completion
date, status, detail URL, source URL, first seen, last changed, scraped at.
Sheets: Projects, Flagged (negative status), Changes This Run, Invalid Rows
(never silently dropped), Duplicates, Run Summary, plus state extras.

**Tamil Nadu** (rera.tn.gov.in) - verified 2026-09-30: 36,942 projects from 28
year-wise tables (online 2024-26, offline 2017-26; buildings, layouts,
regularised layouts). Plain HTTP; the online tables are a normal form POST with
the page's CSRF token. Caveats: the portal has no district column, so
`district` is *derived* from the project address text (blank for ~0.5%);
`pincode` is filled only when the project text contains one (~2%), never from
the promoter's office address; layouts show "Completed" instead of a
completion date. Withdrawn/rejected files (no RERA number) are in their own sheet.

**Karnataka** (rera.karnataka.gov.in) - verified 2026-09-30: 10,000 rows from one
search per district (31): 8,897 approved, 957 rejected, 101 withdrawn, 39
transferred, 6 revoked. Includes applications that never got a registration
number (rejected), keyed by acknowledgement number. The total is exactly
10,000, the same as the page's autocomplete cap; every registration number on
the portal's renewal, expired, rejected and defaulter lists (~4,800) was found,
so it appears complete, but the scraper prints a note whenever the total is a
multiple of 10,000. No pincode on this portal.

Goa was dropped: its register needs an e-mail OTP and a CAPTCHA on every
search, and this project does not bypass CAPTCHAs.

## Re-running

Safe and idempotent. Each page is checkpointed as it completes, so:

- An interrupted run resumes from where it stopped — same command.
- A completed run with no changes prints `Already up to date - N projects, nothing changed.`
- Otherwise it prints what is new, changed, or no longer listed, and the first
  few changed records with the fields that moved.

For a daily job the listing sweep is the expensive part. Status changes are what
actually matter for verification and cost about a minute, so a reasonable split is
`--status-only` daily and a full sweep weekly.

## Known gaps

Three of the five status lists build their tables in JavaScript and return
nothing over plain HTTP. They are reported as `INCOMPLETE` in the console, in
the **Status Lists** sheet, and as a warning at the end of every run:

| List | State |
|---|---|
| Deregistered (530) | OK |
| NCLT projects (334) | OK |
| Revoked / ab initio void | INCOMPLETE — 2 of ~10+ parsed |
| De-registration notices | INCOMPLETE — 44 rows, no RERA numbers in HTML |
| Lapsed under-construction | INCOMPLETE — no rows in HTML |

**A project on one of those three lists will be labelled `registered`.** That is
the one place this dataset can currently mislead, which is why it is surfaced
loudly rather than left as a silent zero. Fixing it means fetching those tables
the way their JavaScript does.

Also not collected: registration date, validity/expiry, and survey number. Those
are not on the listing card — they need the project detail page (a JS app) or the
original application PDF.

## Deployment notes

Pure `requests` + `BeautifulSoup`; no browser, no CAPTCHA, no login, so it runs
headless anywhere.

**Windows Task Scheduler** — daily status refresh:

```
schtasks /create /tn "RERA daily" /tr "python E:\Scraper-for-RERA\maharashtra\scrape_maharera.py --status-only" /sc daily /st 06:00
```

Other states, daily at 06:30 (each run is a few minutes):

```
schtasks /create /tn "RERA TN" /tr "cmd /c cd /d E:\Scraper-for-RERA\tamilnadu && python scrape_tn.py" /sc daily /st 06:30
schtasks /create /tn "RERA KA" /tr "cmd /c cd /d E:\Scraper-for-RERA\karnataka && python scrape_karnataka.py" /sc daily /st 06:40
```

**Linux/cron** — daily status, weekly full:

```
0 6 * * *  cd /opt/rera/maharashtra && python scrape_maharera.py --status-only
0 3 * * 0  cd /opt/rera/maharashtra && python scrape_maharera.py
```

Keep `--workers` at 4 or below on a shared/server IP. All requests come from one
address, and these portals do block.
