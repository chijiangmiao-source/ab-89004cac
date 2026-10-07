"""HTTP 服务：裁决 API 与页面，全部基于 Python 标准库。

环境变量：
  HOST   监听地址，默认 0.0.0.0
  PORT   监听端口，默认 8080
  DB     SQLite 路径，默认 /data/decisions.db
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from .core.models import SpecError
from .core.service import DecisionService
from .core.store import ConflictError, Store

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class Handler(BaseHTTPRequestHandler):
    server_version = "IsolationVerdict/1.0"

    # ---- 依赖由 server 对象注入 ----------------------------------------
    @property
    def service(self) -> DecisionService:
        return self.server.service  # type: ignore[attr-defined]

    # ---- 工具 ----------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise SpecError("请求体为空")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpecError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise SpecError("请求体必须是 JSON 对象")
        return data

    def _send_html(self, status: int, html: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, fmt: str, *args) -> None:  # 简洁日志
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/health":
                stats = self.service.store.health()
                self._send_json(HTTPStatus.OK, {"status": "ok", **stats})
            elif path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                    self._send_html(HTTPStatus.OK, fh.read())
            elif path == "/api/decisions":
                self._send_json(HTTPStatus.OK, {"decisions": self.service.store.list()})
            elif path.startswith("/api/decisions/"):
                migration_id = path[len("/api/decisions/"):]
                record = self.service.get(migration_id)
                if record is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                else:
                    self._send_json(HTTPStatus.OK, record)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except Exception:  # pragma: no cover - 防御
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/decisions/freeze":
                data = self._read_json()
                record = self.service.freeze(
                    str(data.get("migration_id", "")),
                    data.get("old_spec"),
                    data.get("new_spec"),
                    data.get("sessions"),
                )
                self._send_json(HTTPStatus.OK, record)
            elif path.startswith("/api/decisions/") and path.endswith("/publish"):
                migration_id = path[len("/api/decisions/"):-len("/publish")]
                data = self._read_json() if int(self.headers.get("Content-Length") or 0) else {}
                record = self.service.publish(migration_id, data.get("snapshot_hash"))
                self._send_json(HTTPStatus.OK, record)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except SpecError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)})
        except ConflictError as exc:
            payload = {"error": exc.code, "message": str(exc)}
            if exc.existing:
                payload["existing_stage"] = exc.existing.get("stage")
            self._send_json(HTTPStatus.CONFLICT, payload)
        except Exception:  # pragma: no cover - 防御
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal"})


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    store = Store(db_path)
    rolled = store.recover_interrupted()
    if rolled:
        print(f"[recover] 中断的冻结裁决已回滚为未发布: {rolled}", file=sys.stderr)
    service = DecisionService(store)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB", "/data/decisions.db")
    httpd = build_server(host, port, db_path)
    print(f"隔离控制协议迁移裁决服务监听 http://{host}:{port} （DB={db_path}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
