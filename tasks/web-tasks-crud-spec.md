# 任务管理（CRUD）+ 从历史修正重跑 · 开发契约 v2.0

> 用户诉求：**运行的任务能增删改查**；**从运行历史点进去，按错误信息改配置再跑**。
> 前置：`tasks/web-app-mvp-spec.md`（v1.4）已交付并验收通过；本文只描述增量。
​​> 修订：v2.0.3（2026-10-31）明确「仅保存配置」在 201 后允许 PATCH 叠加表单修改
> 修订：v2.0.2（2026-10-31）新增 `save_as_task`（从历史保存不发运行），修「仅保存丢密钥」陷阱
> 修订：v2.0.1（2026-10-31）任务落盘改为 `.panguverse/tasks/`（QA F3 文档对齐）

---

## 1. 为什么需要（一句话）

现在每次运行都得重新填表单，失败之后想改一个 `BASE_URL` 或账号，只能从头再填一次；
运行历史是只读的，看完错误就走人了。**要让它变成"改一改再来一次"的工作流。**

## 2. 数据模型：任务（Task Def）

落盘：`<workspace>/.panguverse/tasks/<task_id>.json`，目录 `0700`、文件 `0600`（**不放 `<workspace>/tasks/`**：那是仓库放契约文档的目录，会撞名且把含明文 env 的文件变成 untracked）（含 env 明文，与 `last_env.json` 同策略）。

```json
{
  "task_id": "t-8f3a1c",
  "name": "订单闭环冒烟",
  "mode": "scenario",
  "scenario": "scenarios/api_smoke.yaml",
  "intent": "",
  "env": {"BASE_URL": "http://127.0.0.1:8899", "API_PASSWORD": "demo123"},
  "regression": false,
  "no_llm": false,
  "created_at": 1793376211.1,
  "updated_at": 1793376211.1,
  "last_run_id": "d3372e77b85b",
  "last_status": "done"
}
```

规则：

- `task_id` 服务端生成，格式 `^t-[0-9a-f]{6,12}$`（不接受客户端指定）。
- `name` 必填、trim 后 1–80 字符；同名允许（用 id 区分）。
- `mode` 必须属于 `demo|scenario|intent|regression`；`mode=scenario` 时 `scenario` 必填且经 realpath 前缀校验；`mode=intent` 时 `intent` 非空。
- `env` 为 `string → (string|number|bool)`。
- **运行不改任务定义**，只在结束后回写 `last_run_id` / `last_status`。

## 3. HTTP API（增量）

### 3.1 列表 / 详情

- `GET /api/tasks` → `{"tasks": [<task 摘要>]}`，按 `updated_at` 倒序；摘要含 `task_id,name,mode,scenario,intent,updated_at,last_run_id,last_status,env_keys`（**只给键名，不给值**）。
- `GET /api/tasks/{task_id}` → 完整 task，`env` 经统一 `mask_env_payload` 打码（键名保留、值 `***`）。

### 3.2 新建 / 更新 / 删除

- `POST /api/tasks` → `201`，body 同 §2（除 id/时间戳）。返回打码后的 task。
- `PATCH /api/tasks/{task_id}` → `200`，**部分更新**语义：
  - 未出现的字段保持不变；
  - `env` 按 **键级合并**：`"***"` 或 `""` 视为"沿用旧值"（前端把已保存的密钥显示为 `***`，用户不动就不会覆盖）；`null` 显式删除该键；其余值覆盖。
- `DELETE /api/tasks/{task_id}` → `204`；**只删任务定义，不删任何 run 产物**。
- 未知 id → `404 not_found`；非法体 → `400 bad_request`。

### 3.3 运行任务

- `POST /api/tasks/{task_id}/run` → `202 {job_id}`；可选 body `{"env": {...}}` 做**本次覆盖**（不写回任务）。
- 执行等价于用任务里存的 mode/scenario/intent/env/regression/no_llm 提交 `/api/runs`。

### 3.4 从历史取配置 / 重跑

- `GET /api/runs/{run_id}/config` → `200`：

```json
{
  "mode": "scenario",
  "scenario": "scenarios/api_smoke.yaml",
  "intent": "",
  "env": {"BASE_URL": "***"},
  "regression": false,
  "no_llm": false,
  "plan_source": "spec",
  "error": {
    "case": "认证链路：登录拿 token → 访问受保护接口",
    "step": "登录",
    "message": "步骤 login 的 capture 路径不存在: body.token",
    "expected": null,
    "actual": null
  }
}
```

  - env 从磁盘 `report.json`/`plan.json` 解析后**打码**回吐；缺省字段给默认值，不返回 500。
  - `error` 取第一个失败/异常步骤；全通过时为 `null`。
  - `mode` 无法判定时回 `"scenario"` 并在 `plan_source` 里写实际来源。

- `POST /api/runs/{run_id}/rerun` → `202 {job_id}`；可选 body：
  - `{"env": {...}}` 覆盖；
  - `{"save_as_task": true, "name": "..."}` 顺带创建任务定义并把本次覆盖写进去（默认 false）。
  - **服务端从磁盘取真实 env 作为基线**（不是打码值）；请求里出现 `"***"` 的键视为"沿用"。

### 3.5 从历史保存为任务（不运行）（v2.0.2 新增，修 F4）

`POST /api/runs/{run_id}/save_as_task` body：`{"name": "可选，缺省沿用 "重跑 <run_id>" 规则"}`

