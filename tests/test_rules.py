"""Rule tests for the bisimulation adjudicator and durable verdict store.

Run cases:
  * migratable protocols (identical and state-renamed but bisimilar),
  * missing action on one side,
  * output mismatch,
  * privilege amplification one action later (initial states look equal),
  * candidate publication only when every frozen session maps,
  * retransmission with the same id/snapshot is idempotent,
  * concurrent / stale snapshots on one id are rejected (no mixing),
  * crash recovery restores FROZEN or the unique PUBLISHED verdict,
  * racing publishers converge on exactly one publication.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from app.bisimulation import adjudicate_session
from app.models import Protocol, Session
from app.store import (
    PHASE_FROZEN,
    PHASE_PUBLISHED,
    SnapshotMismatchError,
    StaleSnapshotError,
    VerdictStore,
)


def spec(states, name="p"):
    return {"name": name, "states": states}


IDENTICAL_OLD = spec(
    {
        "idle": {"output": "READY", "transitions": {"arm": "armed"}},
        "armed": {
            "output": "ARMED",
            "transitions": {"fire": "done", "disarm": "idle"},
        },
        "done": {"output": "DONE", "transitions": {"reset": "idle"}},
    },
    name="iso-v3",
)
IDENTICAL_NEW = json.loads(json.dumps(IDENTICAL_OLD))
IDENTICAL_NEW["name"] = "iso-v4"

# New protocol renames armed->engaged but keeps outputs/actions identical.
RENAMED_NEW = spec(
    {
        "idle": {"output": "READY", "transitions": {"arm": "engaged"}},
        "engaged": {
            "output": "ARMED",
            "transitions": {"fire": "done", "disarm": "idle"},
        },
        "done": {"output": "DONE", "transitions": {"reset": "idle"}},
    },
    name="iso-v4",
)

# Action missing on the new side at the anchor.
MISSING_ACTION_NEW = spec(
    {
        "idle": {"output": "READY", "transitions": {}},  # 'arm' removed
        "armed": {
            "output": "ARMED",
            "transitions": {"fire": "done", "disarm": "idle"},
        },
        "done": {"output": "DONE", "transitions": {"reset": "idle"}},
    },
    name="iso-v4",
)

# Anchor states look identical, but after 'fire' the new done-state exposes an
# extra 'elevate' action — privilege amplification one step later.  Comparing
# only initial states would wrongly admit this migration.
AMPLIFY_NEW = spec(
    {
        "idle": {"output": "READY", "transitions": {"arm": "armed"}},
        "armed": {
            "output": "ARMED",
            "transitions": {"fire": "done", "disarm": "idle"},
        },
        "done": {
            "output": "DONE",
            "transitions": {"reset": "idle", "elevate": "armed"},
        },
    },
    name="iso-v4",
)

# Output differs at anchor.
OUTPUT_DIFF_NEW = json.loads(json.dumps(IDENTICAL_OLD))
OUTPUT_DIFF_NEW["states"]["idle"]["output"] = "STANDBY"


class BisimulationRuleTests(unittest.TestCase):
    def verdict(self, old_s, new_s, state="idle", sid="s"):
        return adjudicate_session(
            sid, state, Protocol.from_dict(old_s), Protocol.from_dict(new_s)
        )

    def test_identical_protocols_migratable(self):
        v = self.verdict(IDENTICAL_OLD, IDENTICAL_NEW, "armed")
        self.assertTrue(v.safe)
        self.assertEqual(v.target_state, "armed")
        paired = {(e.old_state, e.new_state) for e in v.mapping}
        self.assertEqual(
            paired, {("armed", "armed"), ("idle", "idle"), ("done", "done")}
        )
        for e in v.mapping:
            # Every matched action leads to a still-matched pair.
            for action, pair in e.successors.items():
                self.assertIn(tuple(pair), paired, action)

    def test_renamed_state_still_bisimilar(self):
        v = self.verdict(IDENTICAL_OLD, RENAMED_NEW, "idle")
        self.assertTrue(v.safe, v.blocking_reason)
        paired = {(e.old_state, e.new_state) for e in v.mapping}
        self.assertIn(("armed", "engaged"), paired)

    def test_missing_action_blocks_with_witness(self):
        v = self.verdict(IDENTICAL_OLD, MISSING_ACTION_NEW, "idle")
        self.assertFalse(v.safe)
        self.assertIsNone(v.target_state)
        self.assertIn("arm", v.blocking_reason)
        self.assertEqual(v.blocking_path[0].old_state, "idle")
        self.assertIn("missing in new", v.blocking_path[0].detail)

    def test_privilege_amplification_next_step_blocks(self):
        # The anchor itself is identical; only a successor breaks.  This is
        # the case a start-state-only comparison would miss.
        v = self.verdict(IDENTICAL_OLD, AMPLIFY_NEW, "idle")
        self.assertFalse(v.safe)
        self.assertIn("elevate", v.blocking_reason)
        states = [(s.old_state, s.new_state) for s in v.blocking_path]
        self.assertEqual(states[0], ("idle", "idle"))
        self.assertIn(("done", "done"), states)
        last = v.blocking_path[-1]
        self.assertEqual((last.old_state, last.new_state), ("done", "done"))

    def test_output_mismatch_blocks(self):
        v = self.verdict(IDENTICAL_OLD, OUTPUT_DIFF_NEW, "idle")
        self.assertFalse(v.safe)
        self.assertIn("output", v.blocking_reason)

    def test_start_state_absent_in_new(self):
        # Incompatibility propagates backwards: 'done' looks equal but its
        # reset-successor (idle,idle) fails, so it cannot be admitted.
        v = self.verdict(IDENTICAL_OLD, MISSING_ACTION_NEW, "done")
        self.assertFalse(v.safe)
        self.assertEqual(
            [(s.old_state, s.new_state) for s in v.blocking_path],
            [("done", "done"), ("idle", "idle")],
        )

        # A current state with no same-named counterpart in the new protocol
        # is blocked outright at the anchor.
        v2 = self.verdict(IDENTICAL_OLD, RENAMED_NEW, "armed")
        self.assertFalse(v2.safe)
        self.assertIn("missing in new protocol", v2.blocking_reason)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "verdicts.db")
        self.store = VerdictStore(self.db)
        self.sessions = [
            {"session_id": "sess-A", "current_state": "idle"},
            {"session_id": "sess-B", "current_state": "armed"},
        ]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def freeze(self, old=IDENTICAL_OLD, new=IDENTICAL_NEW, sessions=None,
               mid="mig-1"):
        sessions = self.sessions if sessions is None else sessions
        objs = [Session.from_dict(s) for s in sessions]
        return self.store.freeze(
            mid,
            Protocol.from_dict(old),
            Protocol.from_dict(new),
            objs,
            old,
            new,
            sessions,
        )


class FreezePublishTests(StoreFixture):
    def test_freeze_binds_everything_in_one_row(self):
        v, created = self.freeze()
        self.assertTrue(created)
        self.assertEqual(v["phase"], PHASE_FROZEN)
        self.assertTrue(v["publishable"])
        self.assertEqual(len(v["old_digest"]), 64)
        self.assertEqual(len(v["new_digest"]), 64)
        self.assertEqual(len(v["session_version"]), 64)
        self.assertEqual(len(v["results"]), 2)
        self.assertTrue(all(r["safe"] for r in v["results"]))

    def test_one_blocking_session_forbids_publish(self):
        sessions = self.sessions + [
            {"session_id": "sess-C", "current_state": "idle"}
        ]
        # MISSING_ACTION_NEW makes idle->idle unsafe.
        v, _ = self.freeze(new=MISSING_ACTION_NEW, sessions=sessions)
        self.assertFalse(v["publishable"])
        blocked = [r for r in v["results"] if r["session_id"] == "sess-C"][0]
        self.assertFalse(blocked["safe"])
        with self.assertRaises(StaleSnapshotError):
            self.store.publish("mig-1")
        self.assertEqual(self.store.get("mig-1")["phase"], PHASE_FROZEN)

    def test_retransmit_same_snapshot_is_idempotent(self):
        v1, c1 = self.freeze()
        v2, c2 = self.freeze()
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(v1["session_version"], v2["session_version"])
        self.assertEqual(v1["results"], v2["results"])

    def test_stale_snapshot_same_id_rejected(self):
        self.freeze()
        stale = [
            {"session_id": "sess-A", "current_state": "idle"},
            {"session_id": "sess-B", "current_state": "done"},  # moved!
        ]
        with self.assertRaises(SnapshotMismatchError) as ctx:
            self.freeze(sessions=stale)
        self.assertIn("session snapshot", str(ctx.exception))

    def test_competing_pages_different_protocol_rejected(self):
        self.freeze()
        with self.assertRaises(SnapshotMismatchError):
            self.freeze(new=RENAMED_NEW)

    def test_publish_is_unique_and_idempotent(self):
        self.freeze()
        v = self.store.publish("mig-1")
        self.assertEqual(v["phase"], PHASE_PUBLISHED)
        self.assertIsNotNone(v["published_at"])
        # Retransmitted publish returns the same unique conclusion.
        again = self.store.publish("mig-1")
        self.assertEqual(again["published_at"], v["published_at"])

    def test_concurrent_publish_single_winner(self):
        self.freeze()
        outcomes = []

        def publish():
            try:
                outcomes.append(self.store.publish("mig-1")["phase"])
            except Exception as exc:  # pragma: no cover - failure path
                outcomes.append(("ERR", str(exc)))

        threads = [threading.Thread(target=publish) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes, [PHASE_PUBLISHED] * 8)
        self.assertEqual(
            self.store.get("mig-1")["phase"], PHASE_PUBLISHED
        )

    def test_concurrent_freeze_different_snapshots_no_mixing(self):
        errors = []

        def worker(sessions, tag):
            try:
                self.store.freeze(
                    "race-id",
                    Protocol.from_dict(IDENTICAL_OLD),
                    Protocol.from_dict(RENAMED_NEW),
                    [Session.from_dict(s) for s in sessions],
                    IDENTICAL_OLD,
                    RENAMED_NEW,
                    sessions,
                )
            except SnapshotMismatchError as exc:
                errors.append(str(exc))

        s1 = [{"session_id": "sess-A", "current_state": "idle"}]
        s2 = [{"session_id": "sess-A", "current_state": "armed"}]
        threads = [
            threading.Thread(target=worker, args=(s1, 1)),
            threading.Thread(target=worker, args=(s2, 2)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exactly one side is rejected; the stored snapshot is never a blend.
        self.assertEqual(len(errors), 1)
        stored = self.store.get("race-id")
        self.assertEqual(len(stored["sessions"]), 1)
        self.assertIn(stored["sessions"][0]["current_state"], {"idle", "armed"})


class CrashRecoveryTests(StoreFixture):
    def _reopen(self) -> VerdictStore:
        self.store.close()
        return VerdictStore(self.db)

    def test_recovers_frozen_after_reopen(self):
        self.freeze()
        reopened = self._reopen()
        v = reopened.get("mig-1")
        self.assertEqual(v["phase"], PHASE_FROZEN)
        self.assertEqual(len(v["results"]), 2)
        reopened.close()

    def test_recovers_unique_published_after_reopen(self):
        self.freeze()
        self.store.publish("mig-1")
        reopened = self._reopen()
        v = reopened.get("mig-1")
        self.assertEqual(v["phase"], PHASE_PUBLISHED)
        self.assertTrue(v["publishable"])
        # A frozen row can never become published twice / fork into two rows.
        self.assertEqual(len(reopened.list_verdicts()), 1)
        reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
