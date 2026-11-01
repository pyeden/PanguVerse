# 测试报告「AI 总结」· 开发契约 v3.0

> 诉求：报告详情页加「AI 总结」——总结这次测试的情况，并给开发可执行的建议。
> 现实约束：当前**没有配置 LLM 密钥**（`llm_enabled=false`）。所以必须是「LLM 可选 + 规则兜底」，
> 且界面必须**明确标注**本次总结来自模型还是离线规则，不能假装是 AI 写的。

## 1. 数据与产物

- 缓存：`<workspace>/.panguverse/web/insights/<run_id>.json`（目录 0700、文件 0600、原子写）。
- 失效：报告文件 `mtime` 晚于缓存 → 视为过期；`refresh=true` 强制重算。
- 缓存内容 = §2 的响应体（原样）。

## 2. HTTP API（增量）

### 2.1 读取

`GET /api/runs/{run_id}/insight` → `200`

- 有缓存：`{"available": true, ...payload}`
- 无缓存：`{"available": false}`（**不**自动生成，避免打开页面就烧 token）
- `run_id` 非法 → 400；run 不存在 → 404。

### 2.2 生成

`POST /api/runs/{run_id}/insight` body `{"refresh": false}` → `200` payload

```json
{
  "available": true,
  "run_id": "6673be9f7be3",
  "generated_at": 1793500000.0,
  "source": "llm | rules",
  "model": "deepseek-chat | null",
  "degraded_reason": "未配置 LLM 密钥，使用离线规则总结 | LLM 超时，已降级",
  "headline": "18 个用例 2 通过 / 按场景分组的回归被混在一个池子里执行",
  "summary_md": "## 结论\n...\n## 失败分布\n...\n## 建议\n...",
  "findings": [{"kind": "auth|config|assertion|engine|flaky|mixed_target", "title": "...", "detail": "...", "case_ids": ["..."]}],
  "suggestions": [{"priority": "P0|P1|P2", "title": "...", "detail": "...", "case_ids": ["..."]}],
  "stats": {"total": 18, "passed": 2, "failed": 7, "errored": 9, "skipped": 0, "healed_cases": 12, "duration_ms": 20.69}
}
```

- 生成失败（LLM 异常）→ **仍然 200**，`source="rules"` + `degraded_reason` 写明原因。只有报告读不到才 404/500。
- 单次生成上限 25s；超时按降级处理。

## 3. 规则兜底（没有 LLM 时必须好用）

从 `report.json` 推导，**不要编造**：

1. 结论句：`total/passed/failed/errored/skipped` + 结论（通过/未通过）+ 耗时。
2. 失败归类（按 `failure_kind` / `error.kind` 聚合）：
   - `assertion` → 「断言与预期不符」
   - `engine` + `capture 路径不存在` → 「响应结构与该用例预期不一致」
   - `file_not_found` → 「沙箱文件缺失（检查 FS_ROOT / CREDS_FILE）」
   - 状态码 401/403 → 「认证或权限问题（检查凭据与登录步骤）」
   - 连接失败/超时 → 「目标不可达」
3. `plan.source == "memory"` 且存在失败 → 追加一条「回归库混跑不同被测目标」的建议（**这是本次踩过的坑**）。
4. 建议模板（每条必须带 priority + 可操作动作 + 相关 case_ids），例如：
   - P0「先修配置类失败」：`FS_ROOT`/`CREDS_FILE`/`BASE_URL` 与用例目标不匹配的用例清单。
   - P1「按场景分组回归」：不要用统一 env 回放多目标用例。
   - P2「补强断言」：仅断状态码的用例建议补字段断言（若能识别）。

## 4. LLM 路径

- 复用 `axe/llm.py` 的 `LLMClient`；**不得**在 web 层另起一套 HTTP 调用。
- 输入只放**已打码**的报告摘要（复用 `mask_env_payload` + `core/redact`），体积上限 40 KB；只传：summary、plan.source、失败用例/步骤的 `case/step/message/expected/actual/failure_kind`、env **键名**。
- 明确禁止模型输出密钥；输出必须是 JSON（`complete_json`），字段落 §2 结构；解析失败 → 降级规则。
- 顺手修一个既有缺陷：`axe/llm.py` 的 `urllib.request.urlopen` **没有**遵循「回环直连」规则（本机 ollama 会被系统代理打成 502）。改成与 `http/client.py` 一致的 `ProxyHandler({})` 处理回环。

## 5. 前端

- 报告详情页新增「AI 总结」区块，位置在统计条之下、失败修正面板之上。
- 未生成：显示说明 + 「生成总结」按钮（提交中禁用，显示耗时）。
- 已生成：显示 `headline`、`summary_md`（markdown 渲染，**需转义**，不得执行 HTML/脚本）、`suggestions` 按 priority 分组、`stats` 小表、「复制总结」、「重新生成」。
- 来源标注：`source=llm` → 「AI 模型总结（model）」；`source=rules` → 「离线规则总结」+ `degraded_reason`，视觉上明显区分，禁止把规则结果标成 AI。
- 生成失败/网络错误：显示可读原因与重试，不得静默。

## 6. 验收（A40–A46）

| # | 项 | 判定 |
| --- | --- | --- |
| A40 | `POST .../insight`（无 LLM 密钥） | 200；`source="rules"`；`stats` 与 `report.summary` 完全一致；`degraded_reason` 非空 |
| A41 | 内容质量 | `findings` 覆盖报告里每种失败类型；`suggestions` 至少 1 条 P0 且带 `case_ids`；不得出现未在报告中存在的用例名 |
| A42 | 缓存 | 二次 GET 返回 `available:true` 且 `generated_at` 不变；`refresh:true` 后变化 |
| A43 | 安全 | 响应与缓存文件中 `grep` 真实密码、token = 0；LLM 输入（若可观测）不含 env 值 |
| A44 | LLM 路径 | 无密钥时不得抛 500；用假 client 注入时必须走 `source="llm"` 且字段完整；失败时降级为 rules |
| A45 | 前端 | 详情页可生成/查看/重新生成；规则来源有明确标注；markdown 中的 HTML 被转义；控制台零异常 |
| A46 | 回归 | `pytest -q` 全绿、`ruff` 0；既有 A17–A26/A27–A33 不回归 |

## 7. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/web/insight.py`（新） | `static/app.js` |
| `panguverse/web/api.py`、`service.py` | `static/index.html` |
| `panguverse/axe/llm.py`（回环直连修复） | `static/style.css` |
| `tests/test_web_insight.py`（新）、`docs/web-api.md` | |

## 8. 明确不做

多轮对话式追问、团队共享总结、跨运行趋势对比、自动生成修复补丁。
