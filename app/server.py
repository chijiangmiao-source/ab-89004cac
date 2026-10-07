"""HTTP API + single-page UI for protocol migration adjudication.

Pure standard library so the container image needs no network access.
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import urlparse

from .models import Protocol, ProtocolError, Session
from .store import (
    PHASE_FROZEN,
    PHASE_PUBLISHED,
    MAX_SESSIONS,
    SnapshotMismatchError,
    StaleSnapshotError,
    VerdictStore,
)

log = logging.getLogger("migration")

PUBLISH_PATH = re.compile(r"^/api/verdicts/([^/]+)/publish$")
VERDICT_PATH = re.compile(r"^/api/verdicts/([^/]+)$")


def _parse_freeze_payload(raw: bytes) -> Tuple[str, dict, dict, list, dict, dict]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("request body must be a JSON object")
    migration_id = payload.get("migration_id")
    if not isinstance(migration_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}", migration_id
    ):
        raise ProtocolError(
            "migration_id must be 1-128 chars of letters, digits, '_.-'"
        )
    old_payload = payload.get("old")
    new_payload = payload.get("new")
    sessions_payload = payload.get("sessions")
    if not isinstance(sessions_payload, list):
        raise ProtocolError("sessions must be a list (1-12 entries)")
    if not sessions_payload:
        raise ProtocolError("at least one current session is required")
    if len(sessions_payload) > MAX_SESSIONS:
        raise ProtocolError(f"at most {MAX_SESSIONS} sessions per verdict")
    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):
        raise ProtocolError("old and new must be protocol objects")
    return migration_id, old_payload, new_payload, sessions_payload


class Handler(BaseHTTPRequestHandler):
    server_version = "MigrationAdjudicator/1.0"

    # ---- helpers ---------------------------------------------------- #
    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 2_000_000:
            raise ProtocolError("request body too large (limit 2MB)")
        return self.rfile.read(length) if length else b""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _serve_index(self) -> None:
        here = os.path.dirname(os.path.abspath(__file__))
        page = os.path.join(here, "static", "index.html")
        try:
            with open(page, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "page missing"}
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- routing ---------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            store = self.server.store  # type: ignore[attr-defined]
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "phase_model": [PHASE_FROZEN, PHASE_PUBLISHED],
                    "verdicts": len(store.list_verdicts()),
                },
            )
            return
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        if path == "/api/verdicts":
            self._send_json(HTTPStatus.OK, {"verdicts": self.server.store.list_verdicts()})  # type: ignore[attr-defined]
            return
        m = VERDICT_PATH.match(path)
        if m:
            verdict = self.server.store.get(m.group(1))  # type: ignore[attr-defined]
            if verdict is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown migration id"})
            else:
                self._send_json(HTTPStatus.OK, verdict)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        store: VerdictStore = self.server.store  # type: ignore[attr-defined]
        try:
            if path == "/api/verdicts/freeze":
                (
                    migration_id,
                    old_payload,
                    new_payload,
                    sessions_payload,
                ) = _parse_freeze_payload(self._read_body())
                old = Protocol.from_dict(old_payload)
                new = Protocol.from_dict(new_payload)
                sessions = [Session.from_dict(s) for s in sessions_payload]
                ids = [s.session_id for s in sessions]
                if len(set(ids)) != len(ids):
                    raise ProtocolError("duplicate session_id in snapshot")
                for s in sessions:
                    if s.current_state not in old.states:
                        raise ProtocolError(
                            f"session {s.session_id!r}: current state "
                            f"{s.current_state!r} not in old protocol"
                        )
                verdict, created = store.freeze(
                    migration_id,
                    old,
                    new,
                    sessions,
                    old_payload,
                    new_payload,
                    sessions_payload,
                )
                self._send_json(
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"retransmitted": not created, **verdict},
                )
                return

            m = PUBLISH_PATH.match(path)
            if m:
                verdict = store.publish(m.group(1))
                self._send_json(HTTPStatus.OK, verdict)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except SnapshotMismatchError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except StaleSnapshotError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown migration id: {exc.args[0]}"})
        except ProtocolError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # pragma: no cover - defensive
            log.error("unhandled error\n%s", traceback.format_exc())
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
            )


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = VerdictStore(db_path)  # type: ignore[attr-defined]
    return server


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB_PATH", "/data/verdicts.db")
    server = build_server(host, port, db_path)
    log.info("migration adjudicator listening on %s:%s db=%s", host, port, db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.store.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
