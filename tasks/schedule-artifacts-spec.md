# 调度与触发 + 任务产出物 · 实现契约 v8.0

> 依据：上轮产品评审结论——平台"只在被点时才动"，且"跑完的成果没有归宿"。
> 本轮补齐两件事：**让流程自己跑**（调度/触发）+ **让成果可交付**（产出物）。
> 另含一处既有响应式缺陷修复（小窗口画布仅 110px 高）。

---

## 1. 调度与触发（Schedule / Trigger）

### 1.1 数据模型

`<workspace>/.panguverse/schedules/<sid>.json`（目录 0700、文件 0600、原子写）：

```json
{
  "sid": "sc-1a2b3c",
  "name": "每日站点巡检",
  "target": {"kind": "project", "project_id": "p-3f2a1c", "suite_ids": []},
  "trigger": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"},
  "env_override": {},
  "enabled": true,
  "created_at": 0, "updated_at": 0,
  "last_run_at": null, "last_batch_id": null, "last_status": null,
  "next_run_at": 0
}
```

- `sid` 服务端生成 `^sc-[0-9a-f]{6,12}$`；`trigger.kind ∈ cron|interval|webhook`。
- `interval` 用 `{"kind":"interval","minutes":30}`（最小 1 分钟）。
- `webhook` 用 `{"kind":"webhook","token":"..."}`（32 字节随机，展示一次，可重置）。
- `target.kind=project` 时复用项目批量运行；`suite_ids=[]` 表示全部套件。

### 1.2 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/schedules` | 列表（默认只列 enabled+archived=false，`?include_disabled=1`） |
| POST | `/api/schedules` | 创建；返回含 `next_run_at` |
| GET | `/api/schedules/{sid}` | 详情 |
| PATCH | `/api/schedules/{sid}` | 改 trigger/target/env/enabled |
| DELETE | `/api/schedules/{sid}` | 204（不删历史运行） |
| POST | `/api/schedules/{sid}/run-now` | 立即触发一次 → `{job_id,batch_id}` |
| POST | `/api/hooks/{token}` | webhook 触发，body 可带 `env` 覆盖；token 无效 404 |

规则：
- **禁止重叠**：上一次同 sid 的 batch 仍 running → 本次触发记录 `skipped_overlap`（不排队、不堆叠），并写事件。
- `next_run_at` 由 cron/interval 计算；时区默认 `Asia/Shanghai`。
- 调度器为 `serve` 进程内线程（单进程 MVP）；重启后按磁盘状态恢复 `next_run_at`，**错过的任务不补跑**（记录 `missed`）。
- 所有触发都写执行记录 `meta.trigger = schedule|webhook|manual`、`meta.schedule_id`。

### 1.3 验收 S1–S8

| # | 项 | 判定 |
| --- | --- | --- |
| S1 | CRUD | 建/查/改/删全通；落盘 0600；客户端传 sid 被忽略 |
| S2 | cron 计算 | `0 9 * * *` 的 `next_run_at` 为次日 09:00（Asia/Shanghai），连续 3 次推算正确 |
| S3 | interval 触发 | 1 分钟间隔，等待后真实触发一次，产生 batch 且 `meta.trigger=schedule` |
| S4 | run-now | 立即产生 batch，不等 cron |
| S5 | 重叠保护 | 手工让上次 batch 长时间 running 后再次触发 → 记录 `skipped_overlap`，不产生新 batch |
| S6 | webhook | 正确 token 触发成功；错误 token 404；`enabled=false` 时 409/403 |
| S7 | 重启恢复 | 重启后 schedules 仍存在、`next_run_at` 重算；错过的不补跑且记录 `missed` |
| S8 | 关闭态 | `enabled=false` 不触发（cron/webhook 均不生效），`run-now` 仍允许（显式人工） |

## 2. 任务产出物（Artifacts）

### 2.1 概念

**产出物 = 一次运行中值得交付的结果**，不是日志。来源两类：
1. **原子声明**：新增可选字段 `produces`（如 `llm_summarize.produces=["summary_md"]`、`web_screenshot.produces=["screenshot"]`、`android_ui_dump.produces=["ui_tree"]`）；
2. **报告级**：AI 总结（`insight.summary_md`）自动成为产出物。

存储：`<ws>/.panguverse/artifacts/<project_id|unclassified>/<run_id>/<name>.<ext>` + `manifest.json`；文件 0600、目录 0700。

```json
{"artifact_id":"ar-1a2b3c","run_id":"...","project_id":"...","name":"summary_md",
 "type":"markdown|image|json|xml|text|file","bytes":1234,"sha256":"...",
 "node_id":"summarize","created_at":0}
```

### 2.2 API

- `GET /api/artifacts?project=&run=&type=&offset=&limit=` → `{total, items[]}`
- `GET /api/artifacts/{aid}` → 元信息 + 可选 `content`（markdown/text/json ≤ 64KB；图片给下载 URL）
- `GET /api/artifacts/{aid}/download` → 文件流 + `Content-Disposition`
- `POST /api/artifacts/{aid}/share` → `{url_token, expires_at}`（默认 7 天，可配 1h/1d/7d/30d）
- `GET /api/share/{token}` → 只读预览页（HTML，非登录可访问，token 过期 410）

规则：
- 产出物在运行结束后由执行器收集；**不影响执行结果**（收集失败只告警，不判失败）。
- 同名产物按 `<name>-<seq>` 去重；单产物上限 20MB，超过只记录元信息 + `truncated=true`。
- 分享 token 只读、不可列目录；过期即失效；可撤销。

### 2.3 验收 A1–A7

| # | 项 | 判定 |
| --- | --- | --- |
| A1 | 收集 | 跑一次含 `llm_summarize` 的工作流 → 生成 `summary_md` 产出物，manifest 含 sha256/bytes/node_id |
| A2 | 类型 | markdown/image/json/xml 四类正确识别（按 produces 声明 + 内容嗅探） |
| A3 | 列表/过滤 | 按 project/run/type 过滤正确，分页不重不漏 |
| A4 | 下载 | 二进制与 sha256 一致；`Content-Disposition` 带文件名 |
| A5 | 分享 | 生成 token → 未登录可访问预览页 → 过期 410 → 撤销后 404 |
| A6 | 隔离 | 路径穿越、跨项目越权读取均 404；manifest 不可经静态路径直接读 |
| A7 | 不阻断 | 人为让收集失败（目录不可写）→ 运行仍 `done`，只产生告警 |

## 3. 响应式修复（既有缺陷）

小窗口（756×469）下画布仅 **110px** 高，整图必然溢出。要求：
- 画布容器高度为 `flex:1` 且父容器 `min-h-0`，保证在 900/700/469 三种高度下画布可用高度 ≥ **55%** 视口；
- 窗口高度 < 600 时，调试面板默认折叠、节点属性面板改为抽屉，保证画布 ≥ 300px；
- 验收：756×469、900×700、1440×900 三档给出画布实际高度与占比。

## 4. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/web/schedules.py`（新）、`artifacts.py`（新） | `webui/src/**` |
| `webui` 之外的 `api.py`、`service.py`、`jobs.py` | `panguverse/web/static/**`（产物） |
| `panguverse/core/atom.py`（仅新增 `produces` 字段，语义不变） | |
| `tests/test_web_schedules.py`、`tests/test_web_artifacts.py` | |

约束：不改 engine/axe 语义；`dependencies` 保持 `[]`；不 push；调度器不得引入第三方 cron 库（自己解析 5 字段）。

## 5. 明确不做

多实例分布式调度、任务依赖 DAG、可视化 cron 编辑器、产出物版本对比、产出物嵌入外部系统。
