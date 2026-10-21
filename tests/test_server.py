"""Tests for server.py (the Render web service). No scraping, no network
beyond localhost: /run's background start is replaced by a stub."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("X-Run-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def setUp(self):
        env = mock.patch.dict("os.environ", {"RUN_TOKEN": "secret", "DATABASE_URL": "postgresql://x"})
        env.start()
        self.addCleanup(env.stop)
        server.log = lambda msg: None  # keep test output quiet

    def test_health_is_public(self):
        self.assertEqual(self.get("/health"), (200, {"status": "ok", "running": False}))

    def test_token_required(self):
        for path in ("/run", "/status", "/probe"):
            self.assertEqual(self.get(path)[0], 401, path)
            self.assertEqual(self.get(path, token="wrong")[0], 401, path)
        self.assertEqual(self.get("/status", token="secret")[0], 200)

    def test_token_also_accepted_in_query(self):
        self.assertEqual(self.get("/status?token=secret")[0], 200)

    def test_no_token_configured_refuses(self):
        with mock.patch.dict("os.environ", {"RUN_TOKEN": ""}):
            self.assertEqual(self.get("/run", token="")[0], 503)

    def test_run_validates_states_and_starts(self):
        code, body = self.get("/run?states=goa", token="secret")
        self.assertEqual(code, 400)
        with mock.patch.object(server, "start_run", return_value=True) as start:
            code, body = self.get("/run?states=tamilnadu,karnataka", token="secret")
        self.assertEqual((code, body["started"]), (202, True))
        start.assert_called_once_with(["tamilnadu", "karnataka"])
        with mock.patch.object(server, "start_run", return_value=True) as start:
            self.get("/run", token="secret")
        start.assert_called_once_with(["tamilnadu", "karnataka", "maharashtra", "uttarakhand"])

    def test_run_refused_while_running(self):
        with mock.patch.object(server, "start_run", return_value=False):
            self.assertEqual(self.get("/run", token="secret")[0], 409)

    def test_run_needs_database(self):
        with mock.patch.dict("os.environ", {"DATABASE_URL": ""}):
            self.assertEqual(self.get("/run", token="secret")[0], 503)

    def test_unknown_path(self):
        self.assertEqual(self.get("/admin")[0], 404)


if __name__ == "__main__":
    unittest.main()
