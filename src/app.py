"""HTTP 入口：标准库路由、JSON 序列化与角色鉴权。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from service import Service, ServiceError
from store import Store

SERVICE_NAME = '家庭健康账户长期提领规划'
SRC_DIR = Path(__file__).resolve().parent

_DEFAULT_STORE = Store(Path(os.getenv("RUNTIME_DIR", SRC_DIR.parent / ".runtime")))
_DEFAULT_ADVISOR_KEY = os.getenv("ADVISOR_KEY", "advisor-key")


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def create_handler(service: Service) -> type:
    """构造绑定指定 service 的请求处理器（便于测试隔离）。"""

    class RequestHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ServiceError(400, "bad_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ServiceError(400, "bad_json", "请求体应为 JSON 对象")
            return data

        # ---- 鉴权 ----
        def _auth(self):
            """返回 (user|None, role|None)。顾问以 X-Advisor-Key 标识。"""
            if self.headers.get("X-Advisor-Key") == _DEFAULT_ADVISOR_KEY:
                return None, "advisor"
            client_id = self.headers.get("X-Client-Id")
            pin = self.headers.get("X-Client-Pin")
            if client_id:
                state = service.store.snapshot()
                user = service.authenticate(state, client_id, pin)
                if user:
                    return user, "client"
            return None, None

        def _require_advisor(self):
            _, role = self._auth()
            if role != "advisor":
                raise ServiceError(401, "unauthorized", "需要顾问凭据 (X-Advisor-Key)")
            return True

        def _require_party(self):
            user, role = self._auth()
            if not role:
                raise ServiceError(401, "unauthorized", "需要客户或顾问凭据")
            return user, role

        # ---- 路由 ----
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                segs = [s for s in path.split("/") if s]
                body = self._read_json() if method == "POST" else {}

                if method == "GET" and path == "/health":
                    return self._send_json(200, health_payload())

                if method == "POST" and path == "/clients":
                    self._require_advisor()
                    return self._send_json(201, service.register_client(
                        body.get("client_id", ""), body.get("name", ""),
                        body.get("pin", "")))

                if method == "POST" and path == "/valuations":
                    self._require_advisor()
                    return self._send_json(201, service.import_valuation(body))
                if method == "GET" and path == "/valuations":
                    self._require_advisor()
                    return self._send_json(200, {"snapshots": service.list_snapshots()})

                if method == "POST" and path == "/tax-tables":
                    self._require_advisor()
                    return self._send_json(201, service.add_tax_table(
                        body.get("version", ""), body.get("table", {})))
                if method == "POST" and path == "/holidays":
                    self._require_advisor()
                    return self._send_json(201, service.add_holiday_calendar(
                        body.get("version", ""), body.get("holidays", []),
                        body.get("note", "")))

                if method == "POST" and path == "/plans":
                    self._require_advisor()
                    return self._send_json(201, service.create_plan(body, advisor=True))
                if method == "GET" and path == "/plans":
                    user, role = self._require_party()
                    return self._send_json(200, {"plans": service.list_plans(user, role)})

                if len(segs) == 2 and segs[0] == "plans":
                    plan_id = segs[1]
                    if method == "GET":
                        user, role = self._require_party()
                        return self._send_json(200, service.get_plan(plan_id, user, role))

                if len(segs) == 3 and segs[0] == "plans":
                    plan_id, action = segs[1], segs[2]
                    if method == "POST" and action == "scenarios":
                        self._require_advisor()
                        return self._send_json(201, service.add_scenario(plan_id, body, True))
                    if method == "POST" and action == "recompute":
                        self._require_advisor()
                        return self._send_json(200, service.recompute(plan_id, True))
                    if method == "POST" and action == "withdrawals":
                        self._require_advisor()
                        return self._send_json(201, service.execute_withdrawal(plan_id, body, True))
                    if method == "POST" and action == "compare":
                        user, role = self._require_party()
                        return self._send_json(200, service.compare(plan_id, user, role))
                    if method == "POST" and action == "decision":
                        user, _ = self._auth()
                        if not user:
                            raise ServiceError(401, "unauthorized",
                                               "决策需要客户凭据 (X-Client-Id/Pin)")
                        return self._send_json(200, service.decide_plan(
                            plan_id, body.get("decision", ""), user))

                raise ServiceError(404, "not_found", f"未找到路由: {method} {path}")
            except ServiceError as exc:
                self._send_json(exc.status, {"error": exc.code, "message": exc.message})
            except (ValueError, TypeError, KeyError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

    RequestHandler.service = service
    return RequestHandler


def create_server(host: str, port: int, handler: type | None = None) -> ThreadingHTTPServer:
    handler = handler or create_handler(Service(_DEFAULT_STORE))
    return ThreadingHTTPServer((host, port), handler)
