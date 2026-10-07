"""持久化裁决存储（SQLite）。

一个迁移标识 ``migration_id`` 对应唯一一条裁决记录，其生命周期：

    （不存在）──freeze──> FROZEN ──publish──> PUBLISHED（终态）

关键约束：

* 冻结时把旧/新规程摘要、会话版本、会话快照哈希、映射证据整体写入同一行；
  发布只是把该行在*同一事务*内由 FROZEN 翻转为 PUBLISHED，
  因此“切换前”崩溃只会留下 FROZEN，“切换后”崩溃留下的是唯一 PUBLISHED，
  不存在半发布状态。
* 进程重开时执行恢复：所有残留 FROZEN 视为中断的未发布裁决，回滚清理，
  使标识恢复为“未发布”；PUBLISHED 原样保留（唯一已发布结论）。
* 两个页面竞争同一标识：第二个冻结请求得到 409，绝不覆盖前一个会话快照。
* 发布必须回传冻结时的快照哈希；哈希不符即拒绝，防止混用会话快照。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    migration_id    TEXT PRIMARY KEY,
    stage           TEXT NOT NULL CHECK (stage IN ('FROZEN', 'PUBLISHED')),
    snapshot_hash   TEXT NOT NULL,
    old_summary     TEXT NOT NULL,
    new_summary     TEXT NOT NULL,
    sessions        TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    frozen_at       TEXT NOT NULL,
    published_at    TEXT
);
"""


class ConflictError(Exception):
    """标识已被占用且语义不允许当前操作。"""

    def __init__(self, code: str, message: str, existing: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.existing = existing


class Store:
    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False：HTTP 服务器多线程访问，靠事务+应用锁串行化写。
        # isolation_level=None（autocommit）：禁止 sqlite3 模块隐式 BEGIN，
        # 事务边界完全由下面的 BEGIN IMMEDIATE/commit/rollback 显式掌控，
        # 避免多线程下“cannot start a transaction within a transaction”。
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    def recover_interrupted(self) -> List[str]:
        """启动恢复：把中断在冻结阶段的裁决回滚为未发布。

        返回被回滚的迁移标识列表。PUBLISHED 记录保持不动。
        """
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT migration_id FROM decisions WHERE stage='FROZEN'"
            )
            rolled = [r["migration_id"] for r in cur.fetchall()]
            if rolled:
                self._conn.execute("DELETE FROM decisions WHERE stage='FROZEN'")
                self._conn.commit()
        return rolled

    # ------------------------------------------------------------------ #
    def freeze(
        self,
        migration_id: str,
        snapshot_hash: str,
        old_summary: Dict[str, Any],
        new_summary: Dict[str, Any],
        sessions: List[Dict[str, Any]],
        verdict: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        """冻结裁决。

        返回 (stage, record)。同一标识重传：
          * 载荷哈希相同 —— 幂等返回已有冻结/已发布记录；
          * 载荷哈希不同 —— 409（会话快照或规程被混用）。
        """
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM decisions WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if row is not None:
                    if row["snapshot_hash"] != snapshot_hash:
                        raise ConflictError(
                            "snapshot_mismatch",
                            f"迁移标识 {migration_id!r} 已绑定另一份会话快照/规程，"
                            "禁止混用",
                            existing=_row_to_dict(row),
                        )
                    return row["stage"], _row_to_dict(row)

                now = _utcnow()
                self._conn.execute(
                    """INSERT INTO decisions
                       (migration_id, stage, snapshot_hash, old_summary, new_summary,
                        sessions, verdict, frozen_at, published_at)
                       VALUES (?, 'FROZEN', ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        migration_id,
                        snapshot_hash,
                        json.dumps(old_summary, ensure_ascii=False, sort_keys=True),
                        json.dumps(new_summary, ensure_ascii=False, sort_keys=True),
                        json.dumps(sessions, ensure_ascii=False, sort_keys=True),
                        json.dumps(verdict, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM decisions WHERE migration_id=?", (migration_id,)
                ).fetchone()
                return "FROZEN", _row_to_dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def publish(
        self, migration_id: str, snapshot_hash: str
    ) -> Dict[str, Any]:
        """把冻结裁决发布。调用方须已确认裁决 publishable。"""
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM decisions WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if row is None:
                    raise ConflictError(
                        "not_frozen",
                        f"迁移标识 {migration_id!r} 尚未冻结，不能发布",
                    )
                if row["stage"] == "PUBLISHED":
                    if row["snapshot_hash"] != snapshot_hash:
                        raise ConflictError(
                            "snapshot_mismatch",
                            f"迁移标识 {migration_id!r} 已发布且绑定另一份快照，"
                            "禁止混用",
                            existing=_row_to_dict(row),
                        )
                    return _row_to_dict(row)  # 幂等重传
                if row["snapshot_hash"] != snapshot_hash:
                    raise ConflictError(
                        "snapshot_mismatch",
                        "发布请求携带的会话快照哈希与冻结时不一致，拒绝切换",
                        existing=_row_to_dict(row),
                    )
                verdict = json.loads(row["verdict"])
                if not verdict.get("publishable"):
                    raise ConflictError(
                        "not_publishable",
                        f"迁移标识 {migration_id!r} 存在不可映射会话，候选不得发布",
                        existing=_row_to_dict(row),
                    )
                self._conn.execute(
                    "UPDATE decisions SET stage='PUBLISHED', published_at=? "
                    "WHERE migration_id=? AND stage='FROZEN'",
                    (_utcnow(), migration_id),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM decisions WHERE migration_id=?", (migration_id,)
                ).fetchone()
                return _row_to_dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def get(self, migration_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE migration_id=?", (migration_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT migration_id, stage, frozen_at, published_at "
            "FROM decisions ORDER BY frozen_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def health(self) -> Dict[str, Any]:
        frozen = self._conn.execute(
            "SELECT COUNT(*) c FROM decisions WHERE stage='FROZEN'"
        ).fetchone()["c"]
        published = self._conn.execute(
            "SELECT COUNT(*) c FROM decisions WHERE stage='PUBLISHED'"
        ).fetchone()["c"]
        return {"frozen": frozen, "published": published}

    def close(self) -> None:
        self._conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("old_summary", "new_summary", "sessions", "verdict"):
        d[key] = json.loads(d[key])
    return d
