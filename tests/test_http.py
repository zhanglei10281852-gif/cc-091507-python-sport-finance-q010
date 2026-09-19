"""HTTP 层冒烟：在随机端口起真实服务，验证健康检查、客户门户路由与跨客户 400。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app as app_module
from service import PlanningService
from storage import Store


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        app_module.SERVICE = PlanningService(Store(self.tmp.name))
        app_module.RequestHandler.service = app_module.SERVICE
        self.server = app_module.create_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_health(self) -> None:
        status, data = self._request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", data["status"])

    def test_portal_routing_and_auth(self) -> None:
        _, created = self._request("POST", "/clients", {"id": "c1", "name": "家庭甲"})
        token = created["view_token"]
        status, data = self._request("GET", f"/portal?token={token}")
        self.assertEqual(200, status)
        self.assertEqual("c1", data["client"]["id"])
        status, data = self._request("GET", "/portal?token=bad")
        self.assertEqual(400, status)
        status, _ = self._request("GET", "/portal")
        self.assertEqual(400, status)
        status, _ = self._request("GET", "/nope")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
