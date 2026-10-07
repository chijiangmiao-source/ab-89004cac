"""规则测试：最大双向匹配（互模拟）裁决。

覆盖验收三类核心案例：
  1. 可迁移：输出/允许动作/后继全部匹配；
  2. 动作缺失：任一侧独有动作导致阻断，并给出首个阻断路径；
  3. 陈旧快照：观察输出或规程版本与冻结瞬间不一致时直接拒绝。
另外用随机规程对照朴素不动点，验证求出的是“最大”关系而非仅起始状态比较。
"""
from __future__ import annotations

import random
import unittest

from app.core.bisimulation import compute_bisimulation
from app.core.models import SessionSnapshot, Spec, parse_sessions, parse_spec


def make_spec(name, start, states_raw, version=None):
    payload = {"name": name, "start": start, "states": states_raw}
    if version:
        payload["version"] = version
    return parse_spec(payload, default_name=name), payload


def snap(sid, state, target=None, version="v1", observed=None):
    return SessionSnapshot(sid, version, state, observed or "", target or state)


# 与页面示例一致的规程骨架
OLD = {
    "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
    "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
    "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
}


class MigratableTests(unittest.TestCase):
    def test_identical_specs_all_seeds_match(self):
        old, _ = make_spec("isol", "LOCKED", OLD, "v1")
        new, _ = make_spec("isol", "LOCKED", OLD, "v2")
        sessions = [
            snap("S1", "LOCKED", observed="红灯"),
            snap("S2", "OPEN", observed="绿灯"),
            snap("S3", "TRIPPED", observed="黄灯"),
        ]
        res = compute_bisimulation(old, new, sessions)
        self.assertTrue(res.publishable)
        self.assertEqual({s.session_id: s.target_state for s in res.sessions},
                         {"S1": "LOCKED", "S2": "OPEN", "S3": "TRIPPED"})
        self.assertTrue(all(s.ok and not s.blocking_path for s in res.sessions))
        # 三对同名状态互为双向匹配
        pairs = {(p["old_state"], p["new_state"]) for p in res.pairs}
        self.assertEqual(pairs, {("LOCKED", "LOCKED"), ("OPEN", "OPEN"),
                                 ("TRIPPED", "TRIPPED")})

    def test_renamed_equivalent_states_still_match(self):
        """状态改名但输出/动作结构一致：显式 target_state 也能匹配。"""
        new_states = {
            "L": {"output": "红灯", "actions": {"unlock": "O"}},
            "O": {"output": "绿灯", "actions": {"lock": "L", "trip": "T"}},
            "T": {"output": "黄灯", "actions": {"reset": "L"}},
        }
        old, _ = make_spec("isol", "LOCKED", OLD)
        new, _ = make_spec("isol2", "L", new_states)
        sessions = [snap("S1", "OPEN", target="O")]
        res = compute_bisimulation(old, new, sessions)
        self.assertTrue(res.publishable)
        self.assertEqual(res.sessions[0].target_state, "O")


class ActionMissingTests(unittest.TestCase):
    def test_action_missing_in_new_spec_blocks(self):
        new_states = {
            "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
            # 新规程 OPEN 缺 trip，且多出旧侧没有的 force_open
            "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED",
                                                     "force_open": "OPEN"}},
            "TRIPPED": {"output": "黄灯", "actions": {"reset": "LOCKED"}},
        }
        old, _ = make_spec("isol", "LOCKED", OLD)
        new, _ = make_spec("isol2", "LOCKED", new_states)
        res = compute_bisimulation(old, new, [snap("S7", "OPEN")])
        self.assertFalse(res.publishable)
        verdict = res.sessions[0]
        self.assertFalse(verdict.ok)
        self.assertIsNone(verdict.target_state)
        path = verdict.blocking_path
        self.assertTrue(path)
        self.assertIsNone(path[-1].action)  # 末步为签名冲突点
        self.assertEqual(path[-1].reason, "action_missing_new")
        self.assertIn("trip", path[-1].detail)
        # OPEN 对也不应出现在匹配证据中
        self.assertNotIn(("OPEN", "OPEN"),
                         {(p["old_state"], p["new_state"]) for p in res.pairs})

    def test_action_missing_in_old_spec_blocks(self):
        new_states = dict(OLD)
        new_states["LOCKED"] = {"output": "红灯",
                                "actions": {"unlock": "OPEN", "maint": "TRIPPED"}}
        old, _ = make_spec("isol", "LOCKED", OLD)
        new, _ = make_spec("isol2", "LOCKED", new_states)
        res = compute_bisimulation(old, new, [snap("S1", "LOCKED")])
        self.assertFalse(res.publishable)
        self.assertEqual(res.sessions[0].blocking_path[-1].reason,
                         "action_missing_old")

    def test_output_mismatch_blocks(self):
        new_states = {
            "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
            "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
            "TRIPPED": {"output": "橙灯", "actions": {"reset": "LOCKED"}},
        }
        old, _ = make_spec("isol", "LOCKED", OLD)
        new, _ = make_spec("isol2", "LOCKED", new_states)
        res = compute_bisimulation(old, new, [snap("S9", "TRIPPED")])
        self.assertFalse(res.publishable)
        last = res.sessions[0].blocking_path[-1]
        self.assertEqual(last.reason, "output_mismatch")


