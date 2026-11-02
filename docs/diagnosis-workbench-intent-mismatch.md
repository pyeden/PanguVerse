# 工作台实测诊断：意图 → 原子组合 不准确、调试无进度、失败跳过过多

> 触发：用真实任务「打开 https://seres-ai.com 首页，获取文本内容，给出页面布局报告」跑工作台，结果完全不可用。
> 本文是**代码级根因分析 + 整改方案**，不含代码改动。

---

## 1. 复现（我按你的描述原样跑了一遍）

输入意图：`打开 https://seres-ai.com 网站首页，获取首页的文本内容，给出首页的页面布局报告`
领域包：`api_testing`

实际产出：`w-e08be629` rev0，**20 节点 / 25 边，validation.ok = true**。

节点序列（节选）：

```
http_request  health
assert_json   assert_status / assert_service
assert_latency
http_request  登录获取 token
assert_json   assert_token
http_request  me
http_request  create_order → assert_created → assert_amount
http_request  get_order → assert_same_order
http_request  pay_order → assert_paid → assert_paid_at
http_request  list_paid → assert_list
assert_schema
```

**这是内置的「订单闭环」演示模板**，与"打开首页、取文本、给布局报告"毫无关系。
而 `validation.ok=true`，界面会显示"校验通过"——这是最危险的地方：**错误的计划被当成合法的计划**。

---

## 2. 根因（四条，逐条有证据）

### R1. 规划器静默降级成"关键词模板"，且不报告自己是模板

- 当前无 LLM 密钥时走规则模板；模板匹配只看零散关键词（"获取"/"报告"），不做领域+动作对齐。
- 草稿响应体只有 `{wid, rev, state, graph, validation}`——**没有 `planner` / `plan_source` / `confidence` 字段**，界面无从知道"这是 AI 规划的，还是关键词模板凑的"。
- 因此：意图不匹配时，模板照样产出 20 个节点，用户以为 AI 理解错了，其实是**规划器根本没说自己不理解**。

### R2. 校验器是"结构校验"，不是"语义校验"

- 现有校验只检查：原子是否存在、必填端口是否齐全、变量是否未定义、是否有环。
- 它不检查：**这个节点和用户意图有没有关系**；**需要的环境变量是否具备**；**意图里要求的动作在原子库里到底有没有对应原子**。
- 结果：一张与意图无关的图，结构完全合法 → `ok=true` → 发布门禁形同虚设。

### R3. 原子库缺少这类任务的基本原语（这是最硬的约束）

当前 24 个原子的全集：

```
assert_body_contains, assert_header, assert_latency, assert_status, header_get,
http_request, assert_json, assert_schema, json_get, regex_extract, set_var, fail, log, sleep,
assert_file_contains, assert_file_exists, fs_copy, fs_delete, fs_exists, fs_list,
fs_read_json, fs_read_text, fs_write_json, fs_write_text
```

**没有**：抓取网页正文、把 HTML 转成结构化（标题/段落/链接）、让 LLM 总结成报告。
也就是说：**即使规划器完全正确，这个任务在当前原子库里也无法表达**。它必须"编"一个能跑的东西出来——于是就编成了订单闭环。

### R4. 调试没有"过程"，只有"结果"；跳过与失败没有归因

- `POST /debug` 是**同步返回整段 trace**，前端拿不到"正在跑第几个节点"，所以没有进度条、没有节点高亮、没有逐节点点亮的效果。
- 跳过（`when=false` / `foreach` 空 / `optional` / 上游失败）在 trace 里只是一个 `skipped` 状态，**不记录原因**；失败同理只有 message。
- 于是用户看到"失败 9、跳过 5"，但不知道**为什么跳**、**跳的是不是关键路径**，自然觉得"任务难以完成"。

---

## 3. 整改方案（按优先级，每一项都可验收）

### P0-1 规划器必须"诚实"：不理解就说不理解

- 草稿响应新增：`planner`（`llm|rules|template`）、`plan_source`、`confidence`（0–1）、`assumptions[]`、`capability_gaps[]`。
- 规则模板匹配改为**领域 + 动作双重匹配**；匹配不上时**不产出图**，返回：
  ```json
  422 {"error":{"code":"capability_gap","message":"当前原子库无法表达该意图",
        "missing_atoms":["http_get_page","html_extract_structure","llm_summarize"],
        "suggestions":["安装领域包 web_research","或改用 intent 拆解为多个子任务"]}}
  ```
