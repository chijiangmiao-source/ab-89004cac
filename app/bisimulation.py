"""Greatest bisimulation between old and new protocols, reachable from a
session's current state, plus first blocking-path witnesses.

A state pair (s_old, s_new) is *compatible* only when:

  * visible outputs are equal,
  * the allowed-action sets are identical (an action reachable under one
    name must exist on both sides),
  * for every such action, the destination pair is itself compatible
    (coinductive greatest fixed point).

Because transitions are deterministic, the greatest fixed point is computed
by starting with every reachable pair and iteratively removing pairs whose
outputs/actions differ or whose successors have already been removed, until
the relation stabilises.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import Protocol

Pair = Tuple[str, str]


@dataclass
class PairEvidence:
    old_state: str
    new_state: str
    output: str
    actions: Tuple[str, ...]
    successors: Dict[str, Pair]


@dataclass
class BlockingStep:
    old_state: str
    new_state: str
    action: Optional[str]
    detail: str


@dataclass
class SessionVerdict:
    session_id: str
    current_state: str
    safe: bool
    target_state: Optional[str]
    mapping: List[PairEvidence] = field(default_factory=list)
    blocking_path: List[BlockingStep] = field(default_factory=list)
    blocking_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "current_state": self.current_state,
            "safe": self.safe,
            "target_state": self.target_state,
            "mapping": [
                {
                    "old_state": e.old_state,
                    "new_state": e.new_state,
                    "output": e.output,
                    "actions": list(e.actions),
                    "successors": {a: list(p) for a, p in e.successors.items()},
                }
                for e in self.mapping
            ],
            "blocking_path": [
                {
                    "old_state": s.old_state,
                    "new_state": s.new_state,
                    "action": s.action,
                    "detail": s.detail,
                }
                for s in self.blocking_path
            ],
            "blocking_reason": self.blocking_reason,
        }


def _static_mismatch(old_p: Protocol, new_p: Protocol, pair: Pair) -> Optional[str]:
    """Immediate (one-step) reason a pair cannot stay in the relation."""
    so, sn = pair
    io, in_ = old_p.states[so], new_p.states[sn]
    if io.output != in_.output:
        return (
            f"visible output differs: {so!r} emits {io.output!r} but "
            f"{sn!r} emits {in_.output!r}"
        )
    ao, an = set(io.transitions), set(in_.transitions)
    if ao != an:
        missing_new = sorted(ao - an)
        missing_old = sorted(an - ao)
        parts = []
        if missing_new:
            parts.append(f"action(s) {missing_new} missing in new protocol")
        if missing_old:
            parts.append(f"action(s) {missing_old} missing in old protocol")
        return "allowed actions differ: " + "; ".join(parts)
    return None


def compute_bisimulation(
    old_p: Protocol,
    new_p: Protocol,
    old_start: str,
    new_start: Optional[str] = None,
) -> Tuple[Dict[Pair, PairEvidence], bool]:
    """Greatest bisimulation restricted to states reachable from the anchor.

    The new-side anchor defaults to the same state name as the old side
    (a session migrates the state it currently occupies).  Returns the
    relation and whether the anchor pair belongs to it.
    """
    if new_start is None:
        new_start = old_start
    old_reach = set(old_p.reachable_from(old_start))
    new_reach = set(new_p.reachable_from(new_start))

    # Candidate pairs are cross-products of reachable states.
    relation: Dict[Pair, PairEvidence] = {}
    for so in old_reach:
        for sn in new_reach:
            relation[(so, sn)] = PairEvidence(
                old_state=so,
                new_state=sn,
                output=old_p.states[so].output,
                actions=tuple(sorted(old_p.states[so].transitions)),
                successors={
                    a: (
                        old_p.states[so].transitions[a],
                        new_p.states[sn].transitions[a],
                    )
                    for a in sorted(old_p.states[so].transitions)
                    if a in new_p.states[sn].transitions
                },
            )

    # Naive fixed point: remove pairs that fail static checks or whose
    # successor pairs are no longer in the relation.
    changed = True
    while changed:
        changed = False
        for pair in list(relation):
            so, sn = pair
            if _static_mismatch(old_p, new_p, pair) is not None:
                del relation[pair]
                changed = True
                continue
            io = old_p.states[so]
            in_ = new_p.states[sn]
            for action in io.transitions:
                succ = (io.transitions[action], in_.transitions[action])
                if succ not in relation:
                    del relation[pair]
                    changed = True
                    break
    anchor = (old_start, new_start)
    return relation, anchor in relation


def _mapping_closure(
    relation: Dict[Pair, PairEvidence], anchor: Pair
) -> List[PairEvidence]:
    """All relation entries reachable by following matched successors from
    the anchor — the evidence attached to the verdict."""
    out: List[PairEvidence] = []
    seen = set()
    queue = deque([anchor])
    while queue:
        pair = queue.popleft()
        if pair in seen or pair not in relation:
            continue
        seen.add(pair)
        evidence = relation[pair]
        out.append(evidence)
        for succ in evidence.successors.values():
            if succ not in seen:
                queue.append(succ)
    out.sort(key=lambda e: (e.old_state, e.new_state))
    return out


def find_blocking_path(
    old_p: Protocol, new_p: Protocol, relation: Dict[Pair, PairEvidence],
    old_start: str, new_start: Optional[str] = None,
) -> Tuple[List[BlockingStep], str]:
    """Shortest witness from the anchor to the pair that breaks concretely.

    A pair can leave the relation either because it fails a static check
    (different output / different allowed actions) or because one of its
    successors left first.  The useful witness walks matched action edges
    through statically-compatible pairs until it reaches the *first* pair
    with a concrete static mismatch — the root cause — rather than stopping
    at an intermediate pair that only fails transitively.  BFS yields the
    shortest such witness (e.g. idle --arm--> armed --fire--> done, where the
    new 'done' exposes an extra action).
    """
    if new_start is None:
        new_start = old_start
    anchor = (old_start, new_start)

    def explain(pair: Pair) -> str:
        reason = _static_mismatch(old_p, new_p, pair)
        if reason is not None:
            return reason
        so, sn = pair
        io, in_ = old_p.states[so], new_p.states[sn]
        shared = set(io.transitions) & set(in_.transitions)
        for action in sorted(shared):
            succ = (io.transitions[action], in_.transitions[action])
            if succ not in relation:
                return (
                    f"after action {action!r}: successor pair "
                    f"({succ[0]!r}, {succ[1]!r}) is not bisimilar"
                )
        return "pair is outside the bisimulation relation"

    def statically_compatible(pair: Pair) -> bool:
        return _static_mismatch(old_p, new_p, pair) is None

    def make_steps(pairs: List[Pair], acts: List[str],
                   final_pair: Pair, final_action: str,
                   final_reason: str) -> List[BlockingStep]:
        steps: List[BlockingStep] = []
        for idx, p in enumerate(pairs):
            steps.append(
                BlockingStep(
                    old_state=p[0],
                    new_state=p[1],
                    action=None if idx == 0 else acts[idx - 1],
                    detail="anchor" if idx == 0 else "matched",
                )
            )
        steps.append(
            BlockingStep(
                old_state=final_pair[0],
                new_state=final_pair[1],
                action=final_action,
                detail=final_reason,
            )
        )
        return steps

    # Anchor itself carries the concrete mismatch: no path to walk.
    if not statically_compatible(anchor):
        reason = explain(anchor)
        return (
            [BlockingStep(anchor[0], anchor[1], None, reason)],
            reason,
        )

    # BFS across statically-compatible pairs via identical action edges.
    came_from: Dict[Pair, Tuple[Optional[Pair], Optional[str]]] = {
        anchor: (None, None)
    }
    queue: deque[Pair] = deque([anchor])
    while queue:
        pair = queue.popleft()
        so, sn = pair
        io, in_ = old_p.states[so], new_p.states[sn]
        for action in sorted(io.transitions):
            if action not in in_.transitions:
                continue
            succ = (io.transitions[action], in_.transitions[action])
            if succ in came_from:
                continue
            if not statically_compatible(succ):
                # Reconstruct anchor..pair chain.
                path_pairs: List[Pair] = []
                acts: List[str] = []
                cur: Optional[Pair] = pair
                while cur is not None:
                    path_pairs.append(cur)
                    prev, act = came_from[cur]
                    if prev is None:
                        break
                    acts.append(act)
                    cur = prev
                path_pairs.reverse()
                acts.reverse()
                reason = explain(succ)
                return make_steps(path_pairs, acts, succ, action, reason), reason
            came_from[succ] = (pair, action)
            queue.append(succ)

    return [], "anchor is safe; no blocking path"


def adjudicate_session(
    session_id: str,
    current_state: str,
    old_p: Protocol,
    new_p: Protocol,
) -> SessionVerdict:
    if current_state not in old_p.states:
        raise ValueError(
            f"session {session_id!r} occupies unknown old state "
            f"{current_state!r}"
        )
    if current_state not in new_p.states:
        return SessionVerdict(
            session_id=session_id,
            current_state=current_state,
            safe=False,
            target_state=None,
            blocking_path=[
                BlockingStep(
                    old_state=current_state,
                    new_state="<absent>",
                    action=None,
                    detail=(
                        f"current state {current_state!r} has no same-named "
                        "state in the new protocol"
                    ),
                )
            ],
            blocking_reason=(
                f"current state {current_state!r} missing in new protocol"
            ),
        )

    relation, safe = compute_bisimulation(old_p, new_p, current_state)
    anchor = (current_state, current_state)
    if safe:
        mapping = _mapping_closure(relation, anchor)
        return SessionVerdict(
            session_id=session_id,
            current_state=current_state,
            safe=True,
            target_state=current_state,
            mapping=mapping,
        )

    steps, reason = find_blocking_path(old_p, new_p, relation, current_state)
    return SessionVerdict(
        session_id=session_id,
        current_state=current_state,
        safe=False,
        target_state=None,
        blocking_path=steps,
        blocking_reason=reason,
    )
