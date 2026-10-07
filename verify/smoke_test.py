"""End-to-end HTTP smoke test against a real server process.

Covers:
  * GET /healthz and the HTML page (configurable host port),
  * freeze -> per-session target states / blocking paths,
  * retransmitted identical snapshot returns the same verdict,
  * stale snapshot on the same migration id is rejected with HTTP 409,
  * a blocked verdict cannot be published (409),
  * simulated interruption: kill the process after freeze, reopen the same
    database, and observe the single FROZEN or PUBLISHED conclusion.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OLD = {
    "name": "iso-v3",
    "states": {
        "idle": {"output": "READY", "transitions": {"arm": "armed"}},
        "armed": {
            "output": "ARMED",
            "transitions": {"fire": "done", "disarm": "idle"},
        },
        "done": {"output": "DONE", "transitions": {"reset": "idle"}},
    },
}
NEW_SAFE = {
    "name": "iso-v4",
    "states": {
        "idle": {"output": "READY", "transitions": {"arm": "armed"}},
        "armed": {
            "output": "ARMED",
            "transitions": {"fire": "done", "disarm": "idle"},
        },
        "done": {"output": "DONE", "transitions": {"reset": "idle"}},
    },
}
NEW_MISSING = json.loads(json.dumps(NEW_SAFE))
NEW_MISSING["states"]["armed"]["transitions"].pop("disarm")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    def __init__(self, db_path: str, port: int):
        self.db_path = db_path
        self.port = port
        self.proc = None

    def start(self) -> "ServerProcess":
        env = dict(os.environ)
        env.update(
            {
                "HOST": "127.0.0.1",
                "PORT": str(self.port),
                "DB_PATH": self.db_path,
                "PYTHONPATH": ROOT,
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.server"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"server exited early:\n{out}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.5):
                    return self
            except OSError:
                time.sleep(0.15)
        raise RuntimeError("server did not open its port in time")

    def stop(self, sig=signal.SIGKILL) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc is not None and self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except OSError:
                pass


def request(method: str, port: int, path: str, body=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = resp.read().decode()
            return resp.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        parsed = json.loads(payload) if payload else {}
        if expect is not None and exc.code != expect:
            raise AssertionError(
                f"expected {expect}, got {exc.code}: {payload}"
            )
        return exc.code, parsed


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "verdicts.db")
        cls.port = free_port()
        cls.server = ServerProcess(cls.db, cls.port).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_01_health_and_page(self):
        status, body = request("GET", self.port, "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["phase_model"], ["FROZEN", "PUBLISHED"])

        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/", timeout=5
        ) as resp:
            page = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("迁移裁决", page)

    def test_02_freeze_migratable_shows_targets(self):
        body = {
            "migration_id": "smoke-ok",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [
                {"session_id": "s1", "current_state": "idle"},
                {"session_id": "s2", "current_state": "armed"},
            ],
        }
        status, v = request("POST", self.port, "/api/verdicts/freeze", body)
        self.assertEqual(status, 201)
        self.assertEqual(v["phase"], "FROZEN")
        self.assertTrue(v["publishable"])
        for r in v["results"]:
            self.assertTrue(r["safe"])
            self.assertEqual(r["target_state"], r["current_state"])
        self.assertEqual(len(v["results"][0]["mapping"]), 3)

    def test_03_publish_then_retransmit(self):
        status, v = request(
            "POST", self.port, "/api/verdicts/smoke-ok/publish"
        )
        self.assertEqual(status, 200)
        self.assertEqual(v["phase"], "PUBLISHED")
        # Idempotent republish.
        status, v2 = request(
            "POST", self.port, "/api/verdicts/smoke-ok/publish"
        )
        self.assertEqual(status, 200)
        self.assertEqual(v2["published_at"], v["published_at"])

    def test_04_retransmit_same_snapshot_returns_same_verdict(self):
        body = {
            "migration_id": "smoke-ok",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [
                {"session_id": "s1", "current_state": "idle"},
                {"session_id": "s2", "current_state": "armed"},
            ],
        }
        status, v = request("POST", self.port, "/api/verdicts/freeze", body)
        self.assertEqual(status, 200)
        self.assertTrue(v["retransmitted"])

    def test_05_stale_snapshot_conflicts(self):
        body = {
            "migration_id": "smoke-ok",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [
                {"session_id": "s1", "current_state": "done"}  # advanced!
            ],
        }
        status, err = request(
            "POST", self.port, "/api/verdicts/freeze", body, expect=409
        )
        self.assertIn("session snapshot", err["error"])

    def test_06_missing_action_case_blocks_and_cannot_publish(self):
        body = {
            "migration_id": "smoke-missing",
            "old": OLD,
            "new": NEW_MISSING,
            "sessions": [
                {"session_id": "s1", "current_state": "idle"},
                {"session_id": "s2", "current_state": "armed"},
            ],
        }
        status, v = request("POST", self.port, "/api/verdicts/freeze", body)
        self.assertEqual(status, 201)
        self.assertFalse(v["publishable"])
        blocked = [r for r in v["results"] if r["session_id"] == "s2"][0]
        self.assertFalse(blocked["safe"])
        self.assertIn("disarm", blocked["blocking_reason"])
        status, err = request(
            "POST", self.port, "/api/verdicts/smoke-missing/publish",
            expect=409,
        )
        self.assertIn("not publishable", err["error"])

    def test_07_validation_rejects_13_sessions(self):
        body = {
            "migration_id": "smoke-too-many",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [
                {"session_id": f"s{i}", "current_state": "idle"}
                for i in range(13)
            ],
        }
        status, err = request(
            "POST", self.port, "/api/verdicts/freeze", body, expect=400
        )
        self.assertIn("12", err["error"])


class CrashRecoverySmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "verdicts.db")
        cls.port = free_port()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_interrupt_after_freeze_recovers_unpublished(self):
        server = ServerProcess(self.db, self.port).start()
        body = {
            "migration_id": "crash-1",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [{"session_id": "s1", "current_state": "idle"}],
        }
        status, _ = request("POST", self.port, "/api/verdicts/freeze", body)
        self.assertEqual(status, 201)
        # Simulate interruption (hard kill, no graceful shutdown).
        server.stop()

        server2 = ServerProcess(self.db, self.port).start()
        try:
            status, v = request("GET", self.port, "/api/verdicts/crash-1")
            self.assertEqual(status, 200)
            self.assertEqual(v["phase"], "FROZEN")
            self.assertEqual(len(v["results"]), 1)
            # Old snapshot still validates after restart.
            status, again = request(
                "POST", self.port, "/api/verdicts/freeze", body
            )
            self.assertEqual(status, 200)
            self.assertTrue(again["retransmitted"])
            # Finish the switch after reopening.
            status, published = request(
                "POST", self.port, "/api/verdicts/crash-1/publish"
            )
            self.assertEqual(status, 200)
            self.assertEqual(published["phase"], "PUBLISHED")
        finally:
            server2.stop()

    def test_interrupt_during_switch_leaves_one_published(self):
        server = ServerProcess(self.db, self.port).start()
        body = {
            "migration_id": "crash-2",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [{"session_id": "s1", "current_state": "idle"}],
        }
        request("POST", self.port, "/api/verdicts/freeze", body)
        request("POST", self.port, "/api/verdicts/crash-2/publish")
        server.stop()  # interrupt after the switch committed

        server2 = ServerProcess(self.db, self.port).start()
        try:
            status, v = request("GET", self.port, "/api/verdicts/crash-2")
            self.assertEqual(status, 200)
            self.assertEqual(v["phase"], "PUBLISHED")
            status, listing = request("GET", self.port, "/api/verdicts")
            ids = [x["migration_id"] for x in listing["verdicts"]]
            self.assertEqual(ids.count("crash-2"), 1)
        finally:
            server2.stop()


@unittest.skipUnless(
    os.environ.get("APP_BASE_URL"), "APP_BASE_URL not set; skipping live app probe"
)
class LiveAppProbeTests(unittest.TestCase):
    """Exercise the app container running inside Compose over the network."""

    @property
    def base(self) -> str:
        return os.environ["APP_BASE_URL"].rstrip("/")

    def request(self, method, path, body=None, expect=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = resp.read().decode()
                return resp.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()
            if expect is not None and exc.code != expect:
                raise AssertionError(
                    f"expected {expect}, got {exc.code}: {payload}"
                )
            return exc.code, json.loads(payload) if payload else {}

    def test_live_migratable_missing_and_stale_snapshot(self):
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

        ok_body = {
            "migration_id": "live-ok",
            "old": OLD,
            "new": NEW_SAFE,
            "sessions": [{"session_id": "s1", "current_state": "idle"}],
        }
        status, v = self.request("POST", "/api/verdicts/freeze", ok_body)
        self.assertIn(status, (200, 201))
        self.assertTrue(v["results"][0]["safe"])

        missing_body = {
            "migration_id": "live-missing",
            "old": OLD,
            "new": NEW_MISSING,
            "sessions": [{"session_id": "s1", "current_state": "armed"}],
        }
        status, v = self.request(
            "POST", "/api/verdicts/freeze", missing_body
        )
        self.assertIn(status, (200, 201))
        self.assertFalse(v["publishable"])
        self.request("POST", "/api/verdicts/live-missing/publish", expect=409)

        stale = dict(ok_body)
        stale["sessions"] = [{"session_id": "s1", "current_state": "done"}]
        status, err = self.request(
            "POST", "/api/verdicts/freeze", stale, expect=409
        )
        self.assertIn("session snapshot", err["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