- `201` 返回打码后的 task；**不产生新 run**。
- `env` 由**服务端从磁盘报告/计划复制真实值**（不是前端回传的打码值），因此不存在"密钥只能跳过"的情况。
- `mode/scenario/intent/regression/no_llm` 由该 run 的 `meta.run_config`（或退化规则）推断。
- 命名冲突、`name` 非法 → 同 §3.2 的校验口径。
- 旧报告缺 `run_config` 且无法判定目标时 → `400 bad_request`，不猜。

**前端可叠加表单修改（v2.0.3 拍板）**：`201` 之后前端允许再发一次 `PATCH /api/tasks/{id}` 把用户在预填表单里改过的 env/模式/场景/意图叠加到服务端已复制的真实 env 上（`***` 仍表示沿用）。即「仅保存配置」= **保存并应用我的修改，但不运行**。理由：用户在表单里改了 `BASE_URL` 再点保存，静默丢弃修改比多一次请求更糟。PATCH 失败时任务仍保留服务端复制版本，前端必须常驻告警。

**为什么需要**：前端从历史预填时只能拿到 `***`，"仅保存不运行"这条路必然丢密钥，用户保存后一运行就认证失败——这是产品陷阱，在后端修掉。

## 4. 前端（ui）

### 4.1 新视图 `#/tasks`

- 列表列：名称 / 模式 / 目标（场景名或意图摘要）/ 上次运行（run_id 短码 + 状态）/ 更新时间 / 操作。
- 操作：**运行**（就地提交并进任务浮层）、**编辑**、**删除**（二次确认，文案写明"只删配置，历史运行保留"）、**新建**。
- 空态：说明任务是什么、给"新建任务"和"从历史挑一条"两个入口。

### 4.2 任务表单（新建/编辑共用）

- 字段：名称、模式（四选一，切换时显隐场景下拉/意图输入）、场景下拉（来自 `/api/scenarios`）、意图、env 键值行、`regression`、`no_llm`。
- 编辑态：已保存的密钥类 env 显示为 `***`，旁注"留空表示沿用已保存值"。
- 底部按钮：**保存**、**保存并运行**、取消。

### 4.3 运行历史接入

- 历史表每行加两个动作：**重跑**（直接 POST rerun）、**编辑配置**（跳任务表单并预填 `GET /api/runs/{id}/config`，保存后创建为新任务）。
- 运行详情页失败区块顶部加按钮：**按此配置修正并重跑**；点击后进入同款预填表单，并在表单顶部固定展示原错误（用例/步骤/`message`/`expected`/`actual`），让人知道该改什么。

### 4.4 交互要求

- 所有 CRUD 失败必须显示后端 `error.message` 原文，不得静默。
- 删除、覆盖已有任务、`save_as_task` 重名都要二次确认。
- 表单在提交中禁用重复提交。

## 5. 验收（A17–A26）

| # | 项 | 判定 |
| --- | --- | --- |
| A17 | `POST /api/tasks` 合法体 | 201；`<workspace>/.panguverse/tasks/<id>.json` 存在、`0600`、未 git 跟踪；`<workspace>/tasks/` **不得**被创建 |
| A18 | `GET /api/tasks` / `GET /api/tasks/{id}` | 200；列表只给 `env_keys`；详情 env 值为 `***`，`grep demo123` 零命中 |
| A19 | `PATCH` 部分更新 | 只改出现字段；`env` 传 `"***"` 不覆盖旧值；传 `null` 删键；其余覆盖 |
| A20 | `DELETE` | 204；任务文件消失；`runs/` 下历史目录**数量不变** |
| A21 | `POST /api/tasks/{id}/run` | 202 → job `done`；用例数/结果与任务配置一致；任务定义未被本次 env 覆盖改动 |
| A22 | `GET /api/runs/{id}/config` | 200；`mode/scenario/env(打码)/plan_source/error` 齐全；失败 run 的 `error.message` 与报告首个失败步骤一致；通过 run 的 `error=null` |
| A23 | `POST /api/runs/{id}/rerun` | 202 → 新 run_id，配置与源 run 一致（`plan.source` 同源）；env 传 `"***"` 时沿用磁盘真实值并跑通 |
| A24 | 非法输入 | 未知 task_id 404；`mode` 非法 / name 空 / intent 空 / scenario 越界 → 400；`task_id` 形如 `../x` → 400/404，不读任意文件 |
| A25 | 前端 | `#/tasks` 增删改查全通；历史「重跑」「编辑配置」可用；详情失败块按钮能进预填表单且显示原错误；删除/覆盖有二次确认；错误可见；控制台零未捕获异常 |
| A26a | `POST /api/runs/{id}/save_as_task` | 201；任务落盘 env 含**真实值**（读磁盘确认），HTTP 响应仍全为 `***`；随后 `POST /api/tasks/{id}/run` 能跑通（证明密钥没丢） |
| A26b | 该路径不产生新 run | 调用前后 `runs/` 目录数量不变 |
| A26 | 回归 | `pytest -q`（无 NO_PROXY）全绿、`ruff check .` 0 error；A15 workspace 边界仍成立；A16 四处出口仍无明文 |

## 6. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/web/tasks_store.py`（新） | `panguverse/web/static/app.js` |
| `panguverse/web/service.py`、`api.py`、`jobs.py` | `panguverse/web/static/index.html` |
| `tests/test_web_tasks.py`（新） | `panguverse/web/static/style.css` |
| `docs/web-api.md` | |

- 双方都不得改 `panguverse/core|engine|axe|packs|spec|report` 的语义；不得改 `dependencies`（保持 `[]`）；不得 push。
- 后端必须复用现有 `mask_env_payload`，不许另写一套打码。

## 7. 明确不做

任务分组/标签、任务排序拖拽、批量运行、定时任务、任务版本历史、跨工作区迁移、权限隔离。
