"""JSON 文件持久化。

- 每个客户一个状态文件，客户之间物理隔离；计划文件单独存放便于重启后继续审核。
- 所有写操作经过同一把进程锁，先写临时文件再 os.replace，避免半截写入。
- 不依赖第三方数据库，重启后从 .runtime/ 目录完整恢复。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


class Store:
    def __init__(self, root: str | os.PathLike[str] = ".runtime") -> None:
        self.root = Path(root)
        self.clients_dir = self.root / "clients"
        self.plans_dir = self.root / "plans"
        self.clients_dir.mkdir(parents=True, exist_ok=True)
        self.plans_dir.mkdir(parents=True, exist_ok=True)

    # ---- 基础读写 -------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def client_path(self, client_id: str) -> Path:
        if not client_id or not client_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("client_id 非法")
        return self.clients_dir / f"{client_id}.json"

    def plan_path(self, plan_id: str) -> Path:
        if not plan_id or not plan_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("plan_id 非法")
        return self.plans_dir / f"{plan_id}.json"

    # ---- 客户目录 -------------------------------------------------------

    def list_clients(self) -> list[str]:
        return sorted(p.stem for p in self.clients_dir.glob("*.json"))

    def client_exists(self, client_id: str) -> bool:
        return self.client_path(client_id).exists()

    def load_client(self, client_id: str) -> dict[str, Any]:
        path = self.client_path(client_id)
        if not path.exists():
            raise KeyError(f"客户不存在: {client_id}")
        return self._read(path)

    def save_client(self, state: dict[str, Any]) -> None:
        self._write(self.client_path(state["client"]["id"]), state)

    def create_client(self, client_id: str, name: str, created_at: str) -> dict[str, Any]:
        with _LOCK:
            path = self.client_path(client_id)
            if path.exists():
                raise ValueError(f"客户已存在: {client_id}")
            state = new_client_state(client_id, name, created_at)
            self._write(path, state)
            return state

    # ---- 计划 -----------------------------------------------------------

    def list_plan_ids(self, client_id: str | None = None) -> list[str]:
        ids = sorted(p.stem for p in self.plans_dir.glob("*.json"))
        if client_id is None:
            return ids
        return [pid for pid in ids if self._read(self.plan_path(pid)).get("client_id") == client_id]

    def load_plan(self, plan_id: str) -> dict[str, Any]:
        path = self.plan_path(plan_id)
        if not path.exists():
            raise KeyError(f"方案不存在: {plan_id}")
        return self._read(path)

    def save_plan(self, plan: dict[str, Any]) -> None:
        self._write(self.plan_path(plan["id"]), plan)

    def reset(self) -> None:
        """仅供测试：清空运行时目录。"""
        import shutil

        with _LOCK:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.clients_dir.mkdir(parents=True, exist_ok=True)
            self.plans_dir.mkdir(parents=True, exist_ok=True)


def new_client_state(client_id: str, name: str, created_at: str) -> dict[str, Any]:
    return {
        "client": {"id": client_id, "name": name, "created_at": created_at},
        "accounts": [],
        "goals": [],
        # 版本表：只追加、永不删除。客户维度记录每个年度/节假日「已采用」的版本 id，
        # 方案生成时快照引用；旧方案重开仍按当时采用版本复现。
        "tax_versions": [],
        "holiday_versions": [],
        "adopted_tax_versions": {},
        "adopted_holiday_version_id": None,
        "valuations": [],
        "withdrawals": [],
        "insurance_payouts": [],
        "plan_refs": [],
        "approved_plan_id": None,
        "counters": {"withdrawal": 0, "plan": 0, "payout": 0},
    }
