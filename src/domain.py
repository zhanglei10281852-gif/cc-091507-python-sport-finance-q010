"""领域常量与时间工具。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT / "reference" / "domain.json"


def load_reference() -> dict:
    with REFERENCE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


REFERENCE = load_reference()
EVENT_TYPES = REFERENCE["event_types"]
GOAL_TYPES = REFERENCE["goal_types"]
CURRENCIES = set(REFERENCE["currencies"])
QUANTITY_PRECISION = REFERENCE["quantity_precision"]

# 提领用途
PURPOSE_FIXED = "fixed"
PURPOSE_GOAL = "goal"

# 计划状态
PLAN_PENDING = "pending"
PLAN_ACTIVE = "active"
PLAN_REJECTED = "rejected"

# 目标状态
GOAL_FUNDED = "funded"            # 到期月内足额
GOAL_FUNDED_LATE = "funded_late"  # 延后但最终足额
GOAL_DEFERRED = "deferred"        # 期内未能足额

MONEY_EPS = 1e-9


def parse_month(value: str) -> date:
    """接受 'YYYY-MM' 或 'YYYY-MM-DD'，返回当月 1 号。"""
    parts = value.split("-")
    if len(parts) not in (2, 3):
        raise ValueError(f"月份格式错误: {value!r}，应为 YYYY-MM")
    try:
        year, month = int(parts[0]), int(parts[1])
        return date(year, month, 1)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"月份格式错误: {value!r}") from exc


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日期格式错误: {value!r}，应为 YYYY-MM-DD") from exc


def month_str(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def adjust_business_day(d: date, holidays: set[str]) -> date:
    """节假日/周末到账顺延到下一个工作日。"""
    cur = d
    while cur.weekday() >= 5 or cur.isoformat() in holidays:
        cur += timedelta(days=1)
    return cur


def arrival_month(expected_date: date, holidays: set[str]) -> tuple[date, str]:
    """返回（实际到账日, 到账月份）。采用的节假日版本由调用方记录。"""
    settled = adjust_business_day(expected_date, holidays)
    return settled, month_str(settled)
