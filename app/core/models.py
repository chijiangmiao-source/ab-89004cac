"""领域模型：确定有限状态规程（FSP）与会话快照。

规程的每个状态携带：
  * ``output``  —— 该状态下的可见输出；
  * ``actions`` —— 该状态允许的动作集合（动作名即迁移边标签）。

规程是“确定”的：同一状态下同名动作至多通向一个目标状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping


class SpecError(ValueError):
    """规程或会话载荷不合法。"""


@dataclass(frozen=True)
class Spec:
    """一份确定有限状态规程。"""

    name: str
    start: str
    states: Dict[str, "State"]
    # 规范化后的原始 JSON，供摘要/证据使用
    raw: Dict[str, Any]

    @dataclass(frozen=True)
    class State:
        name: str
        output: str
        actions: Dict[str, str]  # 动作名 -> 目标状态名

        def signature(self) -> tuple[str, frozenset[str]]:
            """输出与允许动作集合：迁移成立必须逐位相同。"""
            return self.output, frozenset(self.actions)

    def state(self, name: str) -> "Spec.State":
        try:
            return self.states[name]
        except KeyError:
            raise SpecError(f"状态 {name!r} 不存在于规程 {self.name!r}")

    def digest_payload(self) -> Dict[str, Any]:
        return {"name": self.name, "start": self.start, "spec": self.raw}


@dataclass(frozen=True)
class SessionSnapshot:
    """裁决发起瞬间冻结的会话快照。"""

    session_id: str
    spec_version: str          # 会话当前执行的规程版本（旧规程）
    state: str                 # 会话当前所处状态（旧规程）
    observed_output: str       # 冻结时观察到的可见输出（一致性核验用）
    target_state: str          # 拟迁入的新规程状态（默认与当前状态同名）

    def to_json(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "spec_version": self.spec_version,
            "state": self.state,
            "observed_output": self.observed_output,
            "target_state": self.target_state,
        }


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SpecError(msg)


def parse_spec(payload: Mapping[str, Any], *, default_name: str) -> Spec:
    """解析并校验一份规程 JSON。

    期望结构::

        {
          "name": "isol-v1",            # 可选
          "start": "LOCKED",
          "states": {
            "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
            ...
          }
        }
    """
    _require(isinstance(payload, Mapping), "规程必须是 JSON 对象")
    name = str(payload.get("name") or default_name)
    _require("start" in payload, "规程缺少 start 状态")
    start = str(payload["start"])
    raw_states = payload.get("states")
    _require(isinstance(raw_states, Mapping), "规程缺少 states 对象")

    states: Dict[str, Spec.State] = {}
    for sname, sbody in raw_states.items():
        sname = str(sname)
        _require(isinstance(sbody, Mapping), f"状态 {sname!r} 定义必须是对象")
        output = sbody.get("output", "")
        _require(isinstance(output, str), f"状态 {sname!r} 的 output 必须是字符串")
        raw_actions = sbody.get("actions", {})
        _require(
            isinstance(raw_actions, Mapping),
            f"状态 {sname!r} 的 actions 必须是对象",
        )
        actions: Dict[str, str] = {}
        for action, target in raw_actions.items():
            action = str(action)
            _require(
                isinstance(target, str),
                f"状态 {sname!r} 动作 {action!r} 的目标必须是状态名字符串",
            )
            _require(action not in actions, f"状态 {sname!r} 存在重复动作 {action!r}")
            actions[action] = target
        states[sname] = Spec.State(sname, output, actions)

    _require(start in states, f"start 状态 {start!r} 未在 states 中声明")
    for sname, st in states.items():
        for action, target in st.actions.items():
            _require(
                target in states,
                f"状态 {sname!r} 的动作 {action!r} 指向未声明状态 {target!r}",
            )
    return Spec(name=name, start=start, states=states, raw=dict(payload))


def parse_sessions(items: Any) -> List[SessionSnapshot]:
    """解析最多 12 个会话快照，并做基本一致性检查。"""
    _require(isinstance(items, list), "sessions 必须是数组")
    _require(len(items) > 0, "至少需要一个会话")
    _require(len(items) <= 12, "至多允许 12 个当前会话")
    sessions: List[SessionSnapshot] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        _require(isinstance(item, Mapping), f"sessions[{i}] 必须是对象")
        sid = item.get("session_id")
        _require(isinstance(sid, str) and sid.strip(), f"sessions[{i}].session_id 缺失")
        sid = sid.strip()
        _require(sid not in seen, f"会话 {sid!r} 重复")
        seen.add(sid)
        version = item.get("spec_version", "")
        _require(isinstance(version, str), f"会话 {sid!r} 的 spec_version 必须是字符串")
        state = item.get("state")
        _require(isinstance(state, str) and state, f"会话 {sid!r} 缺少当前状态")
        observed = item.get("observed_output", "")
        _require(isinstance(observed, str), f"会话 {sid!r} 的 observed_output 必须是字符串")
        target = item.get("target_state", "")
        _require(isinstance(target, str), f"会话 {sid!r} 的 target_state 必须是字符串")
        target = target.strip() or state
        sessions.append(SessionSnapshot(sid, version, state, observed, target))
    return sessions
