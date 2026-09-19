"""HTTP 入口（标准库）。

路由：
- 顾问侧 /clients/<id>/... 管理账户、目标、版本、估值、提领、赔付与方案
- POST /plans/<id>/review 审核方案（重启后仍可对待确认方案继续审核）
- 客户侧 /portal/* 凭 view_token 只能看到本人的汇总与解释
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from service import PlanningService, ServiceError
from storage import Store

SERVICE_NAME = '家庭健康账户长期提领规划'

STORE = Store(os.getenv("RUNTIME_DIR", ".runtime"))
SERVICE = PlanningService(STORE)


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


class RequestHandler(BaseHTTPRequestHandler):
    service = SERVICE

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ServiceError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return payload

    # ---------------------------------------------------------- GET

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            if self.path == "/health":
                self._send(200, health_payload())
            elif len(parts) == 2 and parts[0] == "clients":
                self._send(200, self.service.client_summary(parts[1]))
            elif len(parts) == 3 and parts[0] == "clients" and parts[2] == "plans":
                status = query.get("status", [None])[0]
                self._send(200, {"plans": self.service.list_plans(parts[1], status)})
            elif len(parts) == 2 and parts[0] == "plans":
                self._send(200, self.service.get_plan(parts[1]))
            elif len(parts) == 1 and parts[0] == "portal":
                token = query.get("token", [""])[0]
                self._send(200, self.service.portal_summary(token))
            elif len(parts) == 3 and parts[0] == "portal" and parts[1] == "plans":
                token = query.get("token", [""])[0]
                self._send(200, self.service.portal_plan(token, parts[2]))
            else:
                self._send_error(404, "Not Found")
        except ServiceError as exc:
            self._send_error(400, str(exc))

    # ---------------------------------------------------------- POST

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            body = self._body()
            svc = self.service

            if len(parts) == 1 and parts[0] == "clients":
                self._send(201, svc.create_client(body))
            elif len(parts) == 3 and parts[0] == "clients":
                cid, resource = parts[1], parts[2]
                if resource == "accounts":
                    self._send(201, svc.add_account(cid, body))
                elif resource == "goals":
                    self._send(201, svc.add_goal(cid, body))
                elif resource == "tax-versions":
                    self._send(201, svc.add_tax_version(cid, body))
                elif resource == "holiday-versions":
                    self._send(201, svc.add_holiday_version(cid, body))
                elif resource == "valuations":
                    self._send(200, svc.import_valuation(cid, body))
                elif resource == "withdrawals":
                    self._send(201, svc.record_withdrawal(cid, body))
                elif resource == "payouts":
                    self._send(201, svc.register_payout(cid, body))
                elif resource == "plans":
                    self._send(201, svc.create_plan(cid, body))
                elif resource == "compare":
                    self._send(200, svc.compare(cid, list(body.get("plan_ids", []))))
                else:
                    self._send_error(404, "Not Found")
            elif len(parts) == 5 and parts[0] == "clients" and parts[2] == "payouts" and parts[4] == "arrived":
                self._send(200, svc.mark_payout_arrived(parts[1], parts[3], body))
            elif len(parts) == 3 and parts[0] == "plans" and parts[2] == "review":
                action = str(body.get("action", ""))
                self._send(200, svc.review_plan(parts[1], action, body.get("note")))
            else:
                self._send_error(404, "Not Found")
        except ServiceError as exc:
            self._send_error(400, str(exc))

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)
