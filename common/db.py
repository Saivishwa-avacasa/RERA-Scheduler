"""
CockroachDB / PostgreSQL sync for the state scrapers.

Enabled when DATABASE_URL is set (environment variable, or a .env file at the
repo root). Only standard PostgreSQL SQL is used - INSERT ... ON CONFLICT,
unnest(), JSONB, gen_random_uuid() - so the same code runs on CockroachDB
Cloud and on PostgreSQL 13+.

The database, not the scraper, decides what is new or changed: each batch of
scraped records is compared against the rows already stored, by content hash,
then written with one statement per batch. That matters in Docker, where the
container's local snapshot files disappear after every run.

Tables (created if missing): rera_projects, developers, project_developers,
project_change_history, scrape_runs, scrape_errors.
"""

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from common.rera_common import TRACKED_FIELDS, content_hash, log, record_key

ROOT = Path(__file__).resolve().parent.parent
BATCH = 500

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS rera_projects (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        state_code TEXT NOT NULL,
        rera_number TEXT NOT NULL,
        application_no TEXT,
        project_name TEXT,
        developer_name TEXT,
        project_type TEXT,
        district TEXT,
        city TEXT,
        pincode TEXT,
        registration_date DATE,
        proposed_completion_date DATE,
        project_status TEXT,
        status_remarks TEXT,
        detail_url TEXT,
        source_url TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        extra JSONB,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT rera_projects_state_number_key UNIQUE (state_code, rera_number)
    )""",
    # Additive migrations for a rera_projects table created earlier from the
    # spec's minimal column list. Never drops or rewrites existing data.
    "ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS application_no TEXT",
    "ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS pincode TEXT",
    "ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS status_remarks TEXT",
    "ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS detail_url TEXT",
    "ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS extra JSONB",
    """CREATE TABLE IF NOT EXISTS developers (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        normalized_name TEXT NOT NULL UNIQUE,
        original_name TEXT,
        address TEXT,
        email TEXT,
        phone TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS project_developers (
        project_id UUID NOT NULL REFERENCES rera_projects (id) ON DELETE CASCADE,
        developer_id UUID NOT NULL REFERENCES developers (id) ON DELETE CASCADE,
        PRIMARY KEY (project_id, developer_id)
    )""",
    """CREATE TABLE IF NOT EXISTS project_change_history (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id UUID NOT NULL REFERENCES rera_projects (id) ON DELETE CASCADE,
        changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        field_name TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        source_url TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS project_change_history_project_idx
        ON project_change_history (project_id, changed_at)""",
    """CREATE TABLE IF NOT EXISTS scrape_runs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        state_code TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        status TEXT NOT NULL,
        records_discovered INT NOT NULL DEFAULT 0,
        records_inserted INT NOT NULL DEFAULT 0,
        records_updated INT NOT NULL DEFAULT 0,
        records_unchanged INT NOT NULL DEFAULT 0,
        records_failed INT NOT NULL DEFAULT 0,
        error_message TEXT,
        summary JSONB
    )""",
    """CREATE TABLE IF NOT EXISTS scrape_errors (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        scrape_run_id UUID REFERENCES scrape_runs (id) ON DELETE CASCADE,
        state_code TEXT NOT NULL,
        source_url TEXT,
        error_type TEXT,
        error_message TEXT,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        retry_count INT NOT NULL DEFAULT 0
    )""",
]

# Scraper field -> rera_projects column, for the columns change detection compares.
COLUMN_FOR = {
    "project_name": "project_name", "promoter_name": "developer_name",
    "project_type": "project_type", "district": "district", "taluka": "city",
    "pincode": "pincode", "registration_date": "registration_date",
    "proposed_completion_date": "proposed_completion_date",
    "project_status": "project_status", "status_remarks": "status_remarks",
}
assert set(COLUMN_FOR) == set(TRACKED_FIELDS)

# Everything else a state emits goes into `extra` (JSONB), minus bookkeeping.
CORE_FIELDS = set(COLUMN_FOR) | {
    "state_code", "rera_number", "application_no", "detail_url", "source_url",
    "promoter_normalized", "content_hash", "first_seen_at", "last_changed_at",
    "scraped_at",
}


# ─── Configuration ──────────────────────────────────────────────────────────

def _read_dotenv(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def database_url():
    """DATABASE_URL from the environment, else from <repo>/.env."""
    url = os.environ.get("DATABASE_URL") or _read_dotenv(ROOT / ".env").get("DATABASE_URL")
    # `docker run --env-file` passes DATABASE_URL="..." with the quotes included.
    return (url or "").strip().strip('"').strip("'").strip() or None


def redact(text):
    """Remove credentials from anything that might contain a connection
    string - libpq error messages echo the whole URL, password included."""
    text = re.sub(r"(?i)\b(postgres(?:ql)?(?:\+\w+)?://)[^@\s\"']*@", r"\1***:***@", str(text))
    return re.sub(r"(?i)(password\s*=\s*)\S+", r"\1***", text)


def _ssl_root_cert():
    """CockroachDB Cloud certificates are signed by Cockroach's own CA, so
    sslmode=verify-full needs its root.crt. libpq finds it automatically on a
    developer's machine (%APPDATA%/postgresql or ~/.postgresql) but not in a
    container, so fall back to the copy shipped in certs/."""
    if os.environ.get("PGSSLROOTCERT"):
        return None
    defaults = [Path.home() / ".postgresql" / "root.crt"]
    if os.environ.get("APPDATA"):
        defaults.append(Path(os.environ["APPDATA"]) / "postgresql" / "root.crt")
    if any(p.exists() for p in defaults):
        return None  # libpq picks these up itself
    bundled = ROOT / "certs" / "root.crt"
    return str(bundled) if bundled.exists() else None


def connect(url=None):
    import psycopg  # imported lazily so file-only runs need no DB driver

    url = url or database_url()
    kwargs = {"connect_timeout": 20, "application_name": "rera-scraper"}
    root_cert = _ssl_root_cert()
    if root_cert and "sslrootcert=" not in url:
        kwargs["sslrootcert"] = root_cert
    # Autocommit, so each `with conn.transaction()` below is its own real
    # transaction: one batch failing does not roll back the batches before it.
    return psycopg.connect(url, autocommit=True, **kwargs)


def ensure_schema(conn):
    # One statement at a time: CockroachDB dislikes several schema changes in
    # one transaction, and IF NOT EXISTS makes each one idempotent anyway.
    for statement in SCHEMA:
        conn.execute(statement)


# ─── Sync ───────────────────────────────────────────────────────────────────

def _text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)


def _chunks(items, size=BATCH):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _existing(conn, state_code, keys):
    columns = ", ".join(COLUMN_FOR.values())
    rows = conn.execute(
        f"SELECT rera_number, content_hash, {columns} FROM rera_projects "
        f"WHERE state_code = %s AND rera_number = ANY(%s)",
        (state_code, keys)).fetchall()
    out = {}
    for row in rows:
        stored = dict(zip(COLUMN_FOR, (_text(v) for v in row[2:])))
        out[row[0]] = (row[1], stored)
    return out


_UPSERT = """
INSERT INTO rera_projects (
    state_code, rera_number, application_no, project_name, developer_name,
    project_type, district, city, pincode, registration_date,
    proposed_completion_date, project_status, status_remarks, detail_url,
    source_url, content_hash, extra, first_seen_at, last_scraped_at, last_changed_at)
SELECT %(state)s, u.k, NULLIF(u.app, ''), u.name, u.dev, u.typ, u.dist, u.city,
       NULLIF(u.pin, ''), NULLIF(u.reg, '')::DATE, NULLIF(u.comp, '')::DATE,
       u.status, u.remarks, NULLIF(u.detail, ''), u.src, u.hash, u.extra::JSONB,
       %(now)s, %(now)s, %(now)s
FROM unnest(%(k)s::TEXT[], %(app)s::TEXT[], %(name)s::TEXT[], %(dev)s::TEXT[],
            %(typ)s::TEXT[], %(dist)s::TEXT[], %(city)s::TEXT[], %(pin)s::TEXT[],
            %(reg)s::TEXT[], %(comp)s::TEXT[], %(status)s::TEXT[], %(remarks)s::TEXT[],
            %(detail)s::TEXT[], %(src)s::TEXT[], %(hash)s::TEXT[], %(extra)s::TEXT[])
     AS u(k, app, name, dev, typ, dist, city, pin, reg, comp, status, remarks,
          detail, src, hash, extra)
ON CONFLICT (state_code, rera_number) DO UPDATE SET
    application_no = excluded.application_no,
    project_name = excluded.project_name,
    developer_name = excluded.developer_name,
    project_type = excluded.project_type,
    district = excluded.district,
    city = excluded.city,
    pincode = excluded.pincode,
    registration_date = excluded.registration_date,
    proposed_completion_date = excluded.proposed_completion_date,
    project_status = excluded.project_status,
    status_remarks = excluded.status_remarks,
    detail_url = excluded.detail_url,
    source_url = excluded.source_url,
    content_hash = excluded.content_hash,
    extra = excluded.extra,
    last_scraped_at = excluded.last_scraped_at,
    last_changed_at = excluded.last_changed_at
"""

_HISTORY = """
INSERT INTO project_change_history (project_id, changed_at, field_name, old_value, new_value, source_url)
SELECT p.id, %(now)s, u.f, u.o, u.n, u.s
FROM unnest(%(k)s::TEXT[], %(f)s::TEXT[], %(o)s::TEXT[], %(n)s::TEXT[], %(s)s::TEXT[])
     AS u(k, f, o, n, s)
JOIN rera_projects p ON p.state_code = %(state)s AND p.rera_number = u.k
"""

_DEVELOPERS = """
INSERT INTO developers (normalized_name, original_name)
SELECT u.n, u.o FROM unnest(%(n)s::TEXT[], %(o)s::TEXT[]) AS u(n, o)
ON CONFLICT (normalized_name) DO NOTHING
"""

_LINKS = """
INSERT INTO project_developers (project_id, developer_id)
SELECT p.id, d.id
FROM unnest(%(k)s::TEXT[], %(n)s::TEXT[]) AS u(k, n)
JOIN rera_projects p ON p.state_code = %(state)s AND p.rera_number = u.k
JOIN developers d ON d.normalized_name = u.n
ON CONFLICT (project_id, developer_id) DO NOTHING
"""


def _upsert(conn, state_code, records, now):
    columns = {name: [] for name in ("k", "app", "name", "dev", "typ", "dist", "city", "pin",
                                     "reg", "comp", "status", "remarks", "detail", "src",
                                     "hash", "extra")}
    for record in records:
        extra = {k: v for k, v in record.items() if k not in CORE_FIELDS and v not in ("", None)}
        for column, value in (
            ("k", record_key(record)), ("app", record.get("application_no")),
            ("name", record.get("project_name")), ("dev", record.get("promoter_name")),
            ("typ", record.get("project_type")), ("dist", record.get("district")),
            ("city", record.get("taluka")), ("pin", record.get("pincode")),
            ("reg", record.get("registration_date")),
            ("comp", record.get("proposed_completion_date")),
            ("status", record.get("project_status")), ("remarks", record.get("status_remarks")),
            ("detail", record.get("detail_url")), ("src", record.get("source_url")),
            ("hash", content_hash(record)), ("extra", json.dumps(extra, ensure_ascii=False)),
        ):
            columns[column].append(_text(value))
    conn.execute(_UPSERT, {"state": state_code, "now": now, **columns})


def sync_projects(conn, state_code, records, run_time=None):
    """Insert new projects, update changed ones (recording each changed
    field), touch last_scraped_at on unchanged ones. Returns counts."""
    now = run_time or datetime.now(timezone.utc)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "history_rows": 0}
    items = [(record_key(r), r) for r in records.values() if record_key(r)]

    for batch in _chunks(items):
        keys = [k for k, _ in batch]
        with conn.transaction():
            existing = _existing(conn, state_code, keys)
            to_write, unchanged, rehash = [], [], []
            history = {"k": [], "f": [], "o": [], "n": [], "s": []}
            for key, record in batch:
                stored = existing.get(key)
                if stored is None:
                    counts["inserted"] += 1
                    to_write.append(record)
                    continue
                stored_hash, stored_fields = stored
                if stored_hash == content_hash(record):
                    unchanged.append(key)
                    continue
                deltas = [(f, stored_fields.get(f, ""), _text(record.get(f)))
                          for f in TRACKED_FIELDS
                          if stored_fields.get(f, "") != _text(record.get(f))]
                if not deltas:
                    # Only the hash recipe changed, not the values: refresh the
                    # stored hash without touching last_changed_at.
                    rehash.append((content_hash(record), key))
                    continue
                counts["updated"] += 1
                to_write.append(record)
                for field, old, new in deltas:
                    history["k"].append(key)
                    history["f"].append(field)
                    history["o"].append(old)
                    history["n"].append(new)
                    history["s"].append(record.get("source_url", ""))

            if to_write:
                _upsert(conn, state_code, to_write, now)
            for new_hash, key in rehash:
                conn.execute("UPDATE rera_projects SET content_hash = %s WHERE state_code = %s "
                             "AND rera_number = %s", (new_hash, state_code, key))
                unchanged.append(key)
            if unchanged:
                conn.execute(
                    "UPDATE rera_projects SET last_scraped_at = %s "
                    "WHERE state_code = %s AND rera_number = ANY(%s)",
                    (now, state_code, unchanged))
            if history["k"]:
                conn.execute(_HISTORY, {"state": state_code, "now": now, **history})
                counts["history_rows"] += len(history["k"])

            developers = {}
            links = {"k": [], "n": []}
            for record in to_write:
                normalized = record.get("promoter_normalized")
                if not normalized:
                    continue
                developers.setdefault(normalized, record.get("promoter_name", ""))
                links["k"].append(record_key(record))
                links["n"].append(normalized)
            if developers:
                conn.execute(_DEVELOPERS, {"n": list(developers), "o": list(developers.values())})
                conn.execute(_LINKS, {"state": state_code, **links})
        counts["unchanged"] += len(unchanged)
    return counts


def record_run(conn, state_code, started, summary, counts, failures):
    status = "completed" if not failures else "completed_with_errors"
    with conn.transaction():
        run_id = conn.execute(
            "INSERT INTO scrape_runs (state_code, started_at, completed_at, status, "
            "records_discovered, records_inserted, records_updated, records_unchanged, "
            "records_failed, error_message, summary) "
            "VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s::JSONB) RETURNING id",
            (state_code, started, status, summary.get("total_projects", 0),
             counts.get("inserted", 0), counts.get("updated", 0), counts.get("unchanged", 0),
             len(failures), "; ".join(f"{k}: {v}" for k, v in failures.items()) or None,
             json.dumps(summary, default=str))).fetchone()[0]
        for source, message in failures.items():
            conn.execute(
                "INSERT INTO scrape_errors (scrape_run_id, state_code, source_url, error_type, "
                "error_message) VALUES (%s, %s, %s, %s, %s)",
                (run_id, state_code, source, message.split(":", 1)[0], message))
    return run_id


def sync_run(state_code, records, *, started, summary, failures):
    """Entry point used by finish_run. Returns counts, or None when no
    DATABASE_URL is configured. Raises on database errors."""
    if not database_url():
        return None
    from urllib.parse import urlsplit
    parts = urlsplit(database_url())
    # Host and database name only - never the user or password.
    log(f"Syncing to database {parts.hostname}{parts.path} ...")
    with connect() as conn:
        ensure_schema(conn)
        counts = sync_projects(conn, state_code, records, datetime.now(timezone.utc)) if records else {}
        record_run(conn, state_code, started, summary, counts, failures)
    log(f"Database: {counts.get('inserted', 0):,} inserted, {counts.get('updated', 0):,} updated, "
        f"{counts.get('unchanged', 0):,} unchanged, {counts.get('history_rows', 0):,} field changes recorded.")
    return counts
