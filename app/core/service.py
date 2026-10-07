"""裁决编排：规程摘要、快照哈希与冻结/发布流程。"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from .bisimulation import compute_bisimulation
from .models import Spec, SpecError, parse_sessions, parse_spec
from .store import ConflictError, Store


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def spec_summary(spec: Spec, raw: Dict[str, Any]) -> Dict[str, Any]:
    action_count = sum(len(s.actions) for s in spec.states.values())
    return {
        "name": spec.name,
        "version": str(raw.get("version") or spec.name),
        "start": spec.start,
        "state_count": len(spec.states),
        "action_edge_count": action_count,
        "states": {
            name: {"output": st.output, "actions": sorted(st.actions)}
            for name, st in sorted(spec.states.items())
        },
        "sha256": hashlib.sha256(_canonical(raw)).hexdigest(),
    }


def snapshot_hash(
    migration_id: str,
    old_raw: Dict[str, Any],
    new_raw: Dict[str, Any],
    sessions: List[Dict[str, Any]],
) -> str:
    """绑定迁移标识、旧新规程原文与会话快照的防混用哈希。"""
    bound = {
        "migration_id": migration_id,
        "old_spec": old_raw,
        "new_spec": new_raw,
        "sessions": sessions,
    }
    return hashlib.sha256(_canonical(bound)).hexdigest()


class DecisionService:
    def __init__(self, store: Store):
        self.store = store

    def freeze(
        self,
        migration_id: str,
        old_payload: Dict[str, Any],
        new_payload: Dict[str, Any],
        session_items: List[Any],
    ) -> Dict[str, Any]:
        if not isinstance(migration_id, str) or not migration_id.strip():
            raise SpecError("migration_id 缺失")
        migration_id = migration_id.strip()

        old = parse_spec(old_payload, default_name="old")
        new = parse_spec(new_payload, default_name="new")
        sessions = parse_sessions(session_items)

        sessions_json = [s.to_json() for s in sessions]
        snap_hash = snapshot_hash(migration_id, old.raw, new.raw, sessions_json)
        old_sum = spec_summary(old, old.raw)
        new_sum = spec_summary(new, new.raw)

        result = compute_bisimulation(old, new, sessions)
        verdict = result.to_json()
        # 映射证据：匹配状态对 + 逐会话结论
        verdict["evidence"] = {
            "rule": "greatest bisimulation over reachable pairs "
                    "(equal outputs, equal allowed actions, matched successors)",
            "matched_pair_count": len(result.pairs),
            "reachable_pair_count": result.checked_pairs,
        }

        stage, record = self.store.freeze(
            migration_id, snap_hash, old_sum, new_sum, sessions_json, verdict,
        )
        return self._decorate(stage, record, snap_hash)

    def publish(self, migration_id: str, snapshot_hash_value: str | None) -> Dict[str, Any]:
        if not snapshot_hash_value:
            record = self.store.get(migration_id)
            if record is None:
                raise ConflictError("not_frozen", f"迁移标识 {migration_id!r} 不存在")
            snapshot_hash_value = record["snapshot_hash"]
        record = self.store.publish(migration_id, snapshot_hash_value)
        return self._decorate(record["stage"], record, record["snapshot_hash"])

    def get(self, migration_id: str) -> Dict[str, Any] | None:
        record = self.store.get(migration_id)
        if record is None:
            return None
        return self._decorate(record["stage"], record, record["snapshot_hash"])

    @staticmethod
    def _decorate(stage: str, record: Dict[str, Any], snap_hash: str) -> Dict[str, Any]:
        return {
            "migration_id": record["migration_id"],
            "stage": stage,
            "snapshot_hash": snap_hash,
            "frozen_at": record["frozen_at"],
            "published_at": record.get("published_at"),
            "old_summary": record["old_summary"],
            "new_summary": record["new_summary"],
            "sessions": record["sessions"],
            "verdict": record["verdict"],
        }
