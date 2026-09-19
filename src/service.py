"""应用服务层：校验输入、编排持久化与引擎、执行权限规则。"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from typing import Any

from domain import (
    CURRENCIES,
    EMERGENCY_RESERVE,
    GOAL_TYPES,
    MONEY_EPS,
    money,
    now_iso,
    parse_date,
    parse_month,
)
from engine import EngineInput, PlanError, build_plan, compare_plans
from storage import Store

WRITE_LOCK = threading.RLock()


class ServiceError(ValueError):
    """可对外展示的 400 类错误。"""


def _require(obj: dict[str, Any], key: str) -> Any:
    if key not in obj or obj[key] in (None, ""):
        raise ServiceError(f"缺少必填字段: {key}")
    return obj[key]


def _amount(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"{field} 必须是数字") from exc
    if result < 0:
        raise ServiceError(f"{field} 不能为负")
    return money(result)


def _currency(value: Any) -> str:
    if value not in CURRENCIES:
        raise ServiceError(f"币种必须是 {CURRENCIES} 之一")
    return value


class PlanningService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------ 客户

    def create_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        cid = str(_require(payload, "id")).strip()
        name = str(_require(payload, "name")).strip()
        if not cid.replace("-", "").replace("_", "").isalnum():
            raise ServiceError("客户 id 只能包含字母数字、-、_")
        try:
            state = self.store.create_client(cid, name, now_iso())
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        token = secrets.token_urlsafe(18)
        state["client"]["view_token"] = token
        self.store.save_client(state)
        return {"client_id": cid, "name": name, "view_token": token,
                "note": "view_token 仅在创建时展示一次，交给客户用于自助查询"}

    def _load(self, client_id: str) -> dict[str, Any]:
        try:
            return self.store.load_client(client_id)
        except KeyError as exc:
            raise ServiceError(str(exc)) from exc

    def authorize_portal(self, token: str) -> dict[str, Any]:
        """客户门户令牌换客户状态（只能看到自己的数据）。"""
        if not token:
            raise ServiceError("缺少客户令牌")
        for cid in self.store.list_clients():
            state = self.store.load_client(cid)
            if secrets.compare_digest(str(state["client"].get("view_token", "")), token):
                return state
        raise ServiceError("客户令牌无效")

    # ------------------------------------------------------------ 账户/目标

    def add_account(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            aid = str(_require(payload, "id"))
            if any(a["id"] == aid for a in state["accounts"]):
                raise ServiceError(f"账户已存在: {aid}")
            currency = _currency(payload.get("currency", "CNY"))
            ret = payload.get("expected_return", 0.0)
            try:
                ret = float(ret)
            except (TypeError, ValueError) as exc:
                raise ServiceError("expected_return 必须是数字") from exc
            account = {
                "id": aid,
                "name": str(payload.get("name", aid)),
                "currency": currency,
                "expected_return": ret,
                "created_at": now_iso(),
            }
            state["accounts"].append(account)
            self.store.save_client(state)
            return account

    def add_goal(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            gid = str(_require(payload, "id"))
            if any(g["id"] == gid for g in state["goals"]):
                raise ServiceError(f"目标已存在: {gid}")
            gtype = str(_require(payload, "type"))
            if gtype not in GOAL_TYPES:
                raise ServiceError(f"目标类型必须是 {GOAL_TYPES} 之一")
            due = str(_require(payload, "due_month")) if gtype != EMERGENCY_RESERVE else str(payload.get("due_month", "9999-12"))
            parse_month(due)
            goal = {
                "id": gid,
                "name": str(_require(payload, "name")),
                "type": gtype,
                "amount": _amount(_require(payload, "amount"), "amount"),
                "currency": _currency(payload.get("currency", "CNY")),
                "due_month": due,
                "priority": int(payload.get("priority", 100)),
                "deferrable": bool(payload.get("deferrable", gtype != "rehabilitation")),
                "created_at": now_iso(),
            }
            state["goals"].append(goal)
            self.store.save_client(state)
            return goal

    # ------------------------------------------------------------ 版本

    def add_tax_version(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            vid = str(_require(payload, "id"))
            year = int(_require(payload, "year"))
            rate = float(_require(payload, "rate"))
            if not 0 <= rate < 1:
                raise ServiceError("rate 必须在 [0, 1) 区间")
            if any(t["id"] == vid for t in state["tax_versions"]):
                raise ServiceError(f"税率版本已存在: {vid}")
            tv = {
                "id": vid,
                "year": year,
                "name": str(payload.get("name", vid)),
                "rate": rate,
                "locked": bool(payload.get("locked", False)),
                "created_at": now_iso(),
            }
            state["tax_versions"].append(tv)
            # 首个版本或显式 locked 的版本自动成为当前采用版本；
            # 之后可在新方案中显式选用其他版本，确认方案时更新采用关系。
            if tv["locked"] or str(year) not in state.get("adopted_tax_versions", {}):
                state.setdefault("adopted_tax_versions", {})[str(year)] = vid
            self.store.save_client(state)
            return tv

    def add_holiday_version(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            hid = str(_require(payload, "id"))
            if any(h["id"] == hid for h in state["holiday_versions"]):
                raise ServiceError(f"节假日版本已存在: {hid}")
            holidays = sorted({parse_date(str(d), "holidays").isoformat() for d in _require(payload, "holidays")})
            hv = {
                "id": hid,
                "name": str(payload.get("name", hid)),
                "holidays": holidays,
                "locked": bool(payload.get("locked", False)),
                "created_at": now_iso(),
            }
            state["holiday_versions"].append(hv)
            if hv["locked"] or not state.get("adopted_holiday_version_id"):
                state["adopted_holiday_version_id"] = hid
            self.store.save_client(state)
            return hv

    # ------------------------------------------------------------ 估值导入（幂等）

    def import_valuation(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            as_of = parse_date(str(_require(payload, "as_of")), "as_of").isoformat()
            raw_positions = _require(payload, "positions")
            if not isinstance(raw_positions, list) or not raw_positions:
                raise ServiceError("positions 必须是非空列表")
            account_ids = {a["id"] for a in state["accounts"]}
            positions = []
            for pos in raw_positions:
                acc_id = str(_require(pos, "account_id"))
                if acc_id not in account_ids:
                    raise ServiceError(f"持仓引用了未知账户: {acc_id}")
                amount = _amount(_require(pos, "amount"), "amount")
                currency = _currency(pos.get("currency", next(a["currency"] for a in state["accounts"] if a["id"] == acc_id)))
                positions.append({"account_id": acc_id, "amount": amount, "currency": currency})
            positions.sort(key=lambda p: p["account_id"])
            source_ref = payload.get("source_ref")
            digest_src = json.dumps(
                {"as_of": as_of, "source_ref": source_ref, "positions": positions},
                ensure_ascii=False,
                sort_keys=True,
            )
            content_hash = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()

            # 幂等：同一业务内容（同日/同源/同持仓）无论导入多少次只保留一份快照。
            for existing in state["valuations"]:
                if existing["content_hash"] == content_hash:
                    return {"deduplicated": True, "valuation_id": existing["id"], "as_of": existing["as_of"]}

            vid = payload.get("id") or f"val-{len(state['valuations']) + 1}-{content_hash[:8]}"
            if any(v["id"] == vid for v in state["valuations"]):
                raise ServiceError(f"估值 id 重复: {vid}")
            valuation = {
                "id": str(vid),
                "as_of": as_of,
                "source_ref": source_ref,
                "positions": positions,
                "content_hash": content_hash,
                "imported_at": now_iso(),
            }
            state["valuations"].append(valuation)
            self.store.save_client(state)
            return {"deduplicated": False, "valuation_id": valuation["id"], "as_of": as_of}

    # ------------------------------------------------------------ 提领（不可变事实）

    def record_withdrawal(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            day = parse_date(str(_require(payload, "date")), "date")
            net = _amount(_require(payload, "net_cny"), "net_cny")
            gross = payload.get("gross_cny")
            gross = _amount(gross, "gross_cny") if gross is not None else net
            if gross + MONEY_EPS < net:
                raise ServiceError("gross_cny 不能小于 net_cny")
            state["counters"]["withdrawal"] += 1
            wid = payload.get("id") or f"wd-{state['counters']['withdrawal']}"
            if any(w["id"] == wid for w in state["withdrawals"]):
                state["counters"]["withdrawal"] -= 1
                raise ServiceError(f"提领 id 重复: {wid}")
            withdrawal = {
                "id": str(wid),
                "date": day.isoformat(),
                "received_at": now_iso(),
                "net_cny": net,
                "gross_cny": gross,
                "reason": payload.get("reason"),
                "immutable": True,
            }
            state["withdrawals"].append(withdrawal)
            self.store.save_client(state)
            return withdrawal

    # ------------------------------------------------------------ 保险赔付

    def register_payout(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            expected = parse_date(str(_require(payload, "expected_date")), "expected_date")
            payout = {
                "id": str(payload.get("id") or f"po-{state['counters']['payout'] + 1}"),
                "amount": _amount(_require(payload, "amount"), "amount"),
                "currency": _currency(payload.get("currency", "CNY")),
                "expected_date": expected.isoformat(),
                "status": "expected",
                "arrival_date": None,
                "arrival_holiday_version_id": None,
                "created_at": now_iso(),
            }
            if any(p["id"] == payout["id"] for p in state["insurance_payouts"]):
                raise ServiceError(f"赔付 id 重复: {payout['id']}")
            state["counters"]["payout"] += 1
            state["insurance_payouts"].append(payout)
            self.store.save_client(state)
            return payout

    def mark_payout_arrived(self, client_id: str, payout_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            payout = next((p for p in state["insurance_payouts"] if p["id"] == payout_id), None)
            if payout is None:
                raise ServiceError(f"赔付不存在: {payout_id}")
            if payout["status"] == "arrived":
                raise ServiceError("赔付已到账，实际到账日不可修改（节假日版本随之固化）")
            arrival = parse_date(str(_require(payload, "arrival_date")), "arrival_date")
            payout["status"] = "arrived"
            payout["arrival_date"] = arrival.isoformat()
            payout["arrival_holiday_version_id"] = payload.get("holiday_version_id")
            payout["arrived_at"] = now_iso()
            self.store.save_client(state)
            return payout

    # ------------------------------------------------------------ 方案

    def _engine_input(self, state: dict[str, Any], assumptions: dict[str, Any]) -> EngineInput:
        return EngineInput(
            client=state["client"],
            accounts=state["accounts"],
            goals=state["goals"],
            valuations=state["valuations"],
            withdrawals=state["withdrawals"],
            payouts=state["insurance_payouts"],
            tax_versions=state["tax_versions"],
            holiday_versions=state["holiday_versions"],
            assumptions=assumptions,
            adopted_tax_versions=state.get("adopted_tax_versions", {}),
            adopted_holiday_version_id=state.get("adopted_holiday_version_id"),
        )

    def create_plan(self, client_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with WRITE_LOCK:
            state = self._load(client_id)
            assumptions = payload.get("assumptions") if isinstance(payload.get("assumptions"), dict) else payload
            self._validate_assumptions(assumptions)
            state["counters"]["plan"] += 1
            pid = str(payload.get("id") or f"plan-{state['counters']['plan']}")
            if any(r["id"] == pid for r in state["plan_refs"]) or self.store.plan_path(pid).exists():
                state["counters"]["plan"] -= 1
                raise ServiceError(f"方案 id 重复: {pid}")
            try:
                plan = build_plan(self._engine_input(state, assumptions), pid, now_iso())
            except PlanError as exc:
                state["counters"]["plan"] -= 1
                raise ServiceError(str(exc)) from exc
            self.store.save_plan(plan)
            state["plan_refs"].append({"id": pid, "created_at": plan["created_at"], "status": "proposed"})
            self.store.save_client(state)
            return plan

    @staticmethod
    def _validate_assumptions(a: dict[str, Any]) -> None:
        if not isinstance(a, dict):
            raise ServiceError("assumptions 必须是对象")
        if "horizon_months" in a:
            try:
                h = int(a["horizon_months"])
            except (TypeError, ValueError) as exc:
                raise ServiceError("horizon_months 必须是整数") from exc
            if not 1 <= h <= 600:
                raise ServiceError("horizon_months 必须在 1..600 之间")
        for key in ("fixed_monthly_withdrawal", "emergency_floor"):
            if key in a and a[key] is not None:
                try:
                    float(a[key])
                except (TypeError, ValueError) as exc:
                    raise ServiceError(f"{key} 必须是数字") from exc
        if "start_month" in a:
            parse_month(str(a["start_month"]))
        for ev in a.get("market_events", []):
            parse_month(str(_require(ev, "month")))
            try:
                hc = float(ev.get("haircut", 0.0))
            except (TypeError, ValueError) as exc:
                raise ServiceError("market_events.haircut 必须是数字") from exc
            if not 0 <= hc < 1:
                raise ServiceError("market_events.haircut 必须在 [0, 1) 区间")
        for ov in a.get("goal_overrides", []):
            _require(ov, "goal_id")
            parse_month(str(_require(ov, "due_month")))

    def review_plan(self, plan_id: str, action: str, note: str | None = None) -> dict[str, Any]:
        """顾问审核：confirm / reject。重启后仍可对待确认方案继续审核。"""
        with WRITE_LOCK:
            try:
                plan = self.store.load_plan(plan_id)
            except KeyError as exc:
                raise ServiceError(str(exc)) from exc
            if action not in ("confirm", "reject"):
                raise ServiceError("action 必须是 confirm 或 reject")
            if plan["status"] != "proposed":
                raise ServiceError(f"方案已审结（{plan['status']}），不能重复审核")
            state = self._load(plan["client_id"])
            new_status = "confirmed" if action == "confirm" else "rejected"
            plan["status"] = new_status
            plan["reviewed_at"] = now_iso()
            plan["review_note"] = note
            plan["explanation"] = plan["explanation"].replace("状态：proposed", f"状态：{new_status}")
            self.store.save_plan(plan)
            for ref in state["plan_refs"]:
                if ref["id"] == plan_id:
                    ref["status"] = new_status
            if action == "confirm":
                state["approved_plan_id"] = plan_id
                # 确认即采用：把本方案快照中的版本固化为客户当前采用版本。
                adopted_tax = state.setdefault("adopted_tax_versions", {})
                for tv in plan["versions_snapshot"]["tax"]:
                    adopted_tax[str(tv["year"])] = tv["id"]
                state["adopted_holiday_version_id"] = plan["versions_snapshot"]["holiday"]["id"]
            self.store.save_client(state)
            return plan

    def get_plan(self, plan_id: str, client_id: str | None = None) -> dict[str, Any]:
        try:
            plan = self.store.load_plan(plan_id)
        except KeyError as exc:
            raise ServiceError(str(exc)) from exc
        if client_id is not None and plan["client_id"] != client_id:
            # 跨客户访问：不泄露存在性。
            raise ServiceError("方案不存在")
        return plan

    def list_plans(self, client_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        result = []
        for pid in self.store.list_plan_ids(client_id):
            plan = self.store.load_plan(pid)
            if status and plan["status"] != status:
                continue
            result.append({
                "id": plan["id"],
                "client_id": plan["client_id"],
                "name": plan["name"],
                "status": plan["status"],
                "reason": plan.get("reason"),
                "created_at": plan["created_at"],
                "exhaustion_month": plan["summary"]["exhaustion_month"],
                "floor_breach_month": plan["summary"]["floor_breach_month"],
            })
        return result

    def compare(self, client_id: str, plan_ids: list[str]) -> dict[str, Any]:
        if not plan_ids:
            raise ServiceError("plan_ids 不能为空")
        plans = [self.get_plan(pid, client_id) for pid in plan_ids]
        return compare_plans(plans)

    def client_summary(self, client_id: str) -> dict[str, Any]:
        state = self._load(client_id)
        approved_id = state.get("approved_plan_id")
        approved = self.store.load_plan(approved_id) if approved_id and self.store.plan_path(approved_id).exists() else None
        return {
            "client": {"id": state["client"]["id"], "name": state["client"]["name"]},
            "accounts": state["accounts"],
            "goals": state["goals"],
            "executed_withdrawal_count": len(state["withdrawals"]),
            "insurance_payouts": state["insurance_payouts"],
            "approved_plan_id": approved_id,
            "approved_summary": approved["summary"] if approved else None,
            "approved_explanation": approved["explanation"] if approved else None,
            "plan_count": len(state["plan_refs"]),
        }

    def portal_summary(self, token: str) -> dict[str, Any]:
        """客户自助视角：只含本人信息、已确认方案的汇总与解释。"""
        state = self.authorize_portal(token)
        summary = self.client_summary(state["client"]["id"])
        return {
            "client": summary["client"],
            "goals": summary["goals"],
            "approved_summary": summary["approved_summary"],
            "approved_explanation": summary["approved_explanation"],
        }

    def portal_plan(self, token: str, plan_id: str) -> dict[str, Any]:
        state = self.authorize_portal(token)
        plan = self.get_plan(plan_id, state["client"]["id"])
        if plan["status"] != "confirmed":
            raise ServiceError("该方案尚未确认，暂不可查看")
        return {
            "id": plan["id"],
            "name": plan["name"],
            "status": plan["status"],
            "summary": plan["summary"],
            "explanation": plan["explanation"],
        }
