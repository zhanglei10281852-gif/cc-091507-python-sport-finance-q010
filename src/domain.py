"""领域常量与时间工具。

- 事件类型、目标类型、币种来自 reference/domain.json（公开参考资料）。
- 模型使用月度网格，所有日期区分业务时间（business date）与实际到账/接收时间。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

_REFERENCE = Path(__file__).resolve().parents[1] / "reference" / "domain.json"
try:
    _REFERENCE_DATA = json.loads(_REFERENCE.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):  # pragma: no cover - 参考资料随仓库分发
    _REFERENCE_DATA = {}

EVENT_TYPES: list[str] = _REFERENCE_DATA.get(
    "event_types",
    ["valuation", "withdrawal", "insurance_payout", "tax_change", "goal_defer", "scenario_update"],
)
GOAL_TYPES: list[str] = _REFERENCE_DATA.get(
    "goal_types", ["fitness", "rehabilitation", "travel", "emergency_reserve"]
)
CURRENCIES: list[str] = _REFERENCE_DATA.get("currencies", ["CNY", "HKD", "USD"])
QUANTITY_PRECISION: int = _REFERENCE_DATA.get("quantity_precision", 6)

MONEY_EPS = 0.005
EMERGENCY_RESERVE = "emergency_reserve"


def today() -> date:
    return datetime.now().date()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def parse_date(value: str, field: str = "date") -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def parse_month(value: str, field: str = "month") -> tuple[int, int]:
    try:
        year, month = value.split("-")
        result = (int(year), int(month))
        if not 1 <= result[1] <= 12 or len(year) != 4:
            raise ValueError
        return result
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM 月份") from exc


def month_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def add_months(value: str, delta: int) -> str:
    year, month = parse_month(value)
    index = year * 12 + (month - 1) + delta
    return month_str(index // 12, index % 12 + 1)


def month_index(value: str) -> int:
    year, month = parse_month(value)
    return year * 12 + (month - 1)


def max_month(a: str, b: str) -> str:
    return a if month_index(a) >= month_index(b) else b


def month_of_date(value: date) -> str:
    return month_str(value.year, value.month)


def year_of_month(value: str) -> int:
    return parse_month(value)[0]


def next_business_day(day: date, holidays: frozenset[date]) -> date:
    """按「下一工作日」规则顺延：周末或节假日向后一天，直到工作日。

    节假日集合来自某个已采用的假设版本，版本留存后不再被重写。
    """
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return day


def money(value: float) -> float:
    return round(float(value) + 0.0, 2)
