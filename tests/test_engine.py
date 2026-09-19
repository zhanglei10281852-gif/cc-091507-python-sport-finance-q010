from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine import gross_up, monthly_rate, simulate

BASE = {
    "start_month": "2027-01",
    "horizon_months": 12,
    "annual_return": 0.0,
    "fixed_monthly": 1000.0,
    "floor": 5000.0,
    "tax_by_year": {"2027": 0.1},
    "holidays": [],
}


class EngineTest(unittest.TestCase):
    def test_gross_up_and_rate(self) -> None:
        self.assertAlmostEqual(gross_up(900, 0.1), 1000.0, places=6)
        self.assertAlmostEqual((1 + monthly_rate(0.12)) ** 12 - 1, 0.12, places=9)

    def test_floor_blocks_fixed_and_marks_exhaustion(self) -> None:
        # 6000 余额，底线 5000：最多提 ~1000 毛额，净额 900 < 1000
        res = simulate(6000.0, BASE, goals=[], payouts=[], actuals=[])
        self.assertEqual(res["exhausted_month"], "2027-01")
        self.assertGreaterEqual(res["monthly"][0]["ending"], 5000.0 - 1e-6)
        self.assertGreater(res["monthly"][0]["fixed_shortfall"], 0.0)

    def test_floor_defers_goal_until_funds_allow(self) -> None:
        assumptions = dict(BASE, fixed_monthly=0.0)
        goals = [{"id": "g1", "type": "fitness", "name": "私教",
                  "cost": 2000.0, "priority": 1, "due_month": "2027-01"}]
        res = simulate(6000.0, assumptions, goals, [], [])
        # 6000 - 2000 = 4000 < 底线 5000，目标必须顺延；无赔付则永远无法覆盖
        self.assertEqual(res["goals"]["g1"]["status"], "deferred")
        self.assertEqual(res["deferred"][0]["goal_id"], "g1")
        # 赔付到账后可支付
        payouts = [{"id": "p1", "amount": 2000.0, "expected_date": "2027-02-01"}]
        res2 = simulate(6000.0, assumptions, goals, payouts, [])
        self.assertEqual(res2["goals"]["g1"]["status"], "funded_late")
        self.assertEqual(res2["goals"]["g1"]["funded_month"], "2027-02")

    def test_holiday_arrival_postponed_into_next_month(self) -> None:
        # 2027-05-01 为周六；到账顺延至 2027-05-04，仍在 5 月。
        # 构造月末周六 2027-07-31，顺延到 8 月，验证计入月份。
        assumptions = dict(BASE, fixed_monthly=0.0,
                           tax_by_year={"2027": 0.0},
                           holidays=["2027-08-01", "2027-08-02"])
        payouts = [{"id": "p1", "amount": 10000.0,
                    "expected_date": "2027-07-31"}]  # 周六
        goals = [{"id": "g1", "type": "travel", "name": "赛事",
                  "cost": 10000.0, "priority": 1, "due_month": "2027-07"}]
        res = simulate(5000.0, assumptions, goals, payouts, [])
        self.assertEqual(res["goals"]["g1"]["funded_month"], "2027-08")
        self.assertEqual(res["goals"]["g1"]["status"], "funded_late")

    def test_market_down_advances_exhaustion(self) -> None:
        assumptions = dict(BASE, annual_return=0.12, fixed_monthly=1000.0)
        calm = simulate(20000.0, assumptions, [], [], [])
        shocked = dict(assumptions)
        shocked["monthly_shocks"] = {"2027-02": -0.30}
        crash = simulate(20000.0, shocked, [], [], [])
        self.assertIsNone(calm["exhausted_month"])
        self.assertIsNotNone(crash["exhausted_month"])

    def test_goal_advance_causes_deferral(self) -> None:
        assumptions = dict(BASE, fixed_monthly=0.0, floor=5000.0,
                           tax_by_year={"2027": 0.0})
        goals = [{"id": "g1", "type": "rehabilitation", "name": "康复",
                  "cost": 5000.0, "priority": 1, "due_month": "2027-12"}]
        payouts = [{"id": "p1", "amount": 5000.0,
                    "expected_date": "2027-11-01"}]
        on_time = simulate(5000.0, assumptions, goals, payouts, [])
        # 医疗储备（底线）5000 不可动用；资金 11 月赔付才到位，
        # 目标提前到 6 月则无法按时覆盖
        early_goals = [dict(goals[0], due_month="2027-06")]
        advanced = simulate(5000.0, assumptions, early_goals, payouts, [])
        self.assertEqual(on_time["goals"]["g1"]["status"], "funded")
        self.assertEqual(advanced["goals"]["g1"]["status"], "funded_late")
        self.assertEqual(advanced["goals"]["g1"]["funded_month"], "2027-11")

    def test_actuals_lock_past_months(self) -> None:
        # 1 月实际固定提领净额仅 500（手术占用现金）；新假设不得改写该事实
        actuals = [{
            "month": "2027-01", "kind": "fixed", "funded_net": 500.0,
            "gross": 500.0, "delta_balance": -500.0, "tax_rate": 0.0,
        }]
        assumptions = dict(BASE, as_of_month="2027-01")
        res = simulate(100000.0, assumptions, [], [], actuals)
        jan = res["monthly"][0]
        self.assertTrue(jan["locked"])
        self.assertEqual(jan["fixed_net"], 500.0)
        self.assertEqual(jan["fixed_shortfall"], 500.0)
        # 2 月起恢复按新假设投影（10% 税率 gross-up）
        feb = res["monthly"][1]
        self.assertFalse(feb["locked"])
        self.assertAlmostEqual(feb["fixed_net"], 1000.0, places=2)

    def test_partial_goal_funding_then_completion(self) -> None:
        # 康复分期：1 月实付 3000，2 月投影补 2000
        actuals = [{
            "month": "2027-01", "kind": "goal", "goal_id": "g1",
            "funded_net": 3000.0, "gross": 3000.0,
            "delta_balance": -3000.0, "tax_rate": 0.0,
        }]
        assumptions = dict(BASE, fixed_monthly=0.0, floor=0.0,
                           tax_by_year={"2027": 0.0},
                           as_of_month="2027-01")
        goals = [{"id": "g1", "type": "rehabilitation", "name": "术后康复",
                  "cost": 5000.0, "priority": 1, "due_month": "2027-03"}]
        res = simulate(10000.0, assumptions, goals, [], actuals)
        self.assertEqual(res["goals"]["g1"]["funded_amount"], 5000.0)
        self.assertEqual(res["goals"]["g1"]["status"], "funded")
        # 剩余 2000 在目标到期当月（3 月）按新假设补足
        self.assertEqual(res["goals"]["g1"]["funded_month"], "2027-03")


if __name__ == "__main__":
    unittest.main()
