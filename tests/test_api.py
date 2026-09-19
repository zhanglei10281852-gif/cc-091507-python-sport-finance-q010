from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app as app_module
from service import Service
from store import Store

ADVISOR = {"X-Advisor-Key": "advisor-key"}


def plan_body(client_id: str, snapshot_id: str | None = None, **over) -> dict:
    body = {
        "client_id": client_id,
        "snapshot_id": snapshot_id,
        "name": "十年健康提领",
        "start_month": "2027-01",
        "horizon_months": 120,
        "annual_return": 0.05,
        "fixed_monthly": 3000.0,
        "floor": 80000.0,
        "tax_version": "tax-v1",
        "holidays_version": "holidays-v1",
        "goals": [
            {"id": "fitness", "type": "fitness", "name": "年度健身",
             "cost": 12000.0, "priority": 2, "due_month": "2027-06"},
            {"id": "rehab", "type": "rehabilitation", "name": "术后康复",
             "cost": 60000.0, "priority": 1, "due_month": "2027-09"},
            {"id": "race", "type": "travel", "name": "海外赛事",
             "cost": 30000.0, "priority": 3, "due_month": "2028-04"},
        ],
        "payouts": [
            {"id": "ins1", "name": "手术保险赔付", "amount": 40000.0,
             "expected_date": "2027-08-15"},
        ],
    }
    body.update(over)
    return body


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.service = Service(Store(self.tmp_path))
        handler = app_module.create_handler(self.service)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    # ---------- 基础 ----------
    def test_health_and_auth(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])
        status, body = self.call("GET", "/plans")
        self.assertEqual(401, status)
        status, _ = self.call("POST", "/clients",
                              {"client_id": "c1", "name": "王", "pin": "1234"})
        self.assertEqual(401, status)

    def _register(self, cid="c1", pin="1234"):
        status, body = self.call("POST", "/clients", ADVISOR and {
            "client_id": cid, "name": cid, "pin": pin}, ADVISOR)
        self.assertEqual(201, status)
        return body

    def _import(self):
        payload = {
            "source_key": "custody-2026-12-31",
            "as_of_date": "2026-12-31",
            "currency": "CNY",
            "holdings": [
                {"symbol": "FUND-A", "quantity": 100000, "price": 5.0,
                 "currency": "CNY"},
            ],
        }
        status, body = self.call("POST", "/valuations", payload, ADVISOR)
        self.assertEqual(201, status)
        return body

    # ---------- 幂等快照 ----------
    def test_valuation_import_is_idempotent(self) -> None:
        first = self._import()
        status, second = self.call("POST", "/valuations", {
            "source_key": "custody-2026-12-31",
            "as_of_date": "2026-12-31",
            "holdings": [{"symbol": "FUND-A", "quantity": 100000,
                          "price": 5.0, "currency": "CNY"}]}, ADVISOR)
        self.assertEqual(201, status)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        status, listing = self.call("GET", "/valuations", headers=ADVISOR)
        self.assertEqual(1, len(listing["snapshots"]))

    # ---------- 完整工作流 ----------
    def test_plan_scenario_compare_lock_restart(self) -> None:
        self._register()
        snap = self._import()
        status, plan = self.call("POST", "/plans",
                                 plan_body("c1", snap["id"]), ADVISOR)
        self.assertEqual(201, status)
        self.assertEqual("pending", plan["status"])
        pid = plan["id"]
        self.assertIn("baseline", plan["scenarios"])
        base = plan["scenarios"]["baseline"]
        self.assertGreaterEqual(
            base["summary"]["coverage_rate"], 0.0)

        # 市场下跌情景
        status, crash = self.call("POST", f"/plans/{pid}/scenarios", {
            "key": "crash", "name": "明年股灾",
            "market_down": {"month": "2027-03", "monthly_return": -0.25}}, ADVISOR)
        self.assertEqual(201, status)
        self.assertEqual("tax-v1", crash["adopted_tax_version"])
        self.assertEqual("holidays-v1", crash["adopted_holidays_version"])

        # 赔付提前到账情景
        status, payout = self.call("POST", f"/plans/{pid}/scenarios", {
            "key": "payout_early", "name": "赔付提前",
            "payout_arrival": {"payout_id": "ins1",
                               "expected_date": "2027-02-09"}}, ADVISOR)
        self.assertEqual(201, status)

        # 比较：耗尽月份/覆盖率/延后项目
        status, cmp = self.call("POST", f"/plans/{pid}/compare", headers=ADVISOR)
        self.assertEqual(200, status)
        keys = {s["key"] for s in cmp["scenarios"]}
        self.assertEqual({"baseline", "crash", "payout_early"}, keys)
        for s in cmp["scenarios"]:
            self.assertTrue(s["explanation"])

        # 顾问视图含月度明细
        status, advisor_view = self.call("GET", f"/plans/{pid}", headers=ADVISOR)
        self.assertEqual(200, status)
        self.assertEqual(120, len(advisor_view["scenarios"]["baseline"]["monthly"]))

        # 登记已执行固定提领（1 月只提到 2000 净额），触发后续重算
        status, wd = self.call("POST", f"/plans/{pid}/withdrawals", {
            "month": "2027-01", "kind": "fixed",
            "net_amount": 2000.0, "gross_amount": 2000.0}, ADVISOR)
        self.assertEqual(201, status)
        status, view = self.call("GET", f"/plans/{pid}", headers=ADVISOR)
        jan = view["scenarios"]["baseline"]["monthly"][0]
        self.assertTrue(jan["locked"])
        self.assertEqual(2000.0, jan["fixed_net"])  # 新假设不能改写事实
        self.assertEqual("2027-01", view["as_of_month"])

        # 再次登记同样事实 -> 幂等，不重复
        status, wd2 = self.call("POST", f"/plans/{pid}/withdrawals", {
            "month": "2027-01", "kind": "fixed",
            "net_amount": 2000.0, "gross_amount": 2000.0}, ADVISOR)
        self.assertTrue(wd2["duplicate"])

        # 手动重算接口
        status, recalc = self.call("POST", f"/plans/{pid}/recompute", headers=ADVISOR)
        self.assertEqual(200, status)
        self.assertIn("crash", recalc)

        # 客户视图：只有汇总与解释，无月度明细
        client_h = {"X-Client-Id": "c1", "X-Client-Pin": "1234"}
        status, client_view = self.call("GET", f"/plans/{pid}", headers=client_h)
        self.assertEqual(200, status)
        self.assertNotIn("monthly", client_view["scenarios"]["baseline"])
        self.assertIn("explanation", client_view["scenarios"]["baseline"])

        # 客户审批（重启后仍可继续审核）
        status, decided = self.call("POST", f"/plans/{pid}/decision", {
            "decision": "approved"}, client_h)
        self.assertEqual(200, status)
        self.assertEqual("active", decided["status"])
        status, _ = self.call("POST", f"/plans/{pid}/decision", {
            "decision": "approved"}, client_h)
        self.assertEqual(409, status)

        # ---- 用同一状态文件新建 Service，模拟重启 ----
        del self.service
        service2 = Service(Store(self.tmp_path))
        handler2 = app_module.create_handler(service2)
        server2 = ThreadingHTTPServer(("127.0.0.1", 0), handler2)
        port2 = server2.server_address[1]
        t2 = threading.Thread(target=server2.serve_forever, daemon=True)
        t2.start()
        try:
            def call2(method, path, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port2}{path}", data=data, method=method,
                    headers={"Content-Type": "application/json", **(headers or {})})
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read().decode())

            status, restored = call2("GET", f"/plans/{pid}", headers=ADVISOR)
            self.assertEqual(200, status)
            self.assertEqual("active", restored["status"])
            self.assertEqual(1, len(restored["actuals"]))
            self.assertEqual(2000.0, restored["actuals"][0]["funded_net"])
            # 待确认方案在重启后仍可审核：新建一个 pending 方案，由客户审批
            status, p2 = call2("POST", "/plans",
                               plan_body("c1", initial_balance=500000.0), ADVISOR)
            self.assertEqual(201, status)
            status, d2 = call2("POST", f"/plans/{p2['id']}/decision",
                               {"decision": "approved"}, client_h)
            self.assertEqual(200, status)
            self.assertEqual("active", d2["status"])
        finally:
            server2.shutdown()
            server2.server_close()

    def test_client_isolation(self) -> None:
        self._register("c1")
        self._register("c2", "5678")
        snap = self._import()
        status, plan = self.call("POST", "/plans",
                                 plan_body("c1", snap["id"]), ADVISOR)
        pid = plan["id"]
        # c2 看不到 c1 的方案
        h2 = {"X-Client-Id": "c2", "X-Client-Pin": "5678"}
        status, listing = self.call("GET", "/plans", headers=h2)
        self.assertEqual(200, status)
        self.assertEqual([], listing["plans"])
        status, _ = self.call("GET", f"/plans/{pid}", headers=h2)
        self.assertEqual(403, status)
        status, _ = self.call("POST", f"/plans/{pid}/decision",
                              {"decision": "approved"}, h2)
        self.assertEqual(403, status)

    def test_cross_year_tax_and_holiday_versions_retained(self) -> None:
        # 新增跨年度税率表与节假日版本，情景采用后即使再注册新版本也保留原版本
        self._register()
        snap = self._import()
        status, _ = self.call("POST", "/tax-tables", {
            "version": "tax-v2",
            "table": {"2027": 0.20, "2028": 0.20}}, ADVISOR)
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/holidays", {
            "version": "holidays-v2",
            "holidays": ["2027-08-16"], "note": "调休"}, ADVISOR)
        self.assertEqual(201, status)
        body = plan_body("c1", snap["id"], tax_version="tax-v2",
                         holidays_version="holidays-v2", floor=0.0,
                         fixed_monthly=0.0,
                         goals=[{"id": "g1", "type": "travel", "name": "赛事",
                                 "cost": 10000.0, "priority": 1,
                                 "due_month": "2027-08"}],
                         payouts=[{"id": "p1", "amount": 10000.0,
                                   "expected_date": "2027-08-15"}])
        status, plan = self.call("POST", "/plans", body, ADVISOR)
        self.assertEqual(201, status)
        base = plan["scenarios"]["baseline"]
        self.assertEqual("tax-v2", base["adopted_tax_version"])
        self.assertEqual("holidays-v2", base["adopted_holidays_version"])
        # 赔付 8/15(周日) -> 8/16(节假日表 v2) -> 8/17 到账，目标 8 月到期仍 funded
        self.assertEqual("funded", base["summary"]["goals"]["g1"]["status"])
        self.assertEqual("2027-08", base["summary"]["goals"]["g1"]["funded_month"])
        # 冻结的采用税率为 20%
        self.assertEqual(0.2, base["assumptions_snapshot"]["tax_by_year"]["2027"])


if __name__ == "__main__":
    unittest.main()
