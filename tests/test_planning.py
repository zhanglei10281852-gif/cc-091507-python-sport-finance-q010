"""提领规划服务的端到端测试。

覆盖：基线规划、市场下跌重算、目标提前、赔付到账、已执行提领不可覆盖、
跨年度税率版本、节假日顺延、估值幂等、客户隔离、情景对比与重启续审。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine import EngineInput, build_plan
from service import PlanningService, ServiceError
from storage import Store


def build_fixture(service: PlanningService, cid: str = "c1", balance: float = 1_200_000.0) -> str:
    service.create_client({"id": cid, "name": "测试家庭"})
    service.add_account(cid, {"id": "A1", "name": "券商账户", "currency": "CNY", "expected_return": 0.05})
    service.add_goal(cid, {"id": "G_SURGERY", "name": "明年手术康复", "type": "rehabilitation",
                           "amount": 150_000, "due_month": "2027-03", "priority": 1, "deferrable": False})
    service.add_goal(cid, {"id": "G_FIT", "name": "年度健身计划", "type": "fitness",
                           "amount": 60_000, "due_month": "2027-06", "priority": 5, "deferrable": True})
    service.add_goal(cid, {"id": "G_TRAVEL", "name": "赛事旅行", "type": "travel",
                           "amount": 80_000, "due_month": "2028-05", "priority": 8, "deferrable": True})
    service.add_goal(cid, {"id": "G_RESERVE", "name": "医疗应急金", "type": "emergency_reserve",
                           "amount": 200_000, "priority": 1})
    for year in range(2026, 2037):
        service.add_tax_version(cid, {"id": f"TAX_{year}_V1", "year": year, "name": f"{year}初版", "rate": 0.10})
    service.add_holiday_version(cid, {"id": "HOL_V1", "name": "节假日表V1",
                                      "holidays": ["2027-01-01", "2027-02-15"]})
    service.import_valuation(cid, {"as_of": "2026-09-15", "source_ref": "stmt-2026-09",
                                   "positions": [{"account_id": "A1", "amount": balance}]})
    return cid


def baseline_assumptions(**overrides) -> dict:
    a = {
        "name": "基线方案",
        "start_month": "2026-10",
        "horizon_months": 120,
        "fixed_monthly_withdrawal": 3_000,
    }
    a.update(overrides)
    return a


class PlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PlanningService(Store(self.tmp.name))
        build_fixture(self.service)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_baseline_plan_covers_goals_and_preserves_floor(self) -> None:
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        self.assertIsNone(plan["summary"]["exhaustion_month"])
        self.assertIsNone(plan["summary"]["floor_breach_month"])
        coverage = {c["goal_id"]: c for c in plan["summary"]["goal_coverage"]}
        for gid in ("G_SURGERY", "G_FIT", "G_TRAVEL"):
            self.assertEqual(coverage[gid]["status"], "funded", gid)
            self.assertAlmostEqual(coverage[gid]["coverage_pct"], 100.0, places=1)
        # 手术在 2027-03 强制出资，健身在 2027-06。
        self.assertEqual(coverage["G_SURGERY"]["funded_month"], "2027-03")
        self.assertEqual(coverage["G_FIT"]["funded_month"], "2027-06")
        self.assertIn("安全底线始终得到保留", plan["explanation"])

    def test_tax_gross_up_withholding(self) -> None:
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        row = next(r for r in plan["rows"] if r["month"] == "2026-10")
        self.assertFalse(row["settled"])
        self.assertAlmostEqual(row["net_withdrawal"], 3_000, places=2)
        self.assertAlmostEqual(row["gross_withdrawal"], 3_000 / 0.9, places=2)
        self.assertAlmostEqual(row["tax_withheld"], 3_000 / 0.9 - 3_000, places=2)

    def test_market_crash_recalculates_and_defers_optional_goals(self) -> None:
        crash = baseline_assumptions(
            name="市场下跌情景",
            reason="2027年初市场下跌55%，按安全底线重算",
            market_events=[{"month": "2027-01", "haircut": 0.55}],
            annual_returns={"A1": 0.01},
            base_plan_id="plan-1",
        )
        plan = self.service.create_plan("c1", {"assumptions": crash})
        self.assertEqual(plan["base_plan_id"], "plan-1")
        # 安全底线在冲击月或固定提领作用下首次被击穿。
        self.assertIsNotNone(plan["summary"]["floor_breach_month"])
        jan = next(r for r in plan["rows"] if r["month"] == "2027-01")
        self.assertAlmostEqual(jan["market_haircut"], jan["start_balance"] * 0.55, places=2)
        # 手术是必办项目，即便击穿底线也在 2027-03 出资。
        coverage = {c["goal_id"]: c for c in plan["summary"]["goal_coverage"]}
        self.assertEqual(coverage["G_SURGERY"]["status"], "funded")
        # 可延后项目应出现延后记录或部分覆盖。
        travel = coverage["G_TRAVEL"]
        self.assertNotEqual(travel["status"], "funded")
        self.assertTrue(travel["first_deferred_month"])

    def test_goal_brought_forward_is_mandatory(self) -> None:
        a = baseline_assumptions(
            name="目标提前情景",
            goal_overrides=[{"goal_id": "G_TRAVEL", "due_month": "2026-12"}],
        )
        plan = self.service.create_plan("c1", {"assumptions": a})
        travel = next(c for c in plan["summary"]["goal_coverage"] if c["goal_id"] == "G_TRAVEL")
        self.assertEqual(travel["status"], "funded")
        self.assertEqual(travel["funded_month"], "2026-12")
        # 客户登记的原始目标不受方案覆盖影响。
        state = self.service._load("c1")
        self.assertEqual(next(g for g in state["goals"] if g["id"] == "G_TRAVEL")["due_month"], "2028-05")

    def test_expected_payout_shifts_over_weekend_and_holiday(self) -> None:
        # 2027-01-02 为周六，应顺延到 2027-01-04（周一）。
        self.service.register_payout("c1", {"id": "P1", "amount": 50_000,
                                            "expected_date": "2027-01-02"})
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        resolution = next(r for r in plan["payout_resolution"] if r["payout_id"] == "P1")
        self.assertEqual(resolution["arrival_date"], "2027-01-04")
        self.assertEqual(resolution["source"], "expected_shifted")
        self.assertEqual(resolution["holiday_version_id"], "HOL_V1")
        row = next(r for r in plan["rows"] if r["month"] == "2027-01")
        self.assertAlmostEqual(row["insurance_inflow"], 50_000, places=2)

    def test_arrived_payout_counts_once_in_horizon(self) -> None:
        self.service.register_payout("c1", {"id": "P1", "amount": 50_000,
                                            "expected_date": "2027-01-02"})
        self.service.mark_payout_arrived("c1", "P1", {"arrival_date": "2027-01-04",
                                                      "holiday_version_id": "HOL_V1"})
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        # 期初余额不得提前吸收规划期内的赔付（避免双重计数）。
        self.assertAlmostEqual(plan["anchor"]["start_total_cny"], 1_200_000, places=2)
        self.assertAlmostEqual(plan["anchor"]["in_horizon_arrived_payout"], 50_000, places=2)
        row = next(r for r in plan["rows"] if r["month"] == "2027-01")
        self.assertAlmostEqual(row["insurance_inflow"], 50_000, places=2)
        resolution = next(r for r in plan["payout_resolution"] if r["payout_id"] == "P1")
        self.assertEqual(resolution["source"], "actual")
        # 实际到账日不可二次修改。
        with self.assertRaises(ServiceError):
            self.service.mark_payout_arrived("c1", "P1", {"arrival_date": "2027-01-05"})

    def test_arrived_payout_before_start_baked_into_anchor(self) -> None:
        self.service.register_payout("c1", {"id": "P0", "amount": 20_000,
                                            "expected_date": "2026-09-16"})
        self.service.mark_payout_arrived("c1", "P0", {"arrival_date": "2026-09-20"})
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        self.assertAlmostEqual(plan["anchor"]["start_total_cny"], 1_220_000, places=2)
        self.assertEqual(sum(r["insurance_inflow"] for r in plan["rows"]), 0.0)

    def test_executed_withdrawal_is_not_overwritten_by_new_tax_assumption(self) -> None:
        # 已执行提领：净 5000，预扣 556（gross 5556），发生在规划起始月。
        self.service.record_withdrawal("c1", {"id": "W1", "date": "2026-10-10",
                                              "net_cny": 5_000, "gross_cny": 5_556})
        self.service.add_tax_version("c1", {"id": "TAX_2026_V2", "year": 2026,
                                            "name": "2026修订", "rate": 0.20})
        a = baseline_assumptions(tax_version_ids={"2026": "TAX_2026_V2"})
        plan = self.service.create_plan("c1", {"assumptions": a})
        oct_row = next(r for r in plan["rows"] if r["month"] == "2026-10")
        self.assertTrue(oct_row["settled"])
        self.assertAlmostEqual(oct_row["net_withdrawal"], 5_000, places=2)
        self.assertAlmostEqual(oct_row["gross_withdrawal"], 5_556, places=2)
        self.assertAlmostEqual(oct_row["tax_withheld"], 556, places=2)
        self.assertEqual(oct_row["funded_goal_ids"], [])
        # 新税率只作用于未结账月份：11月固定提领净额 3000 按 20% gross-up。
        nov_row = next(r for r in plan["rows"] if r["month"] == "2026-11")
        self.assertFalse(nov_row["settled"])
        self.assertAlmostEqual(nov_row["gross_withdrawal"], 3_750, places=2)
        # 快照保留实际采用的版本，且产生版本变更告警。
        tax2026 = next(t for t in plan["versions_snapshot"]["tax"] if t["year"] == 2026)
        self.assertEqual(tax2026["id"], "TAX_2026_V2")
        self.assertTrue(any("原采用税率版本" in w for w in plan["summary"]["warnings"]))

    def test_confirmed_plan_keeps_original_versions_when_rates_change(self) -> None:
        first = self.service.create_plan("c1", {"assumptions": baseline_assumptions(name="首版")})
        self.service.review_plan(first["id"], "confirm", note="通过")
        # 已审结方案保留原采用版本；新方案使用新年税后版本。
        self.service.add_tax_version("c1", {"id": "TAX_2028_NEW", "year": 2028,
                                            "name": "2028新政", "rate": 0.15})
        second = self.service.create_plan(
            "c1",
            {"assumptions": baseline_assumptions(name="税改后方案",
                                                 tax_version_ids={"2028": "TAX_2028_NEW"})},
        )
        stored_first = self.service.get_plan(first["id"])
        tax2028_first = next(t for t in stored_first["versions_snapshot"]["tax"] if t["year"] == 2028)
        self.assertEqual(tax2028_first["id"], "TAX_2028_V1")
        tax2028_second = next(t for t in second["versions_snapshot"]["tax"] if t["year"] == 2028)
        self.assertEqual(tax2028_second["id"], "TAX_2028_NEW")
        # 确认第二版后客户当前采用版本切换，但第一版文件不变。
        self.service.review_plan(second["id"], "confirm")
        state = self.service._load("c1")
        self.assertEqual(state["adopted_tax_versions"]["2028"], "TAX_2028_NEW")
        self.assertEqual(state["approved_plan_id"], second["id"])
        self.assertEqual(
            next(t for t in self.service.get_plan(first["id"])["versions_snapshot"]["tax"]
                 if t["year"] == 2028)["id"],
            "TAX_2028_V1",
        )

    def test_valuation_import_is_idempotent(self) -> None:
        payload = {"as_of": "2026-09-15", "source_ref": "stmt-2026-09",
                   "positions": [{"account_id": "A1", "amount": 1_200_000}]}
        again = self.service.import_valuation("c1", payload)
        self.assertTrue(again["deduplicated"])
        reordered = {"positions": [{"amount": 1_200_000, "account_id": "A1"}],
                     "as_of": "2026-09-15", "source_ref": "stmt-2026-09"}
        self.assertTrue(self.service.import_valuation("c1", reordered)["deduplicated"])
        state = self.service._load("c1")
        self.assertEqual(len(state["valuations"]), 1)
        # 不同 source_ref 视为另一份估值，允许保留。
        other = self.service.import_valuation("c1", {**payload, "source_ref": "manual-correction"})
        self.assertFalse(other["deduplicated"])

    def test_scenario_compare_reports_exhaustion_and_coverage(self) -> None:
        # 80 万余额：保守提领规划期内存续，激进提领提前耗尽。
        build_fixture(self.service, cid="c2", balance=800_000)
        p1 = self.service.create_plan("c2", {"assumptions": baseline_assumptions(
            name="保守提领", fixed_monthly_withdrawal=3_000, annual_returns={"A1": 0.03})})
        p2 = self.service.create_plan("c2", {"assumptions": baseline_assumptions(
            name="激进提领", fixed_monthly_withdrawal=6_000, annual_returns={"A1": 0.03})})
        report = self.service.compare("c2", [p1["id"], p2["id"]])
        by_name = {s["name"]: s for s in report["scenarios"]}
        self.assertIsNone(by_name["保守提领"]["exhaustion_month"])
        self.assertIsNotNone(by_name["激进提领"]["exhaustion_month"])
        self.assertGreater(by_name["保守提领"]["final_balance"], by_name["激进提领"]["final_balance"])
        # 激进情景下靠后的赛事旅行覆盖率应低于保守情景。
        cov1 = {c["goal_id"]: c for c in by_name["保守提领"]["goal_coverage"]}
        cov2 = {c["goal_id"]: c for c in by_name["激进提领"]["goal_coverage"]}
        self.assertGreaterEqual(cov1["G_TRAVEL"]["coverage_pct"], cov2["G_TRAVEL"]["coverage_pct"])

    def test_fixed_withdrawal_conflicts_with_medical_reserve(self) -> None:
        build_fixture(self.service, cid="c3", balance=230_000)
        plan = self.service.create_plan("c3", {"assumptions": baseline_assumptions(
            fixed_monthly_withdrawal=4_000, annual_returns={"A1": 0.0})})
        self.assertIsNotNone(plan["summary"]["floor_breach_month"])
        self.assertIn("安全底线首次在", plan["explanation"])

    def test_client_isolation_portal_sees_only_own_data(self) -> None:
        created = self.service.create_client({"id": "c9", "name": "另一家庭"})
        token_c9 = created["view_token"]
        plan_c1 = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        self.service.review_plan(plan_c1["id"], "confirm")
        portal = self.service.portal_summary(token_c9)
        self.assertEqual(portal["client"]["id"], "c9")
        self.assertIsNone(portal["approved_summary"])
        with self.assertRaises(ServiceError):
            self.service.portal_plan(token_c9, plan_c1["id"])
        with self.assertRaises(ServiceError):
            self.service.get_plan(plan_c1["id"], client_id="c9")
        with self.assertRaises(ServiceError):
            self.service.portal_summary("not-a-real-token")

    def test_portal_only_shows_confirmed_plans(self) -> None:
        state = self.service._load("c1")
        token = state["client"]["view_token"]
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        with self.assertRaises(ServiceError):
            self.service.portal_plan(token, plan["id"])
        self.service.review_plan(plan["id"], "confirm")
        view = self.service.portal_plan(token, plan["id"])
        self.assertEqual(view["status"], "confirmed")
        self.assertIn("explanation", view)

    def test_restart_allows_review_of_pending_plan(self) -> None:
        plan = self.service.create_plan("c1", {"assumptions": baseline_assumptions()})
        # 模拟进程重启：重新打开同一目录。
        restarted = PlanningService(Store(self.tmp.name))
        pending = restarted.list_plans("c1", status="proposed")
        self.assertEqual([p["id"] for p in pending], [plan["id"]])
        confirmed = restarted.review_plan(plan["id"], "confirm", note="重启后继续审核")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["review_note"], "重启后继续审核")
        # 不能重复审核。
        with self.assertRaises(ServiceError):
            restarted.review_plan(plan["id"], "reject")

    def test_engine_rejects_missing_tax_version(self) -> None:
        state = self.service._load("c1")
        state["tax_versions"] = [t for t in state["tax_versions"] if t["year"] != 2030]
        state["adopted_tax_versions"].pop("2030")
        with self.assertRaises(Exception) as ctx:
            build_plan(
                EngineInput(
                    client=state["client"], accounts=state["accounts"], goals=state["goals"],
                    valuations=state["valuations"], withdrawals=state["withdrawals"],
                    payouts=state["insurance_payouts"], tax_versions=state["tax_versions"],
                    holiday_versions=state["holiday_versions"],
                    assumptions=baseline_assumptions(),
                ),
                "plan-x", "2026-09-20T00:00:00",
            )
        self.assertIn("2030", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
