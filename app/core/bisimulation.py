"""最大双向匹配关系（greatest bisimulation）计算。

规则（对每一对仍匹配的旧状态 q 与新状态 r）：

1. 两侧可见输出必须相同：``output_old(q) == output_new(r)``；
2. 允许动作集合必须相同（同名动作两侧同时存在，缺一不可）；
3. 对每个同名动作 a，后继对 ``(δ_old(q,a), δ_new(r,a))`` 仍须匹配。

从冻结会话的 (旧当前状态, 新规程起始状态) 种子对出发，仅在可达状态对空间内
做划分细化（partition refinement），不动点即“最大”双向匹配关系。

任一冻结会话的种子对不在关系中，则整体不可发布；同时给出该会话沿动作可达
的首个阻断点路径作为证据。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import SessionSnapshot, Spec

Pair = Tuple[str, str]


@dataclass
class BlockingStep:
    index: int
    old_state: str
    new_state: str
    action: Optional[str]
    to_old: Optional[str]
    to_new: Optional[str]
    reason: str
    detail: str

    def to_json(self) -> Dict[str, Any]:
        return {
            "step": self.index,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "action": self.action,
            "to_old": self.to_old,
            "to_new": self.to_new,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class SessionVerdict:
    session_id: str
    ok: bool
    target_state: Optional[str]
    blocking_path: List[BlockingStep] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "ok": self.ok,
            "target_state": self.target_state,
            "blocking_path": [s.to_json() for s in self.blocking_path],
        }


@dataclass
class BisimulationResult:
    publishable: bool
    pairs: List[Dict[str, str]]                 # 全部匹配状态对（证据）
    sessions: List[SessionVerdict]
    checked_pairs: int

    def to_json(self) -> Dict[str, Any]:
        return {
            "publishable": self.publishable,
            "checked_pairs": self.checked_pairs,
            "matched_pairs": self.pairs,
            "sessions": [s.to_json() for s in self.sessions],
        }


def _signature_diff(o: Spec.State, n: Spec.State) -> Optional[Tuple[str, str]]:
    """返回 (reason, detail)；签名一致返回 None。"""
    if o.output != n.output:
        return "output_mismatch", (
            f"可见输出不同：旧={o.output!r}，新={n.output!r}"
        )
    old_only = o.actions.keys() - n.actions.keys()
    new_only = n.actions.keys() - o.actions.keys()
    if old_only:
        return "action_missing_new", (
            f"动作仅旧规程允许，新规程缺失：{sorted(old_only)}"
        )
    if new_only:
        return "action_missing_old", (
            f"动作仅新规程允许，旧规程缺失：{sorted(new_only)}"
        )
    return None


def compute_bisimulation(
    old: Spec, new: Spec, sessions: List[SessionSnapshot]
) -> BisimulationResult:
    # ---- 0. 会话与旧/新规程的一致性校验 ---------------------------------
    seeds: List[Tuple[str, Pair]] = []
    for snap in sessions:
        if snap.state not in old.states:
            raise ValueError(
                f"会话 {snap.session_id!r} 当前状态 {snap.state!r} 不在旧规程中"
            )
        if snap.target_state not in new.states:
            raise ValueError(
                f"会话 {snap.session_id!r} 拟迁入状态 {snap.target_state!r} 不在新规程中"
            )
        if snap.observed_output and snap.observed_output != old.states[snap.state].output:
            raise ValueError(
                f"会话 {snap.session_id!r} 冻结观察输出 "
                f"{snap.observed_output!r} 与旧规程状态 {snap.state} 的可见输出 "
                f"{old.states[snap.state].output!r} 不一致（快照陈旧）"
            )
        if snap.spec_version and old.raw.get("version"):
            old_version = str(old.raw["version"])
            if snap.spec_version != old_version:
                raise ValueError(
                    f"会话 {snap.session_id!r} 版本 {snap.spec_version!r} 与旧规程 "
                    f"版本 {old_version!r} 不一致（快照陈旧）"
                )
        seeds.append((snap.session_id, (snap.state, snap.target_state)))

    # ---- 1. 从所有种子对出发，枚举可达状态对 -----------------------------
    reachable: set[Pair] = set()
    queue: deque[Pair] = deque()
    for _, pair in seeds:
        if pair not in reachable:
            reachable.add(pair)
            queue.append(pair)
    while queue:
        q, r = queue.popleft()
        o_state, n_state = old.states[q], new.states[r]
        for action in o_state.actions.keys() & n_state.actions.keys():
            succ = (o_state.actions[action], n_state.actions[action])
            if succ not in reachable:
                reachable.add(succ)
                queue.append(succ)

    # ---- 2. 划分细化求最大双向匹配 --------------------------------------
    # R_0：签名（输出+允许动作集合）相同的可达对；逐轮剔除后继掉出 R 的对。
    relation: set[Pair] = {
        p for p in reachable
        if _signature_diff(old.states[p[0]], new.states[p[1]]) is None
    }
    # 记录被剔除的轮次，用于沿“最短反驳链”生成首个阻断路径
    removed_round: Dict[Pair, int] = {
        p: 1 for p in reachable if p not in relation
    }
    round_no = 1
    while True:
        drop: List[Pair] = []
        for q, r in relation:
            o_state, n_state = old.states[q], new.states[r]
            for action, o_target in o_state.actions.items():
                if (o_target, n_state.actions[action]) not in relation:
                    drop.append((q, r))
                    break
        if not drop:
            break
        round_no += 1
        for p in drop:
            relation.discard(p)
            removed_round[p] = round_no

    # ---- 3. 逐会话裁决与阻断路径取证 ------------------------------------
    session_verdicts: List[SessionVerdict] = []
    all_ok = True
    for sid, seed in seeds:
        if seed in relation:
            session_verdicts.append(
                SessionVerdict(sid, True, seed[1], [])
            )
            continue
        all_ok = False
        session_verdicts.append(
            SessionVerdict(
                sid, False, None,
                _blocking_path(seed, old, new, relation, removed_round),
            )
        )

    pairs = [
        {"old_state": q, "new_state": r}
        for q, r in sorted(relation)
    ]
    return BisimulationResult(
        publishable=all_ok,
        pairs=pairs,
        sessions=session_verdicts,
        checked_pairs=len(reachable),
    )


def _blocking_path(
    seed: Pair,
    old: Spec,
    new: Spec,
    relation: set[Pair],
    removed_round: Dict[Pair, int],
) -> List[BlockingStep]:
    """沿剔除轮次严格下降的后继走，必在有限步内到达签名冲突点。"""
    path: List[BlockingStep] = []
    q, r = seed
    step_no = 0
    while True:
        step_no += 1
        o_state, n_state = old.states[q], new.states[r]
        diff = _signature_diff(o_state, n_state)
        if diff is not None:
            reason, detail = diff
            path.append(BlockingStep(
                step_no, q, r, None, None, None, reason, detail,
            ))
            return path

        # 签名相同但该对仍被剔除：选择一个“掉出关系”且剔除轮次最小的动作
        best: Optional[Tuple[int, str, Pair]] = None
        for action, o_target in o_state.actions.items():
            succ = (o_target, n_state.actions[action])
            if succ not in relation:
                rank = removed_round.get(succ, 1)
                if best is None or rank < best[0]:
                    best = (rank, action, succ)

        # 理论上不可达：签名相同的被剔除对必有一个掉出关系的后继
        reason_cause = best
        if reason_cause is None:  # pragma: no cover - 防御性分支
            path.append(BlockingStep(
                step_no, q, r, None, None, None,
                "unstable", "该状态对无法稳定匹配，但找不到后继阻断点",
            ))
            return path

        _, action, (nq, nr) = reason_cause
        path.append(BlockingStep(
            step_no, q, r, action, nq, nr,
            "transition",
            f"经动作 {action!r} 到达 ({nq}, {nr})，该后继对不匹配，继续追踪",
        ))
        q, r = nq, nr
