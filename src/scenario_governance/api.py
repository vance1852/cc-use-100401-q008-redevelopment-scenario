"""无第三方依赖的二次开发情景治理 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import GovernanceService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到治理服务，便于无网络单元测试。"""

    def __init__(self, service: GovernanceService) -> None:
        self.service = service
        # 服务进程共享单个 SQLite 连接；用锁把并发 HTTP 请求串行化，
        # 避免两个工作线程在同一连接上交错开启事务。
        self._gate = threading.RLock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        with self._gate:
            return self._handle_locked(method, target, headers, body)

    def _handle_locked(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"])
                return Response(201, result)
            if method == "POST" and path == "/snapshots":
                return Response(201, self.service.register_snapshot(actor(), payload))
            if method == "POST" and path == "/plans":
                return Response(201, self.service.create_plan(actor(), payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "evaluate":
                return Response(200, self.service.evaluate_plan(actor(), parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                return Response(200, self.service.get_plan(parts[1]))
            if method == "POST" and path == "/plans/compare":
                result = self.service.compare_plans(actor(), payload.get("plan_ids", []))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "dossier":
                return Response(200, self.service.dossier(actor(), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "objections":
                result = self.service.add_objection(actor(), parts[1], payload["content"])
                return Response(201, result)
            if method == "POST" and path == "/decisions":
                expected_active = payload.get("expected_active_decision_id")
                result = self.service.decide(
                    actor(), payload["plan_id"], payload["outcome"], payload["conclusion"],
                    int(payload["expected_revision"]), payload.get("objections"),
                    None if expected_active is None else int(expected_active),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "assets" and parts[2] == "decision":
                result = self.service.active_decision(actor(), parts[1])
                return Response(200, {"decision": result})
            if method == "POST" and path == "/actuals":
                return Response(201, self.service.record_actual(actor(), payload))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor()))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ScenarioGovernance/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动二次开发情景治理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("scenario-governance.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(GovernanceService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
