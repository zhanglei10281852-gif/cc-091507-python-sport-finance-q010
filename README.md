# 家庭健康账户长期提领规划

面向家庭长期投资、健身康复目标和安全提领计划的 Python 后端服务（仅标准库）。

服务把账户持仓、预期收益、训练/康复目标、保险赔付、跨年度税费和紧急备用金放入
可调整的月度现金流模型，支持多情景比较、已执行提领锁定、客户审批与文件持久化。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

默认监听 `8000` 端口，状态文件写入 `.runtime/state.json`（可用 `RUNTIME_DIR` 覆盖）。
顾问密钥通过 `ADVISOR_KEY` 配置（默认 `advisor-key`，仅用于本地开发）。

```bash
python3 -m unittest discover -s tests   # 14 个测试
docker compose up --build               # 容器方式
```

## 鉴权

| 角色 | 请求头 |
| --- | --- |
| 顾问 | `X-Advisor-Key: <密钥>` |
| 客户 | `X-Client-Id` + `X-Client-Pin`（由顾问开户时设置） |

客户只能查看/审批自己的方案，且响应只含**汇总与解释**，不含逐月明细。

## 接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 公开 | 健康检查 |
| POST | `/clients` | 顾问 | 开户 `{client_id,name,pin}` |
| POST | `/valuations` | 顾问 | 导入估值快照；相同 `source_key` 幂等，不产生重复快照 |
| GET | `/valuations` | 顾问 | 快照列表 |
| POST | `/tax-tables` | 顾问 | 注册跨年度税率版本 `{version, table:{"2027":0.10,...}}` |
| POST | `/holidays` | 顾问 | 注册节假日版本（到账日遇周末/节假日顺延） |
| POST | `/plans` | 顾问 | 建方案，立即生成 `baseline` 情景 |
| GET | `/plans`、`/plans/{id}` | 客户/顾问 | 列表与详情（顾问含 `monthly` 明细） |
| POST | `/plans/{id}/scenarios` | 顾问 | 新情景：`market_down` / `goal_advance` / `payout_arrival` |
| POST | `/plans/{id}/recompute` | 顾问 | 事实变化后按安全底线重算后续提领 |
| POST | `/plans/{id}/withdrawals` | 顾问 | 登记已执行提领/赔付（锁定，自动重算） |
| POST | `/plans/{id}/compare` | 客户/顾问 | 比较各情景的耗尽月份、目标覆盖率、延后项目 |
| POST | `/plans/{id}/decision` | 客户 | `{decision:"approved"|"rejected"}` |

## 模型规则

- **安全底线（floor，医疗应急金）为硬约束**：任何固定/目标提领后余额不得低于底线；
  固定提领首次无法足额支付的月份即**资金耗尽月份**。
- **固定提领与医疗储备冲突**时，优先保住底线，当月提领记 `fixed_shortfall`。
- 目标按 `priority`、`cost` 排序支付，余额不足则**逐月顺延**（`deferred` /
  `funded_late`），并支持康复费用**分批筹资**。
- 市场下跌通过某月收益率冲击模拟；目标提前、赔付到账均可独立成情景。
- **已执行提领不可被新假设覆盖**：`as_of_month`（含）之前为锁定区间，
  按实际事实回放；重算只影响之后的投影。
- **跨年度税率与节假日版本**在情景中冻结为「采用版本」：即使后来注册了
  新版本，既有情景仍保留原采用的税率表与到账推算。
- 状态持久化到 `.runtime/state.json`，**服务重启后**待确认方案仍可继续审核。
