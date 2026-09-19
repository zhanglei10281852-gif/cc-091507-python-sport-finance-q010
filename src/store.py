"""文件持久化：JSON 状态库，进程内锁保证写入原子性与幂等。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class Store:
    """以单个 JSON 文件保存全部状态。

    重启后状态保留；写盘采用临时文件原子替换。所有变更通过
    ``mutate`` 串行化，保证 HTTP 并发下的一致性。
    """

    def __init__(self, runtime_dir: Path) -> None:
        self._dir = runtime_dir
        self._path = runtime_dir / "state.json"
        self._lock = threading.RLock()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            with self._path.open(encoding="utf-8") as fh:
                return json.load(fh)
        return {
            "schema": 1,
            "users": {},        # client_id -> {id, name, pin}
            "snapshots": {},    # source_key -> {snapshot...}（幂等去重）
            "plans": {},        # plan_id -> {plan...}
            "withdrawals": {},  # withdrawal_id -> {withdrawal...}
            "events": [],       # 领域事件（追加）
        }

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self._path)

    # ---- 读取 ----
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock:
            plan = self._state["plans"].get(plan_id)
            return json.loads(json.dumps(plan)) if plan else None

    # ---- 变更 ----
    def mutate(self, fn) -> Any:
        """在锁内执行 ``fn(state)`` 并原子落盘；返回 fn 的结果。"""
        with self._lock:
            result = fn(self._state)
            self._flush()
            return result
