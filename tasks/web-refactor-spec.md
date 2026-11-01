# Web 交互重构 · 实现契约 v4.0

> 依据 `docs/web-ux-story-v2.md`（UX-01–UX-11）。核心变化：引入 **Project → Suite → Run** 三级结构，
> 前端按新 IA 重写；后端只做**增量**，既有 A17–A46 行为不得回归。

## 1. 数据模型

项目落盘 `<workspace>/.panguverse/projects/<project_id>.json`（目录 0700、文件 0600、原子写）：

```json
{
  "project_id": "p-3f2a1c",
  "name": "coding-log-web",
  "base_url": "https://seres-ai.com",
  "env": {"BASE_URL": "https://seres-ai.com"},
  "suites": [
    {"suite_id": "s-1a2b3c", "name": "站点只读巡检",
     "scenario": "scenarios/suites/codinglog_web.yaml",
     "env": {"WEATHER_DATE": "2026-11-02"}, "tags": ["smoke"]}
  ],
  "created_at": 0.0, "updated_at": 0.0
}
```

规则：
- `project_id` `^p-[0-9a-f]{6,12}$`、`suite_id` `^s-[0-9a-f]{6,12}$`，**服务端生成**。
- `suite.scenario` 校验与现有 `resolve_scenario` 一致（workspace 或内置 `scenarios/`，realpath 前缀）。
- env 合并优先级：`suite.env` > `project.env` > 请求 env 覆盖（请求仅本次生效）。
- **运行归属**：从项目/套件发起的运行，写进报告 `meta.project_id` / `meta.suite_id`（additive）。
- 旧数据（无归属的运行）在列表里归为「未归类」，不得报错。

## 2. API（新增，均需登录）

- `GET /api/projects` → `{"projects":[{project_id,name,base_url,suite_count,last_run:{run_id,status,verdict,at,passed,total}|null,pass_rate:{"passed":n,"total":n}|null}]}`
- `POST /api/projects` `{name, base_url, env?}` → 201（响应 env 打码）
- `GET /api/projects/{pid}` → 项目详情（env 打码，suites 原样）
- `PATCH /api/projects/{pid}` → 部分更新；env 键级合并（`***`/空=沿用、`null`=删键）
- `DELETE /api/projects/{pid}` → 204（**不删任何 run**）
- `POST /api/projects/{pid}/suites` `{name, scenario, env?, tags?}` → 201 suite
- `PATCH /api/projects/{pid}/suites/{sid}` / `DELETE ...` → 200 / 204
- `GET /api/projects/{pid}/summary?limit=5` → `{recent_runs:[...], pass_rate:{...}, failure_top:[{kind,count,cases:[case_id]}]}`（failure_top 按报告 failure_kind 聚合，最多 3 条）
- `POST /api/projects/{pid}/run` `{suite_ids?: [], env?: {}}` → 202 `{batch_id, jobs:[{job_id,suite_id}]}`；缺省跑该项目全部套件
- `GET /api/batches/{batch_id}` → `{batch_id, project_id, status, suites:[{suite_id,name,job_id,run_id,status,passed,total,error}], aggregate:{passed,total,suites_passed,suites_failed,duration_ms}}`
- `GET /api/runs/query?project=&suite=&status=&offset=0&limit=20` → `{total, items:[...]}`（`status` ∈ `all|passed|failed|errored|done`）

**兼容**：`/api/runs`（数组）、`/api/runs/{id}`、`/config`、`/insight`、`/api/tasks*`、`/api/scenarios`、`/api/atoms`、`/api/packs` 全部保持不变。

## 3. 批量运行语义

- 同一 batch 内套件**串行**执行（可预测、避免争抢）；每个套件一个 job，各自产出 run。
- batch 终态 = `done`（全部跑完）或 `failed`（有套件拿不到报告）；`aggregate` 按 run 汇总。
- batch 与 job 都在内存（重启即丢）；run 落盘不受影响。
- 单套件失败不中断其余套件。

## 4. 前端 IA（重写）

路由：
```
#/projects                     项目列表
#/p/{pid}/overview             项目概览
#/p/{pid}/run                  项目内运行
#/p/{pid}/history              项目内历史
#/p/{pid}/runs/{run_id}        运行详情（含 AI 总结、产物）
#/p/{pid}/settings             项目设置（含套件管理）
#/platform/atoms               平台·原子库
#/platform/packs               平台·领域包
#/platform/tasks               平台·任务模板
```
- 左侧：项目切换器（含最近状态点），底部「平台」折叠组。
- 顶部：当前项目名 + 项目内 tab（概览/运行/历史/设置）+ 退出登录。
- **禁止**任何跨项目混跑入口：批量运行只取当前项目套件。
- 详情页保留：失败证据、修正重跑、AI 总结（来源标注不变）、JUnit/HTML。
- 历史页筛选条件写入 URL query（`?suite=&status=&page=`），刷新不丢。
- 空态必须给出下一步动作（创建项目 / 绑定套件）。

## 5. 验收（A50–A62）

| # | 项 | 判定 |
| --- | --- | --- |
| A50 | 项目 CRUD | 建/查/改/删全通；落盘 `.panguverse/projects/`、0600；删项目不动 runs |
| A51 | 套件绑定 | 新增/改/删套件；`scenario` 越界 → 400；同名允许（id 区分） |
| A52 | env 三态 | PATCH `***`/空=沿用、`null`=删键；suite.env 覆盖 project.env |
| A53 | 运行归属 | 从项目发起 → 报告 `meta.project_id/suite_id` 正确；旧运行显示「未归类」 |
| A54 | 概览汇总 | `summary` 的 recent_runs/pass_rate/failure_top 与真实报告一致 |
| A55 | 批量运行 | 跑全部套件：串行、每套件一个 run、`aggregate` 数字与各 run 相符；单套件失败不中断 |
| A56 | 历史筛选分页 | project/suite/status 筛选正确；`total` 与分页一致；offset 不重不漏 |
| A57 | 前端主路径 | 登录 → 项目列表 → 概览 → 一键跑全部 → 详情，全程 ≤ 4 次点击；控制台零异常 |
| A58 | 无跨项目混跑入口 | 批量/回归入口只能选当前项目套件；跨项目用例不可达 |
| A59 | 历史 URL 状态 | 筛选后刷新，条件与结果一致 |
| A60 | 响应式 | 390px 无横向滚动；表格转卡片；详情步骤可折叠 |
| A61 | 回归 | `pytest` 全绿、`ruff` 0；A17–A46 既有行为不变（任务 CRUD、AI 总结、认证、报告出口） |
| A62 | 实战 | 用新 UI 对 `panguverse` 与 `coding-log-web` 各跑一次全量套件，均通过并可下钻 |

## 6. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/web/projects.py`（新）、`batches.py`（新） | `static/index.html` |
| `panguverse/web/api.py`、`service.py`、`jobs.py` | `static/app.js`（可整体重写） |
| `tests/test_web_projects.py`（新） | `static/style.css`（可整体重写） |
| `docs/web-api.md` | |

- ui **可以整体重写**三个静态文件，但必须保留：登录/401 回登录、相对路径、markdown 转义、AI 总结来源标注。
- 双方都不得改 `panguverse/core|engine|axe|packs|spec|report` 语义、`dependencies`、不得 push。

## 7. 迁移

- 现有两个「API 测试」任务与历史运行保留可用；旧运行无归属 → 「未归类」。
- 首次进入项目列表为空时，提供「从历史创建项目」引导（只提示，不自动建）。
