"""Durable adjudication store.

A single verdict row binds, atomically and durably:

  * old and new protocol digests + specs,
  * the frozen session snapshot and its version digest,
  * the per-session bisimulation evidence,
  * the lifecycle phase: FROZEN -> PUBLISHED.

Migration identifiers are idempotency keys.  A retransmitted identifier must
carry the *same* snapshot, otherwise it is a stale/concurrent request and is
rejected; sessions from two competing pages can never be mixed into one
verdict.  Publication is one conditional UPDATE, so even racing publishers
produce exactly one published conclusion.  SQLite WAL + fsync means a crash
after freeze or during switch can only recover FROZEN (unpublished) or the
single PUBLISHED row.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Dict, List, Optional, Tuple

from .bisimulation import SessionVerdict, adjudicate_session
from .models import Protocol, ProtocolError, Session

PHASE_FROZEN = "FROZEN"
PHASE_PUBLISHED = "PUBLISHED"
VALID_PHASES = (PHASE_FROZEN, PHASE_PUBLISHED)

MAX_SESSIONS = 12


class StaleSnapshotError(Exception):
    """Same migration id with a different frozen payload (stale page)."""


class SnapshotMismatchError(Exception):
    """Retransmission conflicts on protocol/session content."""


def canonical_digest(payload: dict) -> str:
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict (
    migration_id    TEXT PRIMARY KEY,
    phase           TEXT NOT NULL CHECK (phase IN ('FROZEN', 'PUBLISHED')),
    old_digest      TEXT NOT NULL,
    new_digest      TEXT NOT NULL,
    old_spec        TEXT NOT NULL,
    new_spec        TEXT NOT NULL,
    session_version TEXT NOT NULL,
    sessions_json   TEXT NOT NULL,
    results_json    TEXT NOT NULL,
    publishable     INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at    TEXT
);
"""


class VerdictStore:
    """Thread-safe SQLite wrapper; one connection guarded by a lock so that
    concurrent HTTP workers serialise their freeze/publish transactions."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # explicit BEGIN/COMMIT
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    def freeze(
        self,
        migration_id: str,
        old: Protocol,
        new: Protocol,
        sessions: List[Session],
        old_payload: dict,
        new_payload: dict,
        sessions_payload: list,
    ) -> Tuple[dict, bool]:
        """Freeze a verdict.  Returns (result_dict, created_now).

        If the migration id already exists, returns the stored verdict when
        every byte of the frozen payload matches; otherwise raises.
        """
        old_digest = canonical_digest(old_payload)
        new_digest = canonical_digest(new_payload)
        # The session version covers the ordered session snapshot itself.
        session_version = canonical_digest(sessions_payload)

        # Evaluate the bisimulation for every frozen session before writing.
        results: List[SessionVerdict] = [
            adjudicate_session(s.session_id, s.current_state, old, new)
            for s in sessions
        ]
        publishable = all(r.safe for r in results)
        results_json = json.dumps(
            [r.to_dict() for r in results], ensure_ascii=False
        )

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM verdict WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if row is not None:
                    self._check_same_snapshot(
                        row,
                        old_digest,
                        new_digest,
                        session_version,
                    )
                    self._conn.execute("COMMIT")
                    return self._row_to_dict(row), False

                self._conn.execute(
                    """
                    INSERT INTO verdict (
                        migration_id, phase, old_digest, new_digest,
                        old_spec, new_spec, session_version, sessions_json,
                        results_json, publishable
                    ) VALUES (?, 'FROZEN', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        migration_id,
                        old_digest,
                        new_digest,
                        json.dumps(old_payload, ensure_ascii=False, sort_keys=True),
                        json.dumps(new_payload, ensure_ascii=False, sort_keys=True),
                        session_version,
                        json.dumps(
                            sessions_payload, ensure_ascii=False, sort_keys=True
                        ),
                        results_json,
                        1 if publishable else 0,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            if self.path != ":memory:":
                # Checkpoint only after the write transaction has committed.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        row = self._conn.execute(
            "SELECT * FROM verdict WHERE migration_id=?", (migration_id,)
        ).fetchone()
        return self._row_to_dict(row), True

    @staticmethod
    def _check_same_snapshot(
        row: sqlite3.Row,
        old_digest: str,
        new_digest: str,
        session_version: str,
    ) -> None:
        mismatches = []
        if row["old_digest"] != old_digest:
            mismatches.append("old protocol")
        if row["new_digest"] != new_digest:
            mismatches.append("new protocol")
        if row["session_version"] != session_version:
            mismatches.append("session snapshot")
        if mismatches:
            raise SnapshotMismatchError(
                "migration id already frozen with a different "
                + ", ".join(mismatches)
                + "; refusing to mix concurrent snapshots"
            )

    def publish(self, migration_id: str) -> dict:
        """Conditionally publish.  Only a FROZEN, publishable verdict moves;
        a retransmitted publish is idempotent.  Racing publishers: exactly one
        conditional UPDATE wins."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM verdict WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise KeyError(migration_id)
                if row["phase"] == PHASE_PUBLISHED:
                    self._conn.execute("COMMIT")
                    return self._row_to_dict(row)
                if not row["publishable"]:
                    self._conn.execute("ROLLBACK")
                    raise StaleSnapshotError(
                        "verdict is not publishable: one or more frozen "
                        "sessions have no bisimulation mapping"
                    )
                cur = self._conn.execute(
                    """
                    UPDATE verdict
                       SET phase='PUBLISHED',
                           published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                     WHERE migration_id=? AND phase='FROZEN' AND publishable=1
                    """,
                    (migration_id,),
                )
                if cur.rowcount != 1:
                    # Lost a race or state changed underneath us.
                    self._conn.execute("ROLLBACK")
                    raise StaleSnapshotError("publication race; reload verdict")
                self._conn.execute("COMMIT")
            except Exception:
                # ROLLBACK may itself fail if the transaction is gone.
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            if self.path != ":memory:":
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        row = self._conn.execute(
            "SELECT * FROM verdict WHERE migration_id=?", (migration_id,)
        ).fetchone()
        return self._row_to_dict(row)

    def get(self, migration_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM verdict WHERE migration_id=?", (migration_id,)
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_verdicts(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM verdict ORDER BY created_at"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "migration_id": row["migration_id"],
            "phase": row["phase"],
            "publishable": bool(row["publishable"]),
            "old_digest": row["old_digest"],
            "new_digest": row["new_digest"],
            "session_version": row["session_version"],
            "sessions": json.loads(row["sessions_json"]),
            "old_spec": json.loads(row["old_spec"]),
            "new_spec": json.loads(row["new_spec"]),
            "results": json.loads(row["results_json"]),
            "created_at": row["created_at"],
            "published_at": row["published_at"],
        }
