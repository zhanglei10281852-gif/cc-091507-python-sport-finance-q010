"""业务服务：账户、估值快照、方案、情景、提领与审批。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from domain import (
    CURRENCIES,
    GOAL_TYPES,
    PLAN_ACTIVE,
    PLAN_PENDING,
    PLAN_REJECTED,
    PURPOSE_FIXED,
    PURPOSE_GOAL,
    QUANTITY_PRECISION,
    arrival_month,
    month_str,
    parse_date,
    parse_month,
)
from engine import simulate

DEFAULT_TAX_VERSION = "tax-v1"
DEFAULT_HOLIDAYS_VERSION = "holidays-v1"

DEFAULT_TAX_TABLE = {"2026": 0.10, "2027": 0.10, "2028": 0.12, "2029": 0.12,
                     "2030": 0.15, "2031": 0.15, "2032": 0.15, "2033": 0.15,
                     "2034": 0.15, "2035": 0.15}
DEFAULT_HOLIDAYS = {
    "version": DEFAULT_HOLIDAYS_VERSION,
    # 示例：春节假期到账顺延（ISO 日期）
    "holidays": ["2027-02-08", "2027-02-09", "2027-02-10",
                 "2028-01-26", "2028-01-27", "2028-01-28"],
    "note": "默认节假日表 v1",
}


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store) -> None:
        self.store = store
        self._seed()

    def _seed(self) -> None:
        def fn(state):
            state.setdefault("tax_tables", {})
            state.setdefault("holiday_calendars", {})
            if DEFAULT_TAX_VERSION not in state["tax_tables"]:
                state["tax_tables"][DEFAULT_TAX_VERSION] = dict(DEFAULT_TAX_TABLE)
            if DEFAULT_HOLIDAYS_VERSION not in state["holiday_calendars"]:
                state["holiday_calendars"][DEFAULT_HOLIDAYS_VERSION] = dict(DEFAULT_HOLIDAYS)
        self.store.mutate(fn)

    # ---------- 事件 ----------
    def _event(self, state, etype: str, payload: dict[str, Any]) -> None:
        state["events"].append(
            {"id": _new_id("evt"), "type": etype, "at": _now(), "payload": payload}
        )

    # ---------- 账户 ----------
    def register_client(self, client_id: str, name: str, pin: str) -> dict:
        if not client_id or not pin:
            raise ServiceError(400, "bad_request", "client_id 与 pin 必填")

        def fn(state):
            if client_id in state["users"]:
                raise ServiceError(409, "client_exists", f"客户 {client_id} 已存在")
            user = {"id": client_id, "name": name, "pin": pin, "created_at": _now()}
            state["users"][client_id] = user
            return dict(user)
        return self.store.mutate(fn)

    def authenticate(self, state, client_id: str | None, pin: str | None) -> dict | None:
        if not client_id:
            return None
        user = state["users"].get(client_id)
        if user and user["pin"] == pin:
            return user
        return None

    def require_client(self, client_id: str | None, pin: str | None) -> dict:
        state = self.store.snapshot()
        user = self.authenticate(state, client_id, pin)
        if not user:
            raise ServiceError(401, "unauthorized", "客户身份验证失败")
        return user

    # ---------- 税率 / 节假日版本 ----------
    def add_tax_table(self, version: str, table: dict[str, float]) -> dict:
        if not version or not isinstance(table, dict) or not table:
            raise ServiceError(400, "bad_request", "version 与年度税率表必填")
        for k, v in table.items():
            if not str(k).isdigit() or not 0 <= float(v) < 1:
                raise ServiceError(400, "bad_request", f"税率表项非法: {k}={v}")

        def fn(state):
            if version in state["tax_tables"]:
                raise ServiceError(409, "version_exists", f"税率版本 {version} 已存在")
            state["tax_tables"][version] = {str(k): float(v) for k, v in table.items()}
            self._event(state, "tax_change", {"version": version, "table": table})
            return {"version": version, "table": state["tax_tables"][version]}
        return self.store.mutate(fn)

    def add_holiday_calendar(self, version: str, holidays: list[str], note: str = "") -> dict:
        parsed = []
        for h in holidays:
            try:
                parsed.append(parse_date(h).isoformat())
            except ValueError:
                raise ServiceError(400, "bad_request", f"节假日日期非法: {h}")

        def fn(state):
            if version in state["holiday_calendars"]:
                raise ServiceError(409, "version_exists", f"节假日版本 {version} 已存在")
            cal = {"version": version, "holidays": parsed, "note": note}
            state["holiday_calendars"][version] = cal
            return {"version": version, **cal}
        return self.store.mutate(fn)

    def _resolve_tax(self, state, version: str) -> dict[str, float]:
        table = state["tax_tables"].get(version)
        if table is None:
            raise ServiceError(400, "unknown_tax_version", f"未知税率版本: {version}")
        return dict(table)

    def _resolve_holidays(self, state, version: str) -> list[str]:
        cal = state["holiday_calendars"].get(version)
        if cal is None:
            raise ServiceError(400, "unknown_holidays_version", f"未知节假日版本: {version}")
        return list(cal["holidays"])

    # ---------- 估值快照（幂等导入） ----------
    def import_valuation(self, body: dict) -> dict:
        source_key = body.get("source_key")
        if not source_key:
            # 未显式提供时按内容派生，保证同一份估值数据重复导入不产生重复快照
            source_key = "sha256:" + hashlib.sha256(
                str(sorted(body.get("holdings", []))).encode()
            ).hexdigest()[:16]
        as_of = body.get("as_of_date")
        try:
            parse_date(as_of)
        except (ValueError, TypeError):
            raise ServiceError(400, "bad_request", "as_of_date 应为 YYYY-MM-DD")

        holdings = body.get("holdings", [])
        total = 0.0
        clean_holdings = []
        for h in holdings:
            currency = h.get("currency", "CNY")
            if currency not in CURRENCIES:
                raise ServiceError(400, "bad_request", f"不支持的币种: {currency}")
            qty = round(float(h["quantity"]), QUANTITY_PRECISION)
            price = float(h["price"])
            if qty < 0 or price < 0:
                raise ServiceError(400, "bad_request", "持仓数量与价格不可为负")
            total += qty * price
            clean_holdings.append(
                {"symbol": h["symbol"], "quantity": qty, "price": price,
                 "currency": currency, "value": round(qty * price, 2)}
            )
        if "total_value" in body:
            total = float(body["total_value"])

        def fn(state):
            existing = state["snapshots"].get(source_key)
            if existing:
                # 幂等：同一份估值数据直接返回原快照，不新增、不覆盖
                return dict(existing, duplicate=True)
            snap = {
                "id": _new_id("snap"),
                "source_key": source_key,
                "as_of_date": as_of,
                "imported_at": _now(),
                "holdings": clean_holdings,
                "total_value": round(total, 2),
                "currency": body.get("currency", "CNY"),
                "note": body.get("note", ""),
                "duplicate": False,
            }
            state["snapshots"][source_key] = snap
            self._event(state, "valuation",
                        {"snapshot_id": snap["id"], "source_key": source_key,
                         "as_of_date": as_of, "total_value": snap["total_value"]})
            return dict(snap)
        return self.store.mutate(fn)

    def list_snapshots(self) -> list[dict]:
        return list(self.store.snapshot()["snapshots"].values())

    # ---------- 方案 ----------
    def _validate_goals(self, goals: list[dict]) -> list[dict]:
        out = []
        seen = set()
        for g in goals:
            gid = g.get("id")
            if not gid or gid in seen:
                raise ServiceError(400, "bad_request", "目标 id 缺失或重复")
            seen.add(gid)
            if g.get("type") not in GOAL_TYPES:
                raise ServiceError(400, "bad_request", f"未知目标类型: {g.get('type')}")
            cost = float(g.get("cost", 0))
            if cost < 0:
                raise ServiceError(400, "bad_request", "目标成本不可为负")
            try:
                due = month_str(parse_month(g["due_month"]))
            except (ValueError, KeyError) as exc:
                raise ServiceError(400, "bad_request", str(exc))
            out.append({"id": gid, "type": g["type"], "name": g.get("name", gid),
                        "cost": cost, "priority": int(g.get("priority", 100)),
                        "due_month": due,
                        "deferrable": bool(g.get("deferrable", True))})
        return out

    def _validate_payouts(self, payouts: list[dict]) -> list[dict]:
        out = []
        seen = set()
        for p in payouts:
            pid = p.get("id")
            if not pid or pid in seen:
                raise ServiceError(400, "bad_request", "赔付 id 缺失或重复")
            seen.add(pid)
            amount = float(p.get("amount", 0))
            if amount < 0:
                raise ServiceError(400, "bad_request", "赔付金额不可为负")
            try:
                exp = parse_date(p["expected_date"]).isoformat()
            except (ValueError, KeyError) as exc:
                raise ServiceError(400, "bad_request", str(exc))
            out.append({"id": pid, "amount": amount, "expected_date": exp,
                        "name": p.get("name", pid)})
        return out

    def create_plan(self, body: dict, advisor: bool) -> dict:
        if not advisor:
            raise ServiceError(403, "forbidden", "仅顾问可创建方案")
        client_id = body.get("client_id")
        state = self.store.snapshot()
        if client_id not in state["users"]:
            raise ServiceError(404, "client_not_found", f"客户不存在: {client_id}")
        snapshot = None
        if body.get("snapshot_id"):
            snapshot = next((s for s in state["snapshots"].values()
                             if s["id"] == body["snapshot_id"]), None)
            if not snapshot:
                raise ServiceError(404, "snapshot_not_found", "估值快照不存在")
        try:
            start = month_str(parse_month(body["start_month"]))
        except (ValueError, KeyError) as exc:
            raise ServiceError(400, "bad_request", str(exc))
        horizon = int(body.get("horizon_months", 120))
        if not 1 <= horizon <= 600:
            raise ServiceError(400, "bad_request", "horizon_months 应在 1..600")
        balance = float(body.get("initial_balance",
                                 snapshot["total_value"] if snapshot else 0.0))
        goals = self._validate_goals(body.get("goals", []))
        payouts = self._validate_payouts(body.get("payouts", []))
        floor = float(body.get("floor", 0.0))
        if floor < 0 or floor > balance:
            raise ServiceError(400, "bad_request", "安全底线不可为负或超过期初余额")
        tax_version = body.get("tax_version", DEFAULT_TAX_VERSION)
        holidays_version = body.get("holidays_version", DEFAULT_HOLIDAYS_VERSION)
        tax_table = self._resolve_tax(state, tax_version)
        self._resolve_holidays(state, holidays_version)

        plan_id = _new_id("plan")
        plan = {
            "id": plan_id,
            "client_id": client_id,
            "name": body.get("name", "提领方案"),
            "status": PLAN_PENDING,
            "created_at": _now(),
            "snapshot_id": snapshot["id"] if snapshot else None,
            "initial_balance": balance,
            "base_assumptions": {
                "start_month": start,
                "horizon_months": horizon,
                "annual_return": float(body.get("annual_return", 0.04)),
                "monthly_shocks": body.get("monthly_shocks", {}),
                "fixed_monthly": float(body.get("fixed_monthly", 0.0)),
                "floor": floor,
            },
            "goals": goals,
            "payouts": payouts,
            "actuals": [],          # 已执行事实：锁定，新假设不可覆盖
            "scenarios": {},
            "decision": None,
            "decided_at": None,
        }

        def fn(state):
            self._resolve_tax(state, tax_version)
            self._resolve_holidays(state, holidays_version)
            state["plans"][plan_id] = plan
            baseline = self._build_scenario(state, plan, "baseline", "基准情景",
                                            tax_version, holidays_version,
                                            adjustments={})
            plan["scenarios"]["baseline"] = baseline
            self._event(state, "scenario_update",
                        {"plan_id": plan_id, "scenario": "baseline"})
            return self._public_plan(state, plan, include_rows=False)
        return self.store.mutate(fn)

    def _build_scenario(self, state, plan, key: str, name: str,
                        tax_version: str, holidays_version: str,
                        adjustments: dict) -> dict:
        """根据已采用的税率/节假日版本与调整项构建情景（冻结假设副本）。"""
        tax_table = self._resolve_tax(state, tax_version)
        holidays = self._resolve_holidays(state, holidays_version)
        assumptions = dict(plan["base_assumptions"])
        assumptions["tax_by_year"] = dict(tax_table)
        assumptions["holidays"] = holidays
        assumptions["holidays_version"] = holidays_version
        assumptions["as_of_month"] = self._as_of_month(plan)

        shocks = dict(assumptions.get("monthly_shocks", {}))
        goals = [dict(g) for g in plan["goals"]]
        payouts = [dict(p) for p in plan["payouts"]]

        # 市场下跌：在指定月注入冲击收益率
        md = adjustments.get("market_down")
        if md:
            try:
                mm = month_str(parse_month(md["month"]))
            except (ValueError, KeyError) as exc:
                raise ServiceError(400, "bad_request", str(exc))
            shocks[mm] = float(md.get("monthly_return", md.get("pct", 0.0)))
        assumptions["monthly_shocks"] = shocks

        # 目标提前
        ga = adjustments.get("goal_advance")
        if ga:
            gid = ga.get("goal_id")
            g = next((x for x in goals if x["id"] == gid), None)
            if not g:
                raise ServiceError(404, "goal_not_found", f"目标不存在: {gid}")
            try:
                g["due_month"] = month_str(parse_month(ga["to_month"]))
            except (ValueError, KeyError) as exc:
                raise ServiceError(400, "bad_request", str(exc))

        # 赔付到账：覆盖预计到账日期（节假日仍按采用版本顺延）
        pa = adjustments.get("payout_arrival")
        if pa:
            pid = pa.get("payout_id")
            p = next((x for x in payouts if x["id"] == pid), None)
            if not p:
                raise ServiceError(404, "payout_not_found", f"赔付不存在: {pid}")
            try:
                p["expected_date"] = parse_date(pa["expected_date"]).isoformat()
            except (ValueError, KeyError) as exc:
                raise ServiceError(400, "bad_request", str(exc))

        result = simulate(plan["initial_balance"], assumptions, goals, payouts,
                          plan["actuals"])
        return {
            "key": key,
            "name": name,
            "created_at": _now(),
            "adopted_tax_version": tax_version,
            "adopted_holidays_version": holidays_version,
            "adjustments": adjustments,
            "assumptions_snapshot": {
                "annual_return": assumptions["annual_return"],
                "fixed_monthly": assumptions["fixed_monthly"],
                "floor": assumptions["floor"],
                "monthly_shocks": assumptions["monthly_shocks"],
                "tax_by_year": assumptions["tax_by_year"],
                # 节假日到账推算结果保留原采用版本
                "holidays_version": holidays_version,
            },
            "result": result,
            "explanation": self._explain(name, assumptions, goals, payouts, result),
        }

    def _as_of_month(self, plan) -> str | None:
        months = sorted(a["month"] for a in plan["actuals"])
        return months[-1] if months else None

    def _explain(self, name, assumptions, goals, payouts, result) -> list[str]:
        lines: list[str] = []
        floor = assumptions["floor"]
        fixed = assumptions["fixed_monthly"]
        ex = result["exhausted_month"]
        if ex:
            lines.append(
                f"「{name}」在 {ex} 固定提领首次无法在不突破安全底线 "
                f"（{floor:,.0f} 元）的情况下足额支付 {fixed:,.0f} 元/月，"
                f"账户安全提领能力到此月为止。")
        else:
            lines.append(
                f"「{name}」在整个规划期内均能维持 {fixed:,.0f} 元/月的固定提领，"
                f"且余额始终不低于安全底线 {floor:,.0f} 元，"
                f"期末余额约 {result['final_balance']:,.0f} 元。")
        funded_late = [g for g in result["goals"].values() if g["status"] == "funded_late"]
        for g in funded_late:
            lines.append(f"目标「{g['name']}」被延后至 {g['funded_month']} 才完成筹资"
                         f"（原到期月 {g['due_month']}）。")
        if result["deferred"]:
            names = "、".join(f"{d['name']}({d['due_month']})" for d in result["deferred"])
            lines.append(f"以下目标在规划期内未能足额覆盖，建议延后或追加资金：{names}。")
        else:
            lines.append(f"全部目标覆盖率 100%，整体覆盖率 {result['coverage_rate']*100:.1f}%。")
        for p in payouts:
            settled, arr = arrival_month(parse_date(p["expected_date"]),
                                         set(assumptions["holidays"]))
            if settled.isoformat()[:7] != p["expected_date"][:7] or settled.isoformat() != p["expected_date"]:
                lines.append(
                    f"赔付「{p['name']}」预计 {p['expected_date']} 到账，"
                    f"按节假日版本 {assumptions['holidays_version']} 顺延至 "
                    f"{settled.isoformat()}（计入 {arr}）。")
        if result["fixed_shortfalls"]:
            total_gap = sum(x["shortfall"] for x in result["fixed_shortfalls"])
            lines.append(f"规划期内固定提领累计缺口约 {total_gap:,.0f} 元，"
                         f"与医疗储备之间存在 {len(result['fixed_shortfalls'])} 个月冲突。")
        return lines

    def add_scenario(self, plan_id: str, body: dict, advisor: bool) -> dict:
        if not advisor:
            raise ServiceError(403, "forbidden", "仅顾问可新增情景")
        key = body.get("key") or _new_id("sc")
        if not key or key in ("baseline",):
            raise ServiceError(400, "bad_request", "情景 key 非法或与 baseline 冲突")

        def fn(state):
            plan = state["plans"].get(plan_id)
            if not plan:
                raise ServiceError(404, "plan_not_found", "方案不存在")
            if key in plan["scenarios"]:
                raise ServiceError(409, "scenario_exists", f"情景已存在: {key}")
            tax_version = body.get("tax_version",
                                   plan["scenarios"]["baseline"]["adopted_tax_version"])
            holidays_version = body.get(
                "holidays_version",
                plan["scenarios"]["baseline"]["adopted_holidays_version"])
            sc = self._build_scenario(
                state, plan, key, body.get("name", key), tax_version,
                holidays_version,
                adjustments={k: body[k] for k in
                             ("market_down", "goal_advance", "payout_arrival")
                             if k in body})
            plan["scenarios"][key] = sc
            self._event(state, "scenario_update",
                        {"plan_id": plan_id, "scenario": key})
            return self._summary(sc)
        return self.store.mutate(fn)

    def recompute(self, plan_id: str, advisor: bool) -> dict:
        """市场变化/目标提前/赔付到账后按安全底线重算后续提领。

        已执行提领（actuals）原样保留；仅锁定月之后的投影重算，
        每个情景继续采用其原采用的税率/节假日版本。
        """
        if not advisor:
            raise ServiceError(403, "forbidden", "仅顾问可重算")

        def fn(state):
            plan = state["plans"].get(plan_id)
            if not plan:
                raise ServiceError(404, "plan_not_found", "方案不存在")
            out = {}
            for key, sc in plan["scenarios"].items():
                rebuilt = self._build_scenario(
                    state, plan, key, sc["name"],
                    sc["adopted_tax_version"], sc["adopted_holidays_version"],
                    adjustments=sc.get("adjustments", {}))
                rebuilt["created_at"] = sc["created_at"]
                plan["scenarios"][key] = rebuilt
                out[key] = self._summary(rebuilt)
            self._event(state, "scenario_update",
                        {"plan_id": plan_id, "scenario": "*", "reason": "recompute"})
            return out
        return self.store.mutate(fn)

    # ---------- 已执行提领（锁定） ----------
    def execute_withdrawal(self, plan_id: str, body: dict, advisor: bool) -> dict:
        if not advisor:
            raise ServiceError(403, "forbidden", "仅顾问可登记提领")
        kind = body.get("kind")
        if kind not in (PURPOSE_FIXED, PURPOSE_GOAL, "payout"):
            raise ServiceError(400, "bad_request", "kind 应为 fixed/goal/payout")
        try:
            ms = month_str(parse_month(body["month"]))
        except (ValueError, KeyError) as exc:
            raise ServiceError(400, "bad_request", str(exc))
        net = float(body.get("net_amount", 0.0))
        if net < 0:
            raise ServiceError(400, "bad_request", "金额不可为负")
        goal_id = body.get("goal_id")
        arrival = None
        if body.get("arrival_date"):
            try:
                arrival = parse_date(body["arrival_date"]).isoformat()
            except ValueError as exc:
                raise ServiceError(400, "bad_request", str(exc))

        def fn(state):
            plan = state["plans"].get(plan_id)
            if not plan:
                raise ServiceError(404, "plan_not_found", "方案不存在")
            if ms < plan["base_assumptions"]["start_month"]:
                raise ServiceError(400, "bad_request", "提领月份早于方案起始月")
            goal = None
            if kind == PURPOSE_GOAL:
                goal = next((g for g in plan["goals"] if g["id"] == goal_id), None)
                if not goal:
                    raise ServiceError(404, "goal_not_found", f"目标不存在: {goal_id}")
            # 税率：采用执行时基准情景对该年度的已采用税率（事实留存，不改写）
            tax_rate = body.get("tax_rate")
            if tax_rate is None:
                tax_table = plan["scenarios"]["baseline"]["assumptions_snapshot"]["tax_by_year"]
                tax_rate = float(tax_table.get(str(parse_month(ms).year), 0.0))
            tax_rate = float(tax_rate)
            if kind == "payout":
                gross = 0.0
                delta = net
            else:
                gross = float(body.get("gross_amount",
                                       net / (1 - tax_rate) if tax_rate < 1 else net))
                delta = -gross
            wid = _new_id("wd")
            actual = {
                "id": wid,
                "month": ms,
                "kind": kind,
                "goal_id": goal_id if kind == PURPOSE_GOAL else None,
                "funded_net": net,
                "gross": round(gross, 2),
                "delta_balance": round(delta, 2),
                "tax_rate": tax_rate,
                "arrival_date": arrival,
                "recorded_at": _now(),
            }
            # 幂等：同月同目标/类型同金额不重复登记
            dup = next((a for a in plan["actuals"]
                        if a["month"] == ms and a["kind"] == kind
                        and a.get("goal_id") == actual["goal_id"]
                        and abs(a["funded_net"] - net) < 1e-6), None)
            if dup:
                return dict(dup, duplicate=True)
            plan["actuals"].append(actual)
            plan["actuals"].sort(key=lambda a: a["month"])
            etype = "insurance_payout" if kind == "payout" else "withdrawal"
            self._event(state, etype, {"plan_id": plan_id, "withdrawal_id": wid,
                                      "month": ms, "net": net})
            if kind == PURPOSE_GOAL and net + 1e-6 < float(goal["cost"]):
                self._event(state, "goal_defer",
                            {"plan_id": plan_id, "goal_id": goal_id, "month": ms,
                             "funded_net": net, "cost": goal["cost"]})
            # 登记事实后立即按安全底线重算后续投影
            for key, sc in plan["scenarios"].items():
                rebuilt = self._build_scenario(
                    state, plan, key, sc["name"],
                    sc["adopted_tax_version"], sc["adopted_holidays_version"],
                    adjustments=sc.get("adjustments", {}))
                rebuilt["created_at"] = sc["created_at"]
                plan["scenarios"][key] = rebuilt
            return dict(actual, duplicate=False)
        return self.store.mutate(fn)

    # ---------- 审批（重启后仍可继续审核） ----------
    def decide_plan(self, plan_id: str, decision: str, user: dict) -> dict:
        if decision not in ("approved", "rejected"):
            raise ServiceError(400, "bad_request", "decision 应为 approved/rejected")

        def fn(state):
            plan = state["plans"].get(plan_id)
            if not plan:
                raise ServiceError(404, "plan_not_found", "方案不存在")
            if plan["client_id"] != user["id"]:
                raise ServiceError(403, "forbidden", "客户只能审核自己的方案")
            if plan["status"] in (PLAN_ACTIVE, PLAN_REJECTED):
                raise ServiceError(409, "plan_closed", f"方案已结束: {plan['status']}")
            plan["status"] = PLAN_ACTIVE if decision == "approved" else PLAN_REJECTED
            plan["decision"] = decision
            plan["decided_at"] = _now()
            return self._public_plan(state, plan, include_rows=False, viewer="client")
        return self.store.mutate(fn)

    # ---------- 查询 ----------
    def _ensure_access(self, plan, user: dict | None, role: str | None) -> str:
        if role == "advisor":
            return "advisor"
        if user and plan["client_id"] == user["id"]:
            return "client"
        raise ServiceError(403, "forbidden", "无权访问该方案")

    def list_plans(self, user: dict | None, role: str | None) -> list[dict]:
        state = self.store.snapshot()
        out = []
        for plan in state["plans"].values():
            if role != "advisor" and (not user or plan["client_id"] != user["id"]):
                continue
            base = plan["scenarios"].get("baseline")
            out.append({
                "id": plan["id"],
                "name": plan["name"],
                "client_id": plan["client_id"],
                "status": plan["status"],
                "created_at": plan["created_at"],
                "exhausted_month": base["result"]["exhausted_month"] if base else None,
                "coverage_rate": base["result"]["coverage_rate"] if base else None,
            })
        return out

    def get_plan(self, plan_id: str, user: dict | None, role: str | None) -> dict:
        state = self.store.snapshot()
        plan = state["plans"].get(plan_id)
        if not plan:
            raise ServiceError(404, "plan_not_found", "方案不存在")
        viewer = self._ensure_access(plan, user, role)
        return self._public_plan(state, plan, include_rows=(viewer == "advisor"),
                                 viewer=viewer)

    def _summary(self, sc: dict) -> dict:
        r = sc["result"]
        return {
            "key": sc["key"],
            "name": sc["name"],
            "adopted_tax_version": sc["adopted_tax_version"],
            "adopted_holidays_version": sc["adopted_holidays_version"],
            "exhausted_month": r["exhausted_month"],
            "final_balance": r["final_balance"],
            "coverage_rate": r["coverage_rate"],
            "deferred": r["deferred"],
            "goals": {gid: {"status": g["status"], "coverage": g["coverage"],
                            "funded_month": g["funded_month"]}
                      for gid, g in r["goals"].items()},
            "explanation": sc["explanation"],
        }

    def compare(self, plan_id: str, user: dict | None, role: str | None) -> dict:
        state = self.store.snapshot()
        plan = state["plans"].get(plan_id)
        if not plan:
            raise ServiceError(404, "plan_not_found", "方案不存在")
        self._ensure_access(plan, user, role)
        return {
            "plan_id": plan_id,
            "client_id": plan["client_id"],
            "as_of_month": self._as_of_month(plan),
            "scenarios": [self._summary(sc) for sc in plan["scenarios"].values()],
        }

    def _public_plan(self, state, plan, include_rows: bool, viewer: str | None = None) -> dict:
        scenarios = {}
        for key, sc in plan["scenarios"].items():
            entry: dict[str, Any] = {
                "key": key,
                "name": sc["name"],
                "created_at": sc["created_at"],
                "adopted_tax_version": sc["adopted_tax_version"],
                "adopted_holidays_version": sc["adopted_holidays_version"],
                "adjustments": sc.get("adjustments", {}),
                "assumptions_snapshot": sc["assumptions_snapshot"],
                "explanation": sc["explanation"],
                "summary": {
                    "exhausted_month": sc["result"]["exhausted_month"],
                    "coverage_rate": sc["result"]["coverage_rate"],
                    "final_balance": sc["result"]["final_balance"],
                    "goals": sc["result"]["goals"],
                    "deferred": sc["result"]["deferred"],
                    "fixed_shortfalls": sc["result"]["fixed_shortfalls"],
                },
            }
            # 客户只看到汇总与解释；月度明细仅顾问可见
            if include_rows:
                entry["monthly"] = sc["result"]["monthly"]
            scenarios[key] = entry
        return {
            "id": plan["id"],
            "client_id": plan["client_id"],
            "name": plan["name"],
            "status": plan["status"],
            "decision": plan["decision"],
            "decided_at": plan["decided_at"],
            "created_at": plan["created_at"],
            "snapshot_id": plan["snapshot_id"],
            "initial_balance": plan["initial_balance"],
            "base_assumptions": plan["base_assumptions"],
            "goals": plan["goals"],
            "payouts": plan["payouts"],
            "actuals": plan["actuals"],
            "as_of_month": self._as_of_month(plan),
            "scenarios": scenarios,
        }
