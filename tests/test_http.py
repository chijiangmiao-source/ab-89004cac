"""API/HTTP 冒烟测试：真实起服，打 /health、页面、冻结/发布/冲突/查询。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import build_server

OLD = {"name": "isol", "version": "v1", "start": "LOCKED", "states": {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}}
NEW = {"name": "isol", "version": "v2", "start": "LOCKED", "states": {
    k: {"output": v["output"], "actions": dict(v["actions"])}
    for k, v in OLD["states"].items()
}}
NEW_BROKEN = {"name": "isol", "version": "v2", "start": "LOCKED", "states": {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}}


class HttpSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(cls.tmp.name, "t.db")
        cls.httpd: ThreadingHTTPServer = build_server("127.0.0.1", 0, db)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.service.store.close()
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path, method="GET", body=None, expect=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url(path), data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode())
                status = resp.status
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            status = exc.code
        if expect is not None:
            self.assertEqual(status, expect, f"{path} 期望 {expect}，实际 {status}：{payload}")
        return status, payload

    def test_01_health_and_page(self):
        status, body = self.request("/health", expect=200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("frozen", body)
        with urllib.request.urlopen(self.url("/"), timeout=5) as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("迁移裁决台", html)

    def test_02_freeze_migratable_and_publish(self):
        body = {"migration_id": "HTTP-OK", "old_spec": OLD, "new_spec": NEW,
                "sessions": [
                    {"session_id": "S1", "spec_version": "v1",
                     "state": "LOCKED", "observed_output": "红灯"},
                    {"session_id": "S2", "spec_version": "v1",
                     "state": "OPEN", "observed_output": "绿灯"},
                ]}
        status, rec = self.request("/api/decisions/freeze", "POST", body, 200)
        self.assertEqual(rec["stage"], "FROZEN")
        self.assertTrue(rec["verdict"]["publishable"])
        self.assertEqual(
            {(s["session_id"], s["target_state"], s["ok"])
             for s in rec["verdict"]["sessions"]},
            {("S1", "LOCKED", True), ("S2", "OPEN", True)})
        h = rec["snapshot_hash"]
        status, pub = self.request("/api/decisions/HTTP-OK/publish", "POST",
                                   {"snapshot_hash": h}, 200)
        self.assertEqual(pub["stage"], "PUBLISHED")
        # 查询持久化结论
        status, got = self.request("/api/decisions/HTTP-OK", expect=200)
        self.assertEqual(got["stage"], "PUBLISHED")
        self.assertEqual(got["snapshot_hash"], h)
        status, listing = self.request("/api/decisions", expect=200)
        self.assertTrue(any(d["migration_id"] == "HTTP-OK" for d in listing["decisions"]))

    def test_03_freeze_action_missing_blocks_publish(self):
        body = {"migration_id": "HTTP-MISS", "old_spec": OLD,
                "new_spec": NEW_BROKEN,
                "sessions": [{"session_id": "S7", "spec_version": "v1",
                              "state": "OPEN", "observed_output": "绿灯"}]}
        status, rec = self.request("/api/decisions/freeze", "POST", body, 200)
        self.assertFalse(rec["verdict"]["publishable"])
        sv = rec["verdict"]["sessions"][0]
        self.assertFalse(sv["ok"])
        self.assertEqual(sv["blocking_path"][-1]["reason"], "action_missing_new")
        status, err = self.request(
            "/api/decisions/HTTP-MISS/publish", "POST",
            {"snapshot_hash": rec["snapshot_hash"]}, expect=409)
        self.assertEqual(err["error"], "not_publishable")

    def test_04_concurrent_stale_snapshot_conflict(self):
        """同一标识重传不同会话快照 -> 409，且第一份快照原样保留。"""
        first = {"migration_id": "HTTP-RACE", "old_spec": OLD, "new_spec": NEW,
                 "sessions": [{"session_id": "S1", "spec_version": "v1",
                               "state": "LOCKED", "observed_output": "红灯"}]}
        stale = {"migration_id": "HTTP-RACE", "old_spec": OLD, "new_spec": NEW,
                 "sessions": [{"session_id": "S99", "spec_version": "v1",
                               "state": "OPEN", "observed_output": "绿灯"}]}
        s1, r1 = self.request("/api/decisions/freeze", "POST", first, 200)
        s2, r2 = self.request("/api/decisions/freeze", "POST", stale, 409)
        self.assertEqual(r2["error"], "snapshot_mismatch")
        _, kept = self.request("/api/decisions/HTTP-RACE", expect=200)
        self.assertEqual([s["session_id"] for s in kept["sessions"]], ["S1"])

    def test_05_stale_snapshot_rejected(self):
        body = {"migration_id": "HTTP-STALE", "old_spec": OLD, "new_spec": NEW,
                "sessions": [{"session_id": "S2", "spec_version": "v1",
                              "state": "OPEN", "observed_output": "红灯"}]}
        status, err = self.request("/api/decisions/freeze", "POST", body, 400)
        self.assertIn("快照陈旧", err["message"])

    def test_06_bad_inputs(self):
        self.request("/api/decisions/freeze", "POST", {}, 400)
        self.request("/api/decisions/NOPE", "GET", expect=404)
        self.request("/api/decisions/NOPE/publish", "POST",
                     {"snapshot_hash": "x"}, expect=409)


if __name__ == "__main__":
    unittest.main()
