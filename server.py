#!/usr/bin/env python3
"""
Tiny web service so the scrapers can run on Render's free web tier.

Render free web services sleep after 15 minutes without HTTP traffic and have
512 MB of memory, so:

  * an external cron (e.g. cron-job.org) calls GET /run once a day, which
    starts the scrape in the background and returns immediately, and pings
    GET /health every few minutes while the run is in progress so Render
    does not put the service to sleep mid-run;
  * each state runs as a separate process, one after another, with
    RERA_DB_ONLY=1 (database only, one source at a time) to stay < 512 MB.

Endpoints
  GET /health   public   "ok" + whether a run is in progress (keep-alive target)
  GET /run      token    start a run (all states, or ?states=tamilnadu,karnataka)
  GET /status   token    current / last run: per-state result and last log lines
  GET /probe    token    can this server reach the three RERA portals?

The token is the RUN_TOKEN environment variable, sent as the header
`X-Run-Token: <token>` (preferred) or as `?token=<token>`.

Run locally:  RUN_TOKEN=test python server.py   ->  http://localhost:10000/health
"""

import hmac
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

ROOT = Path(__file__).resolve().parent

# state -> (folder, script, extra args). Maharashtra runs its daily batch
# (newest pages + one seventh of the listing, rotating by weekday).
STATES = {
    "tamilnadu": ("tamilnadu", "scrape_tn.py", []),
    "karnataka": ("karnataka", "scrape_karnataka.py", []),
    "maharashtra": ("maharashtra", "scrape_maharera.py", ["--batch"]),
    "uttarakhand": ("uttarakhand", "scrape_uttarakhand.py", []),
}
PORTALS = {
    "tamilnadu": "https://rera.tn.gov.in/",
    "karnataka": "https://rera.karnataka.gov.in/",
    "maharashtra": "https://www.maharera.maharashtra.gov.in/",
    "uttarakhand": "https://uttarakhandrera.uk.gov.in/",
}
# Exit codes from the scrapers: 0 ok, 4 finished but some sources failed.
OK_CODES = {0, 4}

_lock = threading.Lock()
_run = {"running": False, "started": None, "finished": None, "states": {}}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


# ─── Running the scrapers ───────────────────────────────────────────────────

def run_states(states):
    env = dict(os.environ, RERA_DB_ONLY="1", PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    try:
        for state in states:
            folder, script, args = STATES[state]
            entry = {"started": now(), "finished": None, "exit_code": None,
                     "ok": None, "log_tail": deque(maxlen=40)}
            _run["states"][state] = entry
            log(f"{state}: starting")
            proc = subprocess.Popen(
                [sys.executable, script, *args], cwd=ROOT / folder, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace")
            for line in proc.stdout:
                line = line.rstrip()
                print(f"[{state}] {line}", flush=True)  # -> Render logs
                entry["log_tail"].append(line)
            code = proc.wait()
            entry.update(finished=now(), exit_code=code, ok=code in OK_CODES)
            log(f"{state}: finished with exit code {code}")
    finally:
        with _lock:
            _run["running"] = False
            _run["finished"] = now()


def start_run(states):
    with _lock:
        if _run["running"]:
            return False
        _run.update(running=True, started=now(), finished=None, states={})
    threading.Thread(target=run_states, args=(states,), daemon=True).start()
    return True


def probe():
    out = {}
    for state, url in PORTALS.items():
        started = time.time()
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            out[state] = {"reachable": True, "http_status": r.status_code,
                          "seconds": round(time.time() - started, 1)}
        except requests.RequestException as exc:
            out[state] = {"reachable": False, "error": type(exc).__name__,
                          "seconds": round(time.time() - started, 1)}
    return out


def status_payload():
    with _lock:
        states = {s: {**e, "log_tail": list(e["log_tail"])} for s, e in _run["states"].items()}
        return {"running": _run["running"], "started": _run["started"],
                "finished": _run["finished"], "states": states}


# ─── HTTP ───────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "rera-scraper"

    def _send(self, code, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, query):
        expected = os.environ.get("RUN_TOKEN", "")
        given = self.headers.get("X-Run-Token") or (query.get("token") or [""])[0]
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"

        if path in ("/", "/health"):
            return self._send(200, {"status": "ok", "running": _run["running"]})

        if path not in ("/run", "/status", "/probe"):
            return self._send(404, {"error": "not found"})
        if not os.environ.get("RUN_TOKEN"):
            return self._send(503, {"error": "RUN_TOKEN is not configured on the server"})
        if not self._authorized(query):
            return self._send(401, {"error": "missing or wrong token"})

        if path == "/status":
            return self._send(200, status_payload())
        if path == "/probe":
            return self._send(200, probe())

        # /run
        wanted = (query.get("states") or [",".join(STATES)])[0].split(",")
        wanted = [s.strip() for s in wanted if s.strip()]
        unknown = [s for s in wanted if s not in STATES]
        if unknown:
            return self._send(400, {"error": f"unknown state(s): {unknown}",
                                    "valid": list(STATES)})
        if not os.environ.get("DATABASE_URL"):
            return self._send(503, {"error": "DATABASE_URL is not configured on the server"})
        if not start_run(wanted):
            return self._send(409, {"started": False, "reason": "a run is already in progress",
                                    **status_payload()})
        log(f"run started for {wanted}")
        return self._send(202, {"started": True, "states": wanted,
                                "note": "keep pinging /health every <15 min until it finishes"})

    def log_message(self, fmt, *args):
        # Keep Render logs readable, and never log the query string (token).
        log(f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}")


def main():
    port = int(os.environ.get("PORT", "10000"))
    log(f"listening on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