class NotJustStartStateTests(unittest.TestCase):
    """关键安全性质：不能只比较种子状态，必须沿所有同名动作追踪后继。"""

    def test_start_pair_equal_but_successor_broken(self):
        # 起始对输出/动作集合相同，但 unlock 通向的后继输出不同
        new_states = {
            "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
            "OPEN":   {"output": "蓝灯", "actions": {"lock": "LOCKED"}},
        }
        old_states = {
            "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
            "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED"}},
        }
        old, _ = make_spec("a", "LOCKED", old_states)
        new, _ = make_spec("b", "LOCKED", new_states)
        res = compute_bisimulation(old, new, [snap("S", "LOCKED")])
        self.assertFalse(res.publishable)
        path = res.sessions[0].blocking_path
        # 首个阻断路径必须先沿 unlock 下钻，再在后继处报输出冲突
        self.assertEqual(path[0].action, "unlock")
        self.assertEqual(path[-1].reason, "output_mismatch")

    def test_two_hop_violation_found(self):
        # LOCKED≈LOCKED、OPEN≈OPEN 签名都相同，违例藏在两跳之外的 TRIPPED
        new_states = {
            "LOCKED": {"output": "红灯", "actions": {"unlock": "OPEN"}},
            "OPEN":   {"output": "绿灯", "actions": {"lock": "LOCKED", "trip": "TRIPPED"}},
            "TRIPPED": {"output": "黑灯", "actions": {"reset": "LOCKED"}},
        }
        old, _ = make_spec("a", "LOCKED", OLD)
        new, _ = make_spec("b", "LOCKED", new_states)
        res = compute_bisimulation(old, new, [snap("S", "LOCKED")])
        self.assertFalse(res.publishable)
        actions = [s.action for s in res.sessions[0].blocking_path if s.action]
        self.assertEqual(actions, ["unlock", "trip"])
        self.assertEqual(res.sessions[0].blocking_path[-1].reason,
                         "output_mismatch")

    def test_publishable_only_when_all_frozen_sessions_mappable(self):
        # 两个互不连通的分量：SAFE 分量一致，BAD 分量的 WORSE 输出不同。
        old_states = {
            "SAFE":  {"output": "平", "actions": {"stay": "SAFE"}},
            "BAD":   {"output": "警", "actions": {"go": "WORSE"}},
            "WORSE": {"output": "危", "actions": {}},
        }
        new_states = {
            "SAFE":  {"output": "平", "actions": {"stay": "SAFE"}},
            "BAD":   {"output": "警", "actions": {"go": "WORSE"}},
            "WORSE": {"output": "险", "actions": {}},
        }
        old, _ = make_spec("a", "SAFE", old_states)
        new, _ = make_spec("b", "SAFE", new_states)
        # 一个会话安全（不可达损坏分量）、一个会话不安全：整体候选禁止发布
        res = compute_bisimulation(old, new,
                                   [snap("ok", "SAFE"), snap("bad", "BAD")])
        self.assertFalse(res.publishable)
        by_id = {s.session_id: s for s in res.sessions}
        self.assertTrue(by_id["ok"].ok)
        self.assertFalse(by_id["bad"].ok)


