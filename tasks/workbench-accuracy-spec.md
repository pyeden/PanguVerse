# 工作台准确性整改 · 实现契约 v6.0

> 依据 `docs/diagnosis-workbench-intent-mismatch.md`（`220eaf2`）。
> 已拍板：**先补原子**（P0-3）→ 再规划器诚实（P0-1）→ 语义校验（P0-2）→ 调试过程（P0-4）→ 失败归因（P0-5）。
> 领域包：`web_research` **独立成包**。`llm_summarize` 无密钥 **硬失败**，不产出占位。

---

## 1. 新领域包 `web_research`

位置 `panguverse/packs/web_research.py` + 注册到 registry（与 api_testing / local_fs 同级）。

| 原子 | 输入 | 输出 | 语义要求 |
| --- | --- | --- | --- |
| `http_get_page` | `url`（必填）、`headers?`、`timeout?` | `status`、`html`、`text`、`final_url`、`charset` | 按响应 `charset` 解码（缺省 utf-8，再退 `apparent` 兜底）；4xx/5xx 不抛异常，返回 status；连接失败 → `failure_kind=network` |
| `html_extract_text` | `html` | `text`、`chars` | 去 script/style、去标签、压缩空白、保留段落换行；空文档返回空串而非报错 |
| `html_extract_structure` | `html` | `title`、`headings[]`、`links[]`、`sections[]`、`counts{}` | `headings` 含 level/text；`links` 含 href/text/internal(bool)；`sections` 为顶层区块计数摘要 |
| `llm_summarize` | `text`、`instruction`、`max_chars?` | `summary_md`、`model` | **必须走 `LLMClient`**；无密钥 → `failure_kind=missing_llm`，错误文案明确「未配置 LLM，无法生成总结」；不写占位 |

约束：
- 全部只读（无 `mutate`/`destructive`）；`llm_summarize` 声明能力 `needs_llm`。
- 输出密钥类字段无；所有输出可进报告。
- 解析失败给 `parse` 失败类，不抛未捕获异常。

## 2. 规划器诚实（P0-1）

草稿响应新增字段（additive）：

```json
{"wid":"w-…","rev":0,"state":"draft",
 "planner":"llm|rules|template","plan_source":"llm|rules|template",
 "confidence":0.0,"assumptions":["需要 BASE_URL"],"capability_gaps":[],
 "graph":{…},"validation":{…}}
```

规则：
1. 规则/模板匹配必须**领域 + 动作**双重命中（如 `web_research` + "取首页文本/布局报告"）；仅命中零散词不算。
2. 命中失败**不产出图**，返回 `422 {"error":{"code":"capability_gap","message":…,"missing_atoms":[…],"suggestions":[…]}}`。
3. LLM 规划：提示词必须带**原子清单 + 端口契约**，禁止发明原子；输出后**逐节点校验原子存在**，不存在则视为 `capability_gap` 而不是丢弃节点。
4. `confidence`：LLM 路径取模型自评或按覆盖率估算；规则/模板固定 ≤0.5 并在界面标注「低置信度」。
5. `assumptions`：计划依赖但未提供的 env（如 `BASE_URL`）必须列出，供前端要求确认。

## 3. 语义校验（P0-2）

`POST /api/studio/{wid}/validate` 增加三类检查（默认开启，可在请求里关闭）：

| 检查 | 规则 | 错误码 |
| --- | --- | --- |
| 意图覆盖 | 每个节点须映射到意图动作/实体词，或显式 `helper:true`；覆盖率 <60% → error | `intent_mismatch` |
| 环境就绪 | 计划引用的 `${env.X}` 必须在项目 env / 假设清单中 | `missing_env` |
| 能力缺口 | 意图动作无法对应到原子 | `capability_gap` |

- `validation` 响应加 `coverage: {matched,total,ratio}`、`missing_env:[]`、`capability_gaps:[]`。
- **阻塞发布**：存在 `intent_mismatch` / `capability_gap` 时 `publish` 返回 409 并回吐原因。

## 4. 异步调试（P0-4）

- `POST /api/studio/{wid}/debug` → `202 {session_id}`（不再同步返回整段 trace）。
- `GET /api/studio/debug/{session_id}` → `{nodes:[{node_id,status,reason,attempts,duration_ms,error,inputs,outputs}],cursor,done,summary:{passed,failed,skipped,by_reason{}}}`；支持 `?cursor=` 增量。
- 会话落盘 `<ws>/.panguverse/studio/<wid>/debug/<sid>.jsonl`，0600；**不进 `/api/runs`**。
- 节点状态：`pending|running|passed|failed|skipped`；`reason` 取值：
  `when_false|foreach_empty|upstream_failed|optional|assertion|network|missing_env|missing_llm|cancelled`。
- 仍只允许项目已配置环境；URL 覆盖 400。

## 5. 前端（ui）

1. 画布顶部**来源横幅**：`AI 规划 · 置信度 0.82` / `规则模板 · 低置信度，请人工核对`；`capability_gaps` 非空时红条列出缺失原子与建议。
2. 调试抽屉改**轮询会话**：进度条 `n/total`、节点状态染色（running 脉冲 / passed 绿 / failed 红 / skipped 灰）、时间线自动跟随、点开看 reason 与 inputs/outputs。
3. 顶部统计：`通过 N / 失败 M / 跳过 K`，跳过按原因分组（可展开）。
4. 画布节点卡实时反映调试状态（与抽屉同步高亮）。
5. 校验不通过时，问题列表要能点击定位节点，并显示覆盖率 `x/y`。

## 6. 验收（T1–T7 + R）

| # | 项 | 判定 |
| --- | --- | --- |
| T1 | 首页任务能正确表达 | 意图「打开 https://seres-ai.com 首页取文本并给布局报告」→ 图节点数 **4–6**，含 `http_get_page`、`html_extract_structure`、`llm_summarize`；不含 `create_order/pay_order` 之类 |
| T2 | 不匹配时不瞎编 | 原子不足的意图 → `422 capability_gap` + `missing_atoms[]`；**不产出图** |
| T3 | 语义校验拦住错图 | 用旧订单模板图跑首页意图 → `validation.ok=false`，`intent_mismatch` + `coverage.ratio` 低；publish 409 |
| T4 | 调试有过程 | `POST /debug` 202 → 轮询能看到节点逐个 `running→passed/failed`，`cursor` 递增；前端有进度与高亮 |
| T5 | 跳过有原因 | 人为构造 `when=false` 节点 → `skipped` + `reason=when_false`；上游失败 → `upstream_failed` |
| T6 | 无密钥硬失败 | `llm_summarize` 无 LLM → `failed` + `missing_llm`，**不产出占位**；报告结论为未通过 |
| T7 | 真实闭环 | 用 T1 的图跑一次（有 LLM）→ 生成 `summary_md` 且报告可通过；无 LLM 时明确失败 |
| R | 回归 | `pytest` 全绿、`ruff` 0；A17–A62、W1–W9 既有行为不变；`npm run build` 无 diff |

## 7. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/packs/web_research.py`、`panguverse/web/planner_gate.py`（新，诚实规划+能力缺口） | `webui/src/**` |
| `panguverse/web/studio.py`、`service.py`、`api.py`、`axe/planner.py`（提示词与校验） | `panguverse/web/static/**`（产物） |
| `tests/test_web_research_pack.py`、`tests/test_web_planner_gate.py` | |
| `docs/web-api.md` | |
