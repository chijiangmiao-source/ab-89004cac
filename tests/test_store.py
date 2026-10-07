"""持久化裁决测试：竞争冻结、重传防混用、发布校验与崩溃恢复。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from app.core.models import parse_spec
from app.core.service import DecisionService
from app.core.store import ConflictError, Store


OLD_PAYLOAD = {"name": "isol", "version": "v1", "start": "LOCKED", "states": {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}}
NEW_PAYLOAD = {"name": "isol", "version": "v2", "start": "LOCKED", "states": {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}}
NEW_BROKEN = dict(NEW_PAYLOAD, states={
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED"}},  # 缺 trip
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
})

SESSIONS_OK = [
    {"session_id": "S1", "spec_version": "v1", "state": "LOCKED",
     "observed_output": "红灯"},
    {"session_id": "S2", "spec_version": "v1", "state": "OPEN",
     "observed_output": "绿灯"},
]


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "decisions.db")
        self.store = Store(self.db)
        self.svc = DecisionService(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class FreezePublishTests(StoreTestBase):
    def test_freeze_binds_summary_sessions_evidence_stage_together(self):
        rec = self.svc.freeze("M-1", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.assertEqual(rec["stage"], "FROZEN")
        self.assertTrue(rec["snapshot_hash"])
        self.assertEqual(rec["old_summary"]["version"], "v1")
        self.assertEqual(rec["new_summary"]["version"], "v2")
        self.assertEqual({s["session_id"] for s in rec["sessions"]}, {"S1", "S2"})
        self.assertIn("evidence", rec["verdict"])
        self.assertTrue(rec["verdict"]["publishable"])

    def test_blocking_candidate_cannot_publish(self):
        rec = self.svc.freeze("M-2", OLD_PAYLOAD, NEW_BROKEN,
                              [{"session_id": "S7", "spec_version": "v1",
                                "state": "OPEN", "observed_output": "绿灯"}])
        self.assertFalse(rec["verdict"]["publishable"])
        with self.assertRaises(ConflictError) as ctx:
            self.svc.publish("M-2", rec["snapshot_hash"])
        self.assertEqual(ctx.exception.code, "not_publishable")

    def test_publish_is_idempotent_and_terminal(self):
        rec = self.svc.freeze("M-3", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        pub = self.svc.publish("M-3", rec["snapshot_hash"])
        self.assertEqual(pub["stage"], "PUBLISHED")
        self.assertTrue(pub["published_at"])
        # 幂等重传
        pub2 = self.svc.publish("M-3", rec["snapshot_hash"])
        self.assertEqual(pub2["snapshot_hash"], pub["snapshot_hash"])
        # 同一行仍然只有唯一已发布结论
        self.assertEqual(len(self.store.list()), 1)

    def test_publish_with_wrong_snapshot_hash_rejected(self):
        rec = self.svc.freeze("M-4", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.publish("M-4", "deadbeef" * 8)
        self.assertEqual(ctx.exception.code, "snapshot_mismatch")
        # 拒绝后裁决仍停留在 FROZEN
        self.assertEqual(self.store.get("M-4")["stage"], "FROZEN")


class RetransmitAndCompetitionTests(StoreTestBase):
    def test_same_id_same_payload_is_idempotent(self):
        a = self.svc.freeze("M-5", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        b = self.svc.freeze("M-5", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.assertEqual(a["snapshot_hash"], b["snapshot_hash"])
        self.assertEqual(len(self.store.list()), 1)

    def test_same_id_different_sessions_conflicts(self):
        """重传同一标识但换了会话快照：必须 409，不得覆盖混用。"""
        self.svc.freeze("M-6", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        other_sessions = [{"session_id": "S99", "spec_version": "v1",
                           "state": "TRIPPED", "observed_output": "黄灯"}]
        with self.assertRaises(ConflictError) as ctx:
            self.svc.freeze("M-6", OLD_PAYLOAD, NEW_PAYLOAD, other_sessions)
        self.assertEqual(ctx.exception.code, "snapshot_mismatch")
        # 原封不动：仍是第一份快照
        rec = self.store.get("M-6")
        self.assertEqual([s["session_id"] for s in rec["sessions"]],
                         ["S1", "S2"])

    def test_two_pages_competing_same_id_only_one_wins(self):
        """两个页面并发竞争同一迁移标识，且携带不同快照。"""
        results: list = []
        errors: list = []

        def worker(sessions, tag):
            try:
                rec = self.svc.freeze("M-RACE", OLD_PAYLOAD, NEW_PAYLOAD, sessions)
                results.append((tag, rec["snapshot_hash"]))
            except ConflictError as exc:
                errors.append((tag, exc.code))

        t1 = threading.Thread(target=worker, args=(SESSIONS_OK, "A"))
        t2 = threading.Thread(target=worker, args=(
            [{"session_id": "SX", "spec_version": "v1", "state": "OPEN",
              "observed_output": "绿灯"}], "B"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(results), 1, "恰有一个冻结成功")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "snapshot_mismatch")
        rows = self.store.list()
        self.assertEqual(len(rows), 1, "标识下只能存在唯一裁决行")

    def test_different_ids_do_not_conflict(self):
        self.svc.freeze("M-7", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.svc.freeze("M-8", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.assertEqual(len(self.store.list()), 2)


class CrashRecoveryTests(StoreTestBase):
    def _reopen(self) -> Store:
        self.store.close()
        store = Store(self.db)
        self.addCleanup(store.close)
        return store

    def test_interrupted_after_freeze_recovers_to_unpublished(self):
        rec = self.svc.freeze("M-C1", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.assertEqual(rec["stage"], "FROZEN")
        # 模拟冻结后、切换前进程被杀死：直接关闭，不发布
        store2 = self._reopen()
        rolled = store2.recover_interrupted()
        self.assertIn("M-C1", rolled)
        self.assertIsNone(store2.get("M-C1"), "恢复后应为未发布（记录回滚）")

    def test_published_survives_restart_as_unique_conclusion(self):
        rec = self.svc.freeze("M-C2", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.svc.publish("M-C2", rec["snapshot_hash"])
        # 切换完成后进程重启
        store2 = self._reopen()
        self.assertEqual(store2.recover_interrupted(), [])
        row = store2.get("M-C2")
        self.assertEqual(row["stage"], "PUBLISHED")
        self.assertEqual(row["snapshot_hash"], rec["snapshot_hash"])
        # 原始摘要/会话/证据仍完整绑定
        self.assertEqual(row["old_summary"]["sha256"], rec["old_summary"]["sha256"])
        self.assertEqual(len(row["sessions"]), 2)
        self.assertTrue(row["verdict"]["publishable"])

    def test_recovery_keeps_published_while_rolling_frozen(self):
        a = self.svc.freeze("M-DONE", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        self.svc.publish("M-DONE", a["snapshot_hash"])
        self.svc.freeze("M-PENDING", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        store2 = self._reopen()
        rolled = store2.recover_interrupted()
        self.assertEqual(rolled, ["M-PENDING"])
        self.assertEqual(store2.get("M-DONE")["stage"], "PUBLISHED")
        self.assertIsNone(store2.get("M-PENDING"))

    def test_publish_commit_is_atomic(self):
        """发布与状态翻转在同一事务；磁盘上不存在“半发布”。"""
        rec = self.svc.freeze("M-C3", OLD_PAYLOAD, NEW_PAYLOAD, SESSIONS_OK)
        # 直接检查落盘内容：阶段只有 FROZEN/PUBLISHED 两种 CHECK 约束
        self.store._conn.commit()
        self.svc.publish("M-C3", rec["snapshot_hash"])
        store2 = self._reopen()
        row = store2.get("M-C3")
        self.assertIn(row["stage"], ("FROZEN", "PUBLISHED"))
        self.assertEqual(row["stage"], "PUBLISHED")


class StaleSnapshotDetectionTests(StoreTestBase):
    def test_stale_observed_output_rejected_at_freeze(self):
        stale = [{"session_id": "S2", "spec_version": "v1", "state": "OPEN",
                  "observed_output": "红灯"}]  # OPEN 的真实输出是绿灯
        with self.assertRaisesRegex(ValueError, "快照陈旧"):
            self.svc.freeze("M-S1", OLD_PAYLOAD, NEW_PAYLOAD, stale)

    def test_stale_spec_version_rejected_at_freeze(self):
        stale = [{"session_id": "S2", "spec_version": "v0", "state": "OPEN",
                  "observed_output": "绿灯"}]
        with self.assertRaisesRegex(ValueError, "快照陈旧"):
            self.svc.freeze("M-S2", OLD_PAYLOAD, NEW_PAYLOAD, stale)


if __name__ == "__main__":
    unittest.main()
