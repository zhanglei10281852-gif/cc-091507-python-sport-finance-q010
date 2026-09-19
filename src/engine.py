"""现金流规划引擎。

月度网格模型，关键规则：

1. 以最新估值快照为锚点；估值日之后、规划起始月之前的已执行提领/已到账赔付
   烘焙进期初余额，起始月及之后的进入月度行，两处只记一次、绝不重复。
2. 已结账月份（含已执行提领）只回放事实：按记录的原始 gross/net 出账、按实际
   到账赔付入账，不套用假设冲击、不虚构收益（真实行情由更新的估值快照带入）。
3. 未结账月份按账户权重混合预期收益，支持市场冲击（月初按比例折减）；提领按
   当年采用税率版本 gross-up：gross = net / (1 - rate)。
4. 安全底线 = emergency_reserve 目标合计（或显式 floor）。固定提领与必办目标
   属于强制支出，余额不足时允许击穿底线并标记；可延后目标按优先级出资，不足则
   滚动延后。
5. 税率/节假日版本只追加，方案生成时把实际采用版本完整快照；后续方案可选用
   新版本并附变更告警，已审结方案的快照永不改写。
6. 输出耗尽月份、首次击穿底线月份、每个目标覆盖率与延后清单及中文解释。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from domain import (
    EMERGENCY_RESERVE,
    MONEY_EPS,
    add_months,
    money,
    month_index,
    month_of_date,
    next_business_day,
    parse_date,
    parse_month,
    year_of_month,
)


class PlanError(ValueError):
    """方案输入或版本不满足建模条件。"""


# ---------------------------------------------------------------- 数据装配


@dataclass
class EngineInput:
    client: dict[str, Any]
    accounts: list[dict[str, Any]]
    goals: list[dict[str, Any]]
    valuations: list[dict[str, Any]]
    withdrawals: list[dict[str, Any]]
    payouts: list[dict[str, Any]]
    tax_versions: list[dict[str, Any]]
    holiday_versions: list[dict[str, Any]]
    assumptions: dict[str, Any]
    adopted_tax_versions: dict[str, str] = field(default_factory=dict)
    adopted_holiday_version_id: str | None = None
    base_plan: dict[str, Any] | None = None


def _convert(amount: float, currency: str, fx: dict[str, float]) -> float:
    if currency not in fx:
        raise PlanError(f"假设中缺少币种 {currency} 的 fx_rates")
    return money(amount * fx[currency])


def _latest_valuation(input: EngineInput) -> dict[str, Any]:
    if not input.valuations:
        raise PlanError("缺少账户估值快照，无法建立规划锚点")
    return max(input.valuations, key=lambda v: v["as_of"])


def _date_cutoff(start_month: str) -> date:
    year, month = parse_month(start_month)
    return date(year, month, 1)


def _anchor_balances(input: EngineInput, fx: dict[str, float], start_month: str) -> dict[str, Any]:
    val = _latest_valuation(input)
    per_account: dict[str, float] = {}
    for pos in val["positions"]:
        account = _find_account(input, pos["account_id"])
        per_account[pos["account_id"]] = money(
            per_account.get(pos["account_id"], 0.0) + _convert(pos["amount"], account["currency"], fx)
        )

    # 估值余额已反映估值日（含）之前的一切；只吸收估值日之后、规划起始月之前的事实。
    # 起始月及之后的事实进入月度行，与计划行为分开，绝不重复计算。
    cutoff = _date_cutoff(start_month)
    before_outflows = after_outflows = 0.0
    executed_by_month: dict[str, list[dict[str, float]]] = {}
    for w in input.withdrawals:
        wdate = parse_date(w["date"])
        if parse_date(val["as_of"]) < wdate < cutoff:
            before_outflows = money(before_outflows + w["gross_cny"])
        elif wdate >= cutoff:
            executed_by_month.setdefault(month_of_date(wdate), []).append(
                {"net_cny": money(w["net_cny"]), "gross_cny": money(w["gross_cny"])}
            )
            after_outflows = money(after_outflows + w["gross_cny"])
    before_inflows = after_inflows = 0.0
    arrived_payouts: list[str] = []
    for p in input.payouts:
        if p["status"] != "arrived":
            continue
        amount_cny = _convert(p["amount"], p["currency"], fx)
        arrived_payouts.append(p["id"])
        adate = parse_date(p["arrival_date"])
        if parse_date(val["as_of"]) < adate < cutoff:
            before_inflows = money(before_inflows + amount_cny)
        elif adate >= cutoff:
            after_inflows = money(after_inflows + amount_cny)
    total = money(sum(per_account.values()) - before_outflows + before_inflows)
    return {
        "valuation_id": val["id"],
        "as_of": val["as_of"],
        "month": month_of_date(parse_date(val["as_of"])),
        "projection_start_month": start_month,
        "per_account_cny": {k: money(v) for k, v in per_account.items()},
        "pre_start_executed_withdrawal_gross": before_outflows,
        "pre_start_arrived_payout": before_inflows,
        "in_horizon_executed_withdrawal_gross": after_outflows,
        "in_horizon_arrived_payout": after_inflows,
        "arrived_payout_ids": arrived_payouts,
        "executed_by_month": executed_by_month,
        "start_total_cny": total,
    }


def _find_account(input: EngineInput, account_id: str) -> dict[str, Any]:
    for acc in input.accounts:
        if acc["id"] == account_id:
            return acc
    raise PlanError(f"估值引用了未知账户 {account_id}")


# ---------------------------------------------------------------- 版本解析


def resolve_tax_versions(input: EngineInput, projection_years: set[int]) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """返回 {year: 实际采用版本}。

    选择顺序：方案显式指定 > 客户已采用版本 > 标记 locked 的版本 > 该年度最新登记版本。
    显式指定与已采用版本不同时产生告警；已审结方案的快照永不改写。
    """
    warnings: list[str] = []
    overrides = input.assumptions.get("tax_version_ids") or {}
    resolved: dict[int, dict[str, Any]] = {}
    for year in sorted(projection_years):
        by_year = [t for t in input.tax_versions if t["year"] == year]
        adopted_id = input.adopted_tax_versions.get(str(year)) or input.adopted_tax_versions.get(year)
        adopted = next((t for t in by_year if t["id"] == adopted_id), None)
        if adopted is None:
            locked = [t for t in by_year if t.get("locked")]
            adopted = locked[-1] if locked else (by_year[-1] if by_year else None)
        override_id = overrides.get(str(year)) or overrides.get(year)
        chosen = adopted
        if override_id is not None:
            candidate = next((t for t in by_year if t["id"] == override_id), None)
            if candidate is None:
                raise PlanError(f"年度 {year} 指定的税率版本 {override_id} 不存在")
            if adopted is not None and candidate["id"] != adopted["id"]:
                warnings.append(
                    f"年度 {year} 原采用税率版本 {adopted['id']}（{adopted['name']}，{adopted['rate']:.1%}），"
                    f"本方案改用 {candidate['id']}（{candidate['name']}，{candidate['rate']:.1%}）；"
                    f"已审结方案继续保留原版本"
                )
            chosen = candidate
        if chosen is None:
            raise PlanError(f"年度 {year} 缺少已采用的税率版本，无法计算提领税金")
        resolved[year] = {"id": chosen["id"], "name": chosen["name"], "rate": chosen["rate"]}
    return resolved, warnings


def resolve_holiday_version(input: EngineInput) -> tuple[dict[str, Any], list[str]]:
    """返回采用的节假日版本与告警（客户已采用版本优先，版本表只追加）。"""
    warnings: list[str] = []
    versions = input.holiday_versions
    wanted = input.assumptions.get("holiday_version_id")
    adopted = next((h for h in versions if h["id"] == input.adopted_holiday_version_id), None)
    if adopted is None:
        locked = [h for h in versions if h.get("locked")]
        adopted = locked[-1] if locked else (versions[-1] if versions else None)
    if wanted:
        hv = next((h for h in versions if h["id"] == wanted), None)
        if hv is None:
            raise PlanError(f"节假日版本 {wanted} 不存在")
        if adopted is not None and hv["id"] != adopted["id"]:
            warnings.append(
                f"原采用节假日版本 {adopted['id']}（{adopted['name']}），本方案改用 {hv['id']}（{hv['name']}）；"
                f"已到账赔付的到账日与已审结方案不受影响"
            )
        adopted = hv
    if adopted is None:
        raise PlanError("缺少节假日版本，无法确定赔付到账日（周末/节假日顺延规则）")
    return adopted, warnings


# ---------------------------------------------------------------- 主模拟


@dataclass
class _GoalState:
    goal: dict[str, Any]
    funded: float = 0.0
    funded_month: str | None = None
    deferred_months: list[str] = field(default_factory=list)
    defer_reason: str | None = None


def build_plan(input: EngineInput, plan_id: str, created_at: str) -> dict[str, Any]:
    assumptions = deepcopy(input.assumptions)
    fx = assumptions.get("fx_rates") or {"CNY": 1.0}
    if not isinstance(fx, dict) or "CNY" not in fx:
        raise PlanError("fx_rates 必须是包含 CNY 的汇率表")
    fx = {k: float(v) for k, v in fx.items()}
    fx["CNY"] = 1.0

    horizon = int(assumptions.get("horizon_months", 120))
    if horizon <= 0 or horizon > 600:
        raise PlanError("horizon_months 必须在 1..600 之间")

    val = _latest_valuation(input)
    valuation_month = month_of_date(parse_date(val["as_of"]))
    start_month = assumptions.get("start_month") or add_months(valuation_month, 1)
    parse_month(start_month)
    anchor = _anchor_balances(input, fx, start_month)
    months = [add_months(start_month, i) for i in range(horizon)]

    tax_versions, warnings = resolve_tax_versions(input, {year_of_month(m) for m in months})
    holiday_version, holiday_warnings = resolve_holiday_version(input)
    warnings.extend(holiday_warnings)
    holidays = frozenset(parse_date(d) for d in holiday_version["holidays"])

    # 目标提前/推后覆盖（仅作用于本方案，不改写客户登记的目标）。
    due_overrides = {o["goal_id"]: o["due_month"] for o in assumptions.get("goal_overrides", [])}
    market_events = {e["month"]: float(e.get("haircut", 0.0)) for e in assumptions.get("market_events", [])}

    goal_states: dict[str, _GoalState] = {}
    for g in input.goals:
        due = due_overrides.get(g["id"], g["due_month"])
        merged = deepcopy(g)
        merged["due_month"] = due
        merged["amount_cny"] = _convert(g["amount"], g["currency"], fx)
        goal_states[g["id"]] = _GoalState(goal=merged)

    floor_value = assumptions.get("emergency_floor")
    if floor_value is None:
        floor_value = sum(
            gs.goal["amount_cny"] for gs in goal_states.values() if gs.goal["type"] == EMERGENCY_RESERVE
        )
    floor_value = money(floor_value)
    fixed_net = money(assumptions.get("fixed_monthly_withdrawal", 0.0))

    # 赔付到账月份：已到账用实际日期（不可变）；未到账用期望日按本方案节假日版本顺延。
    actual_payout_inflows: dict[str, float] = {}
    expected_payout_inflows: dict[str, float] = {}
    payout_resolution: list[dict[str, Any]] = []
    for p in input.payouts:
        amount_cny = _convert(p["amount"], p["currency"], fx)
        if p["status"] == "arrived":
            arrival = p["arrival_date"]
            source = "actual"
            hv_id = p.get("arrival_holiday_version_id")
        else:
            expected = parse_date(p["expected_date"])
            arrival = next_business_day(expected, holidays).isoformat()
            source = "expected_shifted"
            hv_id = holiday_version["id"]
        payout_resolution.append(
            {"payout_id": p["id"], "arrival_date": arrival, "source": source, "holiday_version_id": hv_id}
        )
        arrival_month = month_of_date(parse_date(arrival))
        if source != "actual" and month_index(arrival_month) < month_index(start_month):
            warnings.append(
                f"赔付 {p['id']} 仍为预期状态，但推算到账日 {arrival} 已早于规划起始月 {start_month}，"
                f"请确认是否漏记实际到账"
            )
        if source == "actual":
            actual_payout_inflows[arrival_month] = money(
                actual_payout_inflows.get(arrival_month, 0.0) + amount_cny
            )
        else:
            expected_payout_inflows[arrival_month] = money(
                expected_payout_inflows.get(arrival_month, 0.0) + amount_cny
            )

    # 账户权重混合年化收益（提领按比例分摊，权重恒定）。
    weights = anchor["per_account_cny"]
    total_w = sum(weights.values())
    returns_override = assumptions.get("annual_returns") or {}
    blended_annual = 0.0
    if total_w > 0:
        for acc in input.accounts:
            w = weights.get(acc["id"], 0.0)
            if w <= 0:
                continue
            rate = float(returns_override.get(acc["id"], acc.get("expected_return", 0.0)))
            blended_annual += (w / total_w) * rate
    monthly_return = (1.0 + blended_annual) ** (1.0 / 12.0) - 1.0

    # 规划期内若混入已执行事实（用户把 start_month 设在已发生月份），
    # 这些月份按事实出账、不再叠加计划提领，并给出告警提醒起始月应选首个未结账月。
    executed_by_month = anchor["executed_by_month"]
    settled_months = set(executed_by_month)
    if settled_months:
        warnings.append(
            "规划区间包含已执行提领的月份（" + "、".join(sorted(settled_months)) +
            "），这些月份按实际记录出账，固定提领与目标出资只作用于其后的未结账月份"
        )

    bal = anchor["start_total_cny"]
    rows: list[dict[str, Any]] = []
    exhaustion_month: str | None = None
    floor_breach_month: str | None = None

    for month in months:
        row_start = bal
        settled = month in settled_months
        haircut = market_events.get(month, 0.0) if not settled else 0.0
        if haircut:
            bal = money(bal * (1.0 - haircut))
        # 已结账月份的真实行情只能由更新的估值快照带入，模型不虚构收益。
        ret = money(bal * monthly_return) if not settled else 0.0
        bal = money(bal + ret)
        inflow = money(
            actual_payout_inflows.get(month, 0.0)
            + (0.0 if settled else expected_payout_inflows.get(month, 0.0))
        )
        bal = money(bal + inflow)

        rate = float(tax_versions[year_of_month(month)]["rate"])

        def gross_up(net: float) -> float:
            return money(net / (1.0 - rate)) if rate < 1 else money(net)

        funded_now: list[str] = []
        deferred_now: list[str] = []

        if settled:
            # 已结账月份：只回放不可变事实，税额采用记录里的原始 gross-net 差额。
            actuals = executed_by_month[month]
            net_total = money(sum(a["net_cny"] for a in actuals))
            gross_total = money(sum(a["gross_cny"] for a in actuals))
            tax_total = money(gross_total - net_total)
            bal = money(bal - gross_total)
        else:
            net_total = 0.0

            # 1) 固定提领：强制支出，即使击穿安全底线也执行（这正是与医疗储备的冲突点）。
            if fixed_net > 0:
                net_total = money(net_total + fixed_net)
                bal = money(bal - gross_up(fixed_net))

            # 2) 到期目标：必办优先（医疗/康复），可延后按优先级。
            active = [
                gs
                for gs in goal_states.values()
                if gs.funded + MONEY_EPS < gs.goal["amount_cny"]
                and month_index(gs.goal["due_month"]) <= month_index(month)
                and gs.goal["type"] != EMERGENCY_RESERVE
            ]
            mandatory = sorted((gs for gs in active if not gs.goal["deferrable"]), key=lambda g: g.goal["priority"])
            deferrable = sorted((gs for gs in active if gs.goal["deferrable"]), key=lambda g: g.goal["priority"])

            def try_fund(gs: _GoalState, forced: bool) -> bool:
                nonlocal bal, net_total
                need = money(gs.goal["amount_cny"] - gs.funded)
                gross_need = gross_up(need)
                if not forced:
                    if money(bal - gross_need) < floor_value - MONEY_EPS:
                        return False
                    gross_paid = gross_need
                else:
                    # 强制出资不被安全底线阻挡，但不得无中生有：现金不足时按可承担 gross 部分出资。
                    gross_paid = money(min(gross_need, max(bal, 0.0)))
                net_paid = money(gross_paid * (1.0 - rate))
                if net_paid <= MONEY_EPS:
                    return False
                bal = money(bal - gross_paid)
                net_total = money(net_total + net_paid)
                gs.funded = money(gs.funded + net_paid)
                if gs.funded + MONEY_EPS >= gs.goal["amount_cny"]:
                    gs.funded_month = month
                    funded_now.append(gs.goal["id"])
                return True

            for gs in mandatory:
                try_fund(gs, forced=True)
            for gs in deferrable:
                if not try_fund(gs, forced=False):
                    gs.deferred_months.append(month)
                    if gs.defer_reason is None:
                        gs.defer_reason = "市场下跌导致安全底线不足" if haircut else "安全底线（医疗储备）不足"
                    deferred_now.append(gs.goal["id"])

            gross_total = gross_up(net_total)
            tax_total = money(gross_total - net_total)

        end_bal = bal
        breach = end_bal < floor_value - MONEY_EPS
        exhausted = end_bal < -MONEY_EPS
        if breach and floor_breach_month is None:
            floor_breach_month = month
        if exhausted and exhaustion_month is None:
            exhaustion_month = month

        rows.append(
            {
                "month": month,
                "settled": settled,
                "start_balance": row_start,
                "market_haircut": money(row_start - money(row_start * (1.0 - haircut))) if haircut else 0.0,
                "investment_return": ret,
                "insurance_inflow": inflow,
                "net_withdrawal": money(net_total),
                "tax_withheld": tax_total,
                "gross_withdrawal": gross_total,
                "funded_goal_ids": funded_now,
                "deferred_goal_ids": deferred_now,
                "end_balance": end_bal,
                "safety_floor": floor_value,
                "floor_breach": breach,
                "exhausted": exhausted,
            }
        )
        if exhausted:
            break

    coverage = _coverage(goal_states, months, exhaustion_month)
    deferred_goals = [c for c in coverage if c["status"] != "funded"]

    plan = {
        "id": plan_id,
        "client_id": input.client["id"],
        "name": assumptions.get("name", plan_id),
        "status": "proposed",
        "reason": assumptions.get("reason"),
        "base_plan_id": assumptions.get("base_plan_id"),
        "created_at": created_at,
        "assumptions_snapshot": {
            "plan_currency": "CNY",
            "fx_rates": fx,
            "start_month": start_month,
            "horizon_months": horizon,
            "fixed_monthly_withdrawal": fixed_net,
            "annual_returns": {a["id"]: float(returns_override.get(a["id"], a.get("expected_return", 0.0))) for a in input.accounts},
            "market_events": assumptions.get("market_events", []),
            "goal_overrides": assumptions.get("goal_overrides", []),
            "emergency_floor": floor_value,
        },
        "versions_snapshot": {
            "tax": [
                {"year": y, **tv} for y, tv in sorted(tax_versions.items())
            ],
            "holiday": {
                "id": holiday_version["id"],
                "name": holiday_version["name"],
                "holidays": list(holiday_version["holidays"]),
            },
        },
        "payout_resolution": payout_resolution,
        "anchor": anchor,
        "rows": rows,
        "summary": {
            "exhaustion_month": exhaustion_month,
            "floor_breach_month": floor_breach_month,
            "final_month": rows[-1]["month"] if rows else None,
            "final_balance": rows[-1]["end_balance"] if rows else anchor["start_total_cny"],
            "goal_coverage": coverage,
            "deferred_goals": deferred_goals,
            "warnings": warnings,
        },
    }
    plan["explanation"] = explain(plan)
    return plan


def _coverage(goal_states: dict[str, _GoalState], months: list[str], exhaustion_month: str | None) -> list[dict[str, Any]]:
    result = []
    for gs in goal_states.values():
        g = gs.goal
        if g["type"] == EMERGENCY_RESERVE:
            continue
        amount = g["amount_cny"]
        rate = money(min(gs.funded, amount) / amount * 100.0) if amount else 100.0
        if gs.funded + MONEY_EPS >= amount:
            status = "funded"
        elif gs.funded > MONEY_EPS and exhaustion_month is not None:
            status = "partially_funded_then_exhausted"
        elif gs.funded > MONEY_EPS:
            status = "partially_funded"
        elif exhaustion_month is not None:
            status = "unfunded_after_exhaustion"
        elif gs.deferred_months:
            status = "deferred_beyond_horizon"
        else:
            status = "unfunded"
        result.append(
            {
                "goal_id": g["id"],
                "name": g["name"],
                "type": g["type"],
                "priority": g["priority"],
                "deferrable": g["deferrable"],
                "due_month": g["due_month"],
                "amount_cny": amount,
                "funded_amount_cny": money(min(gs.funded, amount)),
                "coverage_pct": rate,
                "status": status,
                "funded_month": gs.funded_month,
                "first_deferred_month": gs.deferred_months[0] if gs.deferred_months else None,
                "reason": gs.defer_reason,
            }
        )
    return sorted(result, key=lambda c: (c["priority"], c["due_month"]))


# ---------------------------------------------------------------- 解释文本


_STATUS_TEXT = {
    "funded": "已覆盖",
    "partially_funded": "部分覆盖",
    "partially_funded_then_exhausted": "部分覆盖后资金耗尽",
    "deferred_beyond_horizon": "延后至规划期外",
    "unfunded_after_exhaustion": "资金耗尽后未覆盖",
    "unfunded": "未覆盖",
}


def explain(plan: dict[str, Any]) -> str:
    s = plan["summary"]
    lines = [f"方案「{plan['name']}」（{plan['id']}，状态：{plan['status']}）"]
    if plan.get("reason"):
        lines.append(f"重算原因：{plan['reason']}")
    if plan.get("base_plan_id"):
        lines.append(f"基于方案 {plan['base_plan_id']} 的假设重算；已执行提领保持原值，未被新假设覆盖。")
    snap = plan["assumptions_snapshot"]
    tax_desc = "、".join(f"{t['year']}年 {t['rate']:.1%}（{t['name']}/{t['id']}）" for t in plan["versions_snapshot"]["tax"])
    lines.append(f"采用版本：税率 {tax_desc}；节假日表 {plan['versions_snapshot']['holiday']['name']}（{plan['versions_snapshot']['holiday']['id']}）。")
    lines.append(
        f"规划区间 {snap['start_month']} 起 {snap['horizon_months']} 个月，"
        f"每月固定提领净额 {snap['fixed_monthly_withdrawal']:.2f} 元，安全底线 {snap['emergency_floor']:.2f} 元。"
    )
    if s["exhaustion_month"]:
        lines.append(f"资金将在 {s['exhaustion_month']} 耗尽。")
    else:
        lines.append(f"规划期内不会耗尽资金，期末（{s['final_month']}）余额 {s['final_balance']:.2f} 元。")
    if s["floor_breach_month"]:
        lines.append(f"安全底线首次在 {s['floor_breach_month']} 被击穿，固定提领与医疗储备在该月发生冲突。")
    else:
        lines.append("规划期内安全底线始终得到保留。")
    for c in s["goal_coverage"]:
        lines.append(
            f"- {c['name']}（{c['type']}，目标月 {c['due_month']}）：覆盖率 {c['coverage_pct']:.1f}%，{_STATUS_TEXT.get(c['status'], c['status'])}"
            + (f"，出资月份 {c['funded_month']}" if c["funded_month"] else "")
            + (f"，首次延后 {c['first_deferred_month']}，原因：{c['reason']}" if c["reason"] else "")
        )
    if s["deferred_goals"]:
        lines.append("被延后/未覆盖项目：" + "、".join(f"{c['name']}（{c['reason'] or _STATUS_TEXT.get(c['status'], c['status'])}）" for c in s["deferred_goals"]))
    for pr in plan["payout_resolution"]:
        lines.append(f"赔付 {pr['payout_id']} 到账日 {pr['arrival_date']}（{'实际到账，不可改写' if pr['source'] == 'actual' else '按采用节假日表顺延的预期日'}）。")
    for w in s["warnings"]:
        lines.append("提示：" + w)
    return "\n".join(lines)


def compare_plans(plans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scenarios": [
            {
                "plan_id": p["id"],
                "name": p["name"],
                "status": p["status"],
                "reason": p.get("reason"),
                "exhaustion_month": p["summary"]["exhaustion_month"],
                "floor_breach_month": p["summary"]["floor_breach_month"],
                "final_month": p["summary"]["final_month"],
                "final_balance": p["summary"]["final_balance"],
                "goal_coverage": p["summary"]["goal_coverage"],
                "deferred_goals": [
                    {"goal_id": c["goal_id"], "name": c["name"], "reason": c["reason"], "first_deferred_month": c["first_deferred_month"]}
                    for c in p["summary"]["deferred_goals"]
                ],
            }
            for p in plans
        ]
    }
