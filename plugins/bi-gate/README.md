# bi-gate — 系统 B 的门禁

在 `query_metric` 派发之前做一轮确定性校验，不通过就拦下并落审计。

## 它拦什么

| 拒因 | 判定 |
|---|---|
| `rejected_unknown_metric` | 指标不在受控事实层 |
| `rejected_bad_param` | 维度不是该指标声明过的 |
| `rejected_no_time_window` | 缺时间窗，或时间窗不是绝对区间 |
| `rejected_scan` | EXPLAIN 预估扫描量超过该指标上限 |

时间窗只收绝对区间（`2026-08-01`），不收 `最近七天` 这类相对表述。相对时间必须在调用前解析成具体日期，否则同一个问题在不同时刻问会得到不同的数，评估集就无法回归。

## 它明确不拦什么

**行列级权限（ACL）不在这一层。** 谁能看哪些行、哪些列，必须由数据层的独立库账号与行级权限保证。放在 agent 层的权限本质上是提示词级约束——绕过一个 hook 就没了。本插件在理由里可以提示权限问题，但它不是防线。

`run_sql` 的降级路径也不在本插件范围内，那套围栏另做。

## 为什么是插件

仓库是 `nousresearch/hermes-agent` 的 fork，上游非常活跃。门禁只用 `pre_tool_call` / `post_tool_call` 两个既有扩展点，核心文件一行不动，同步上游时不会冲突。

## 拒绝理由为什么带来源

每条拒绝都以「BI 门禁（bi-gate 插件，在调用发出前拦截）」开头。

实测依据：理由里只写"命中规则"时，模型会自行编造归因——两次实验里它分别把 harness 的拦截说成「本地代理的安全策略」和「远端服务检测到」，最后给用户一个错误的解释。见《评估与 Reward v0.1》§2.4。

## 配置

指标注册表路径由环境变量给出：

```bash
export BI_GATE_REGISTRY=/path/to/registry.json
```

格式见 `registry.example.json`。字段只有门禁需要的那几项——口径描述、责任人、新鲜度在指标层维护，不在这里。

**载入失败时按空表处理，即所有 `query_metric` 调用都被拦截。** 这是有意的 fail-closed：门禁配置坏掉时应该停摆，而不是放行。

## 现状与缺口

- 扫描量预检的 `estimated_rows` 目前恒为 `None`（即该条恒放行）。执行层接上 EXPLAIN 后传入即可，`rules.check_scan_budget` 已经写好。
- 审计当前只写结构化日志。接 `ai_cs.agent_audit` 时替换 `_audit` 的实现，调用点不用改。
- 注册表从 JSON 文件读。指标层稳定后应改为从指标服务拉取并带版本号，否则无法回答"这次判定用的是哪一版口径"。

## 测试

```bash
pytest tests/plugins/test_bi_gate.py
```
