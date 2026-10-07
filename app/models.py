"""Domain model for deterministic finite-state control protocols (FSMs).

A protocol ("regulation") maps named states to a visible output and an
allowed-action table.  Transitions are deterministic: (state, action) -> state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple


class ProtocolError(ValueError):
    """Raised when a protocol or session payload is malformed."""


@dataclass(frozen=True)
class StateInfo:
    output: str
    transitions: Dict[str, str]  # action -> destination state

    @property
    def actions(self) -> Tuple[str, ...]:
        return tuple(sorted(self.transitions))


@dataclass(frozen=True)
class Protocol:
    name: str
    states: Dict[str, StateInfo]

    @classmethod
    def from_dict(cls, payload: dict) -> "Protocol":
        if not isinstance(payload, dict):
            raise ProtocolError("protocol must be an object")
        name = payload.get("name", "")
        if not isinstance(name, str):
            raise ProtocolError("protocol.name must be a string")
        raw_states = payload.get("states")
        if not isinstance(raw_states, dict) or not raw_states:
            raise ProtocolError("protocol.states must be a non-empty object")

        states: Dict[str, StateInfo] = {}
        for state_name, raw in raw_states.items():
            if not isinstance(state_name, str) or not state_name:
                raise ProtocolError("state names must be non-empty strings")
            if not isinstance(raw, dict):
                raise ProtocolError(f"state {state_name!r} must be an object")
            output = raw.get("output")
            if not isinstance(output, str) or not output:
                raise ProtocolError(
                    f"state {state_name!r} requires a non-empty 'output'"
                )
            raw_trans = raw.get("transitions", {})
            if not isinstance(raw_trans, dict):
                raise ProtocolError(
                    f"state {state_name!r} transitions must be an object"
                )
            transitions: Dict[str, str] = {}
            for action, dest in raw_trans.items():
                if not isinstance(action, str) or not action:
                    raise ProtocolError(
                        f"state {state_name!r}: action names must be non-empty"
                    )
                if not isinstance(dest, str) or not dest:
                    raise ProtocolError(
                        f"state {state_name!r} action {action!r}: "
                        "destination must be a non-empty string"
                    )
                if action in transitions:
                    raise ProtocolError(
                        f"state {state_name!r}: duplicate action {action!r}"
                    )
                transitions[action] = dest
            states[state_name] = StateInfo(output=output, transitions=transitions)

        # Determinism is guaranteed by dict construction; verify all edges.
        for sname, info in states.items():
            for action, dest in info.transitions.items():
                if dest not in states:
                    raise ProtocolError(
                        f"state {sname!r} action {action!r} leads to unknown "
                        f"state {dest!r}"
                    )
        return cls(name=name, states=states)

    def reachable_from(self, start: str) -> Dict[str, int]:
        """Return {state: distance} for every state reachable from start."""
        if start not in self.states:
            raise ProtocolError(f"current state {start!r} does not exist")
        seen = {start: 0}
        frontier = [start]
        while frontier:
            cur = frontier.pop()
            for dest in self.states[cur].transitions.values():
                if dest not in seen:
                    seen[dest] = seen[cur] + 1
                    frontier.append(dest)
        return seen


@dataclass(frozen=True)
class Session:
    session_id: str
    current_state: str

    @classmethod
    def from_dict(cls, raw: dict) -> "Session":
        if not isinstance(raw, dict):
            raise ProtocolError("each session must be an object")
        sid = raw.get("session_id")
        state = raw.get("current_state")
        if not isinstance(sid, str) or not sid:
            raise ProtocolError("session.session_id must be a non-empty string")
        if not isinstance(state, str) or not state:
            raise ProtocolError("session.current_state must be a non-empty string")
        return cls(session_id=sid, current_state=state)


@dataclass(frozen=True)
class MigrationRequest:
    migration_id: str
    old: Protocol
    new: Protocol
    sessions: Tuple[Session, ...]
    snapshots: Dict[str, str] = field(default_factory=dict)