- 界面在画布顶部常驻一条来源横幅：`AI 规划（置信度 0.82）` / `规则模板（低置信度，请人工核对）`。
- **验收**：同一个意图在原子不足时必须返回 422 并列出缺失原子，而不是产出 20 个无关节点。

### P0-2 校验升级为"语义 + 就绪度"校验

新增三类检查（都可关闭但默认开启）：

| 检查 | 规则 |
| --- | --- |
| 意图覆盖 | 每个节点必须能映射到意图中的动作/实体关键词，或被显式标 `helper:true`；覆盖率 < 60% 时 `ok=false` |
| 环境就绪 | 计划里引用的 `${env.X}` 必须在项目 env 或假设清单中存在；缺失列出 `missing_env[]` |
| 能力缺口 | 意图要求的动作（如"取文本""总结"）必须能对应到原子；对应不上 → `capability_gaps[]` 并阻止发布 |

- **验收**：错误计划（订单模板跑首页任务）必须 `ok=false` 且给出"覆盖率 0/20、缺少 html 提取原子"。

### P0-3 补齐领域包原子（让这个任务能真的做）

在 `api_testing` 之外新增 `web_research` 领域包（或并入现有包），至少四个原子：

| 原子 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| `http_get_page` | url | status/html/text/final_url | GET + 按 charset 正确解码（复用已修的 charset 逻辑） |
| `html_extract_text` | html | text | 去标签、压缩空白、保留段落 |
| `html_extract_structure` | html | title/headings[]/links[]/sections[] | 标题层级、导航链接、区块计数 |
| `llm_summarize` | text, instruction | summary_md | 走 `LLMClient`，`needs_llm` 能力；无密钥时明确失败而不是静默跳过 |

- **验收**：意图「打开 X 首页取文本并给布局报告」→ 图应为 4–6 个节点（`http_get_page → html_extract_structure → llm_summarize`），不再是订单模板。

### P0-4 调试要有"过程感"

- 调试改为**异步会话**：`POST /debug` 返回 `session_id`，`GET /debug/{sid}` 增量返回 `{nodes:[{node_id,status,duration_ms}], cursor, done}`。
- 前端：节点卡按状态染色（运行中脉冲、通过绿、失败红、跳过灰），顶部进度条 `n/total`，时间线自动滚动到当前节点。
- 跳过/失败都要带**原因码**：`when_false | foreach_empty | upstream_failed | optional | assertion | network | missing_env`。
- **验收**：调试时用户能看到"正在跑第 7/16 个节点"，且被跳过的节点点开能看到原因。

### P0-5 失败/跳过要有统计与策略

- 调试面板顶部：`通过 N / 失败 M / 跳过 K（原因分布）`。
- 新增"仅跑关键路径"开关：跳过 `optional` 节点，先验证主干。
- 节点级 `continue_on_failure` 必须在画布上可见（徽标），不许隐式继承。

---

## 4. 需要你拍板

1. **原子扩展先行还是规划器先行**？我建议**先 P0-3（原子）**——没有原语，规划器再诚实也只能报错；原子到位后，规划质量才有意义。
2. **`web_research` 独立领域包还是并入 `api_testing`**？我倾向独立包：网页研究 ≠ API 测试，正好验证"领域包"这套抽象。
3. `llm_summarize` 在无密钥时应**硬失败**还是**产出一个"未总结"的占位**？我倾向硬失败并明确提示，避免又一次"看起来完成了"。

---

## 5. 附：本次实测的原始证据

- 草稿：`w-e08be629`，20 节点/25 边，`validation.ok=true`，响应无规划来源字段。
- 原子库：24 个，无 html/text/llm 相关原子（只有 `fs_read_text` 这类本地文件原子）。
- 复现命令：`POST /api/studio/drafts {"intent":"打开 https://seres-ai.com 网站首页，获取首页的文本内容，给出首页的页面布局报告","domain_pack":"api_testing"}`。