class StaleSnapshotTests(unittest.TestCase):
    def test_observed_output_inconsistent_rejected(self):
        old, _ = make_spec("isol", "LOCKED", OLD, version="v1")
        new, _ = make_spec("isol", "LOCKED", OLD, version="v2")
        # 现场实际已变绿灯，却拿来了红灯时刻的陈旧快照
        stale = snap("S", "OPEN", observed="红灯")
        with self.assertRaisesRegex(ValueError, "快照陈旧"):
            compute_bisimulation(old, new, [stale])

    def test_spec_version_inconsistent_rejected(self):
        old, _ = make_spec("isol", "LOCKED", OLD, version="v1")
        new, _ = make_spec("isol", "LOCKED", OLD, version="v2")
        stale = SessionSnapshot("S", "v9", "OPEN", "", "OPEN")
        with self.assertRaisesRegex(ValueError, "快照陈旧"):
            compute_bisimulation(old, new, [stale])

    def test_unknown_target_state_rejected(self):
        old, _ = make_spec("isol", "LOCKED", OLD)
        new, _ = make_spec("isol", "LOCKED", OLD)
        with self.assertRaisesRegex(ValueError, "不在新规程"):
            compute_bisimulation(old, new, [snap("S", "OPEN", target="GHOST")])


class ParserTests(unittest.TestCase):
    def test_session_limit_and_duplicates(self):
        items = [{"session_id": f"S{i}", "state": "LOCKED"} for i in range(13)]
        with self.assertRaisesRegex(ValueError, "至多允许 12"):
            parse_sessions(items)
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            parse_sessions([])
        with self.assertRaisesRegex(ValueError, "重复"):
            parse_sessions([{"session_id": "x", "state": "LOCKED"},
                            {"session_id": "x", "state": "OPEN"}])

    def test_dangling_transition_rejected(self):
        bad = {"start": "A", "states": {"A": {"output": "", "actions": {"go": "B"}}}}
        with self.assertRaisesRegex(ValueError, "未声明状态"):
            parse_spec(bad, default_name="x")


class GreatestFixedPointFuzzTests(unittest.TestCase):
    """随机规程：实现结果必须等于朴素全局不动点（最大双向匹配）。"""

    NAIVE_STATES = list("ABCDE")

    def naive_greatest_bisimulation(self, transitions_o, outputs_o,
                                    transitions_n, outputs_n):
        pairs = {(q, r) for q in self.NAIVE_STATES for r in self.NAIVE_STATES}
        while True:
            drop = set()
            for q, r in pairs:
                if outputs_o[q] != outputs_n[r]:
                    drop.add((q, r)); continue
                if set(transitions_o[q]) != set(transitions_n[r]):
                    drop.add((q, r)); continue
                for a in transitions_o[q]:
                    if (transitions_o[q][a], transitions_n[r][a]) not in pairs:
                        drop.add((q, r)); break
            if not drop:
                return pairs
            pairs -= drop

    def fuzz_once(self, seed):
        rng = random.Random(seed)
        actions = ["x", "y", "z"]

        def rand_spec():
            trans, outs = {}, {}
            for s in self.NAIVE_STATES:
                outs[s] = rng.choice(["o1", "o2", "o3"])
                chosen = [a for a in actions if rng.random() < 0.5]
                trans[s] = {a: rng.choice(self.NAIVE_STATES) for a in chosen}
            return trans, outs

        to, oo = rand_spec()
        tn, on = rand_spec()

        def build(trans, outs):
            return {s: {"output": outs[s],
                        "actions": dict(trans[s])} for s in self.NAIVE_STATES}

        old = parse_spec({"name": "o", "start": "A", "states": build(to, oo)},
                         default_name="o")
        new = parse_spec({"name": "n", "start": "A", "states": build(tn, on)},
                         default_name="n")
        # 为每一对状态放一个种子会话（直接构造快照，绕开 12 个上限）
        sessions = [
            SessionSnapshot(f"{q}-{r}", "", q, "", r)
            for q in self.NAIVE_STATES for r in self.NAIVE_STATES
        ]
        res = compute_bisimulation(old, new, sessions)
        got = {(p["old_state"], p["new_state"]) for p in res.pairs}
        expected = self.naive_greatest_bisimulation(to, oo, tn, on)
        self.assertEqual(got, expected, f"seed={seed} 时非最大双向匹配")

    def test_fuzz_300_random_specs(self):
        for seed in range(300):
            self.fuzz_once(seed)


if __name__ == "__main__":
    unittest.main()
