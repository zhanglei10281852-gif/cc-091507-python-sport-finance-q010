"""月度现金流投影引擎（纯函数）。

约定：
- 所有月份用 ``date``（当月 1 号）表示。
- ``as_of_month``（含）之前为已锁定区间，按实际执行数据回放，
  新假设不得覆盖已执行提领；之后才按假设投影。
- 安全底线 floor 为硬约束：任何提领后余额不得低于 floor，
  不足以整额支付的目标顺延到后续月份重试。
- 目标支持分批筹资（如康复费用分期）：累计实付达到成本才算完成。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from domain import (
    GOAL_DEFERRED,
    GOAL_FUNDED,
    GOAL_FUNDED_LATE,
    MONEY_EPS,
    add_months,
    arrival_month,
    month_str,
    parse_month,
)


def monthly_rate(annual_return: float) -> float:
    return (1.0 + annual_return) ** (1.0 / 12.0) - 1.0


def _tax_rate(tax_by_year: dict[str, float], year: int) -> float:
    return float(tax_by_year.get(str(year), tax_by_year.get(year, 0.0)))


def gross_up(net: float, rate: float) -> float:
    if rate >= 1 - MONEY_EPS:
        return net / MONEY_EPS
    return net / (1.0 - rate)


def simulate(
    initial_balance: float,
    assumptions: dict[str, Any],
    goals: list[dict[str, Any]],
    payouts: list[dict[str, Any]],
    actuals: list[dict[str, Any]],
) -> dict[str, Any]:
    """执行滚动模拟。

    assumptions 键：start_month, horizon_months, annual_return,
    monthly_shocks({"YYYY-MM": 当月收益率}), fixed_monthly, floor,
    tax_by_year({"YYYY": 税率}), holidays(list of ISO date),
    holidays_version, as_of_month(可空)。

    goals: {id, type, name, cost, priority, due_month(YYYY-MM), deferrable}
    payouts: {id, amount, expected_date(YYYY-MM-DD)}
    actuals: {month(YYYY-MM), kind(fixed/goal/payout), goal_id, funded_net,
              delta_balance(有符号), gross, tax_rate, arrival_date,
              end_balance(可选，锁定月月末锚点)}
    """
    start = parse_month(assumptions["start_month"])
    horizon = int(assumptions["horizon_months"])
    floor = float(assumptions.get("floor", 0.0))
    fixed_net = float(assumptions.get("fixed_monthly", 0.0))
    base_r = monthly_rate(float(assumptions.get("annual_return", 0.0)))
    shocks = assumptions.get("monthly_shocks", {}) or {}
    tax_by_year = assumptions.get("tax_by_year", {}) or {}
    holidays = set(assumptions.get("holidays", []) or [])
    as_of = assumptions.get("as_of_month")

    goal_by_id: dict[str, dict[str, Any]] = {}
    for g in goals:
        gg = deepcopy(g)
        gg["due"] = gg["due_month"]
        goal_by_id[gg["id"]] = gg

    payout_arrivals: dict[str, str] = {}
    for p in payouts:
        _, arr = arrival_month(date.fromisoformat(p["expected_date"]), holidays)
        payout_arrivals[p["id"]] = arr

    actuals_by_month: dict[str, list[dict[str, Any]]] = {}
    for a in actuals:
        actuals_by_month.setdefault(a["month"], []).append(a)

    funded_so_far: dict[str, float] = {}       # goal_id -> 累计净额
    first_fund_month: dict[str, str] = {}      # goal_id -> 首次筹资月
    completed: dict[str, dict[str, Any]] = {}  # goal_id -> 完成记录
    balance = float(initial_balance)
    rows: list[dict[str, Any]] = []
    fixed_shortfalls: list[dict[str, Any]] = []
    pending: list[str] = []
    exhausted_month: str | None = None

    for i in range(horizon):
        m = add_months(start, i)
        ms = month_str(m)
        locked = as_of is not None and ms <= as_of
        opening = balance

        # 1) 市场收益（锁定月若给了 end_balance 锚点则以后者为准）
        r = float(shocks.get(ms, base_r))
        return_credit = opening * r
        balance = opening + return_credit

        payouts_in = 0.0
        fixed_gross = fixed_net_paid = 0.0
        shortfall = 0.0
        goal_outflows: list[dict[str, Any]] = []

        if locked:
            # 2) 锁定区间：完全按实际执行回放，新假设不可覆盖
            for a in actuals_by_month.get(ms, []):
                kind = a["kind"]
                if "delta_balance" in a:
                    balance += float(a["delta_balance"])
                if kind == "payout":
                    payouts_in += float(a.get("funded_net", a.get("delta_balance", 0.0)))
                elif kind == "fixed":
                    net = float(a.get("funded_net", 0.0))
                    fixed_net_paid += net
                    fixed_gross += abs(float(a.get("delta_balance", 0.0)))
                    if net + MONEY_EPS < fixed_net:
                        gap = fixed_net - net
                        shortfall += gap
                        fixed_shortfalls.append(
                            {"month": ms, "shortfall": round(gap, 2)})
                elif kind == "goal" and a.get("goal_id"):
                    gid = a["goal_id"]
                    net = float(a.get("funded_net", 0.0))
                    gross = abs(float(a.get("delta_balance", 0.0)))
                    fixed_gross += gross
                    goal_outflows.append(
                        {"goal_id": gid, "gross": round(gross, 2), "net": round(net, 2)})
                    g = goal_by_id.get(gid)
                    if g and net > MONEY_EPS:
                        funded_so_far[gid] = funded_so_far.get(gid, 0.0) + net
                        first_fund_month.setdefault(gid, ms)
                        if (gid not in completed
                                and funded_so_far[gid] + MONEY_EPS >= float(g["cost"])):
                            completed[gid] = {
                                "goal_id": gid,
                                "funded_month": ms,
                                "funded_amount": round(float(g["cost"]), 2),
                                "status": (GOAL_FUNDED if ms <= g["due"]
                                           else GOAL_FUNDED_LATE),
                                "source": "actual",
                            }
                if a.get("end_balance") is not None:
                    balance = float(a["end_balance"])
        else:
            # 2) 赔付到账（节假日按本情景采用版本推算）
            for p in payouts:
                if payout_arrivals.get(p["id"]) == ms:
                    balance += float(p["amount"])
                    payouts_in += float(p["amount"])

            # 3) 固定提领（按到账年的已采用税率 gross-up）；底线为硬约束
            if fixed_net > MONEY_EPS:
                rate = _tax_rate(tax_by_year, m.year)
                wanted_gross = gross_up(fixed_net, rate)
                available = max(0.0, balance - floor)
                gross = min(wanted_gross, available)
                net = gross * (1.0 - rate)
                balance -= gross
                fixed_gross = gross
                fixed_net_paid = net
                if gross + MONEY_EPS < wanted_gross:
                    shortfall = fixed_net - net
                    fixed_shortfalls.append(
                        {"month": ms, "shortfall": round(shortfall, 2)})

            # 4) 目标提领：已到期（含锁定区间内到期未完成）的目标入队，
            #    按优先级、成本排序；仅对未完成部分筹资，余额不足则顺延
            for g in goal_by_id.values():
                if (g["due"] <= ms and g["id"] not in completed
                        and g["id"] not in pending):
                    pending.append(g["id"])
            rate = _tax_rate(tax_by_year, m.year)
            still_pending: list[str] = []
            ordered = sorted(
                pending,
                key=lambda gid: (
                    int(goal_by_id[gid].get("priority", 100)),
                    float(goal_by_id[gid]["cost"]),
                ),
            )
            for gid in ordered:
                g = goal_by_id[gid]
                remaining = float(g["cost"]) - funded_so_far.get(gid, 0.0)
                if remaining <= MONEY_EPS:
                    continue
                wanted_gross = gross_up(remaining, rate)
                if balance - wanted_gross >= floor - MONEY_EPS:
                    balance -= wanted_gross
                    goal_outflows.append(
                        {"goal_id": gid, "gross": round(wanted_gross, 2),
                         "net": round(remaining, 2)})
                    funded_so_far[gid] = funded_so_far.get(gid, 0.0) + remaining
                    first_fund_month.setdefault(gid, ms)
                    if funded_so_far[gid] + MONEY_EPS >= float(g["cost"]):
                        completed[gid] = {
                            "goal_id": gid,
                            "funded_month": ms,
                            "funded_amount": round(float(g["cost"]), 2),
                            "status": (GOAL_FUNDED if ms <= g["due"]
                                       else GOAL_FUNDED_LATE),
                            "source": "projected",
                        }
                else:
                    still_pending.append(gid)
            pending = still_pending

        if exhausted_month is None and shortfall > MONEY_EPS:
            # 固定提领首次无法在不突破安全底线的情况下足额支付
            exhausted_month = ms

        rows.append(
            {
                "month": ms,
                "locked": locked,
                "opening": round(opening, 2),
                "market_return": round(return_credit, 2),
                "payouts_in": round(payouts_in, 2),
                "fixed_gross": round(fixed_gross, 2),
                "fixed_net": round(fixed_net_paid, 2),
                "fixed_shortfall": round(shortfall, 2),
                "goal_outflows": goal_outflows,
                "ending": round(balance, 2),
                "floor": round(floor, 2),
            }
        )

    # 汇总
    goal_results: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    total_funded = 0.0
    deferred: list[dict[str, Any]] = []
    for g in goal_by_id.values():
        cost = float(g["cost"])
        total_cost += cost
        c = completed.get(g["id"])
        funded_amount = min(funded_so_far.get(g["id"], 0.0), cost)
        total_funded += funded_amount
        if c:
            status = c["status"]
            funded_month = c["funded_month"]
        else:
            status = GOAL_DEFERRED
            funded_month = None
            deferred.append(
                {"goal_id": g["id"], "name": g.get("name", g["id"]),
                 "type": g.get("type"), "due_month": g["due"],
                 "cost": round(cost, 2), "priority": g.get("priority", 100),
                 "funded_so_far": round(funded_amount, 2)})
        goal_results[g["id"]] = {
            "goal_id": g["id"],
            "name": g.get("name", g["id"]),
            "type": g.get("type"),
            "due_month": g["due"],
            "cost": round(cost, 2),
            "status": status,
            "funded_month": funded_month,
            "funded_amount": round(funded_amount, 2),
            "coverage": round(funded_amount / cost, 6) if cost > MONEY_EPS else 1.0,
        }

    coverage_rate = round(total_funded / total_cost, 6) if total_cost > MONEY_EPS else 1.0

    return {
        "monthly": rows,
        "goals": goal_results,
        "deferred": deferred,
        "coverage_rate": coverage_rate,
        "exhausted_month": exhausted_month,
        "fixed_shortfalls": fixed_shortfalls,
        "final_balance": round(balance, 2),
        "holidays_version": assumptions.get("holidays_version"),
    }
