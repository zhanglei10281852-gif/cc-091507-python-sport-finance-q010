# 家庭健康账户长期提领规划

面向家庭长期投资、健身康复目标和安全提领计划的 Python 后端服务（仅标准库，无第三方依赖）。

## 运行与测试

```bash
python3 src/index.py                 # 默认 0.0.0.0:8000，数据写入 .runtime/
RUNTIME_DIR=/data python3 src/index.py
python3 -m unittest discover -s tests
docker compose up --build
```

访问 `GET /health` 确认进程状态。

## 模型规则

- **月度网格**：账户按估值快照折算 CNY（`fx_rates`），按账户权重混合预期年化收益换算月收益。
- **安全底线**：取 `emergency_reserve` 目标合计或显式 `emergency_floor`。固定提领与必办
  （康复/手术）目标即使击穿底线也执行；可延后目标在出资后余额低于底线时顺延。
- **重算触发**：市场下跌（`market_events[].haircut`）、目标提前/推后（`goal_overrides`）、
  赔付到账后，基于 `base_plan_id` 生成新方案；输出资金耗尽月份、首次击穿底线月份、
  每目标覆盖率与被延后项目。
- **已执行提领不可覆盖**：记录（net/gross/日期）是不可变事实；规划区间内的已结账月份
  以 `settled=true` 按原始 gross 回放，新税率只作用于其后的未结账月份。
- **版本留存**：税率按年度、节假日按版本只追加；方案快照写入实际采用版本。新方案可选用
  新版本并附变更告警，已审结方案永不改写。赔付未到账时按采用节假日表做「下一工作日」顺延，
  到账后实际日期固化。
- **估值幂等**：同日 + 同 `source_ref` + 同持仓内容（SHA-256）只产生一份快照。
- **客户隔离**：客户文件按 id 物理隔离，门户凭一次性 `view_token` 只能看到本人已确认方案的
  汇总与解释。
- **重启续审**：全部状态在 `.runtime/` 原子落盘，进程重启后可继续审核 `proposed` 方案。

## 主要接口（JSON）

顾问侧：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/clients` | 建客户，返回一次性 `view_token` |
| POST | `/clients/{id}/accounts` `/goals` `/tax-versions` `/holiday-versions` | 登记基础数据 |
| POST | `/clients/{id}/valuations` | 导入估值（重复内容返回 `deduplicated:true`） |
| POST | `/clients/{id}/withdrawals` | 登记已执行提领（不可变） |
| POST | `/clients/{id}/payouts` | 登记预期保险赔付 |
| POST | `/clients/{id}/payouts/{pid}/arrived` | 标记赔付实际到账（仅一次） |
| POST | `/clients/{id}/plans` | 用 `assumptions` 生成方案（可带 `base_plan_id`/`reason`） |
| POST | `/clients/{id}/compare` | `{plan_ids:[...]}` 对比耗尽月份、覆盖率、延后项 |
| POST | `/plans/{id}/review` | `{action:"confirm"|"reject"}`，重启后仍可审核待确认方案 |
| GET | `/clients/{id}` `/clients/{id}/plans` `/plans/{id}` | 查询 |

客户侧：`GET /portal?token=...`、`GET /portal/plans/{id}?token=...`（仅已确认方案）。

## 示例

```bash
curl -s -X POST localhost:8000/clients/fam1/plans -H 'Content-Type: application/json' -d '{
  "assumptions": {
    "name": "市场下跌重算",
    "start_month": "2026-10",
    "horizon_months": 120,
    "fixed_monthly_withdrawal": 3000,
    "market_events": [{"month": "2027-02", "haircut": 0.3}],
    "goal_overrides": [{"goal_id": "travel", "due_month": "2027-06"}],
    "tax_version_ids": {"2028": "TAX_2028_NEW"},
    "base_plan_id": "plan-1",
    "reason": "市场下跌且赛事提前，按安全底线重算后续提领"
  }
}'
```
