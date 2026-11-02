# 工作台 MVP · 实现契约 v5.1

> 依据 `docs/workbench-prototype.md`（四阶段闭环 + 六模块）。
> **已拍板**：画布 = React Flow（MIT）；调试 = 只允许项目已配置环境；P0 发布形态 = **只发布为「流程」**，
> 「封存为新原子」延到 P1（P0 先把主链跑通，递归组合的 UI 与领域包写入留到下一轮）。

## 1. 工程结构（关键决策）

```
webui/                     # 新增：React 源码（Vite + React 19 + TS + React Flow + TanStack Query）
panguverse/web/static/     # 构建产物，提交进仓库（保证 pip 用户零构建可用）
```

- `webui` 构建输出直接覆盖 `panguverse/web/static/`（`index.html` + `assets/`）。
- **产物必须提交**：`python -m panguverse serve` 不依赖 npm。CI 增加一条「`npm run build` 后 git diff 为空」的检查。
- 相对路径 + `base: './'`：继续支持挂在 `/panguverse/` 子路径。
- 旧 `app.js/style.css` 迁移完成后删除，不保留双份。

## 2. 后端新增（Python，复用现有内核）

新增文件：`panguverse/web/graph.py`（线性→图）、`panguverse/web/studio.py`（草案/版本/审计/调试）、`tests/test_web_studio.py`。

### 2.1 数据落盘

```
<ws>/.panguverse/studio/<wid>/workflow.json           # {wid,name,intent,domain_pack,state,current_rev,locked[],published_ref}
<ws>/.panguverse/studio/<wid>/revisions/<rev>.json     # 不可变：nodes[]/edges[]/env_schema/message/created_by
<ws>/.panguverse/studio/<wid>/audit.jsonl              # 状态迁移审计（append-only）
<ws>/.panguverse/executions/<eid>/execution.json       # 执行元信息（trigger: task|studio|debug|manual）
```

目录 `0700`、文件 `0600`、原子写。

### 2.2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/studio/drafts` | `{intent, domain_pack, exclude_atoms?}` → `{wid, rev, graph, validation}`；内部：Planner→Graphifier→rev0(`origin=ai`) |
| GET | `/api/studio/drafts` | 列表（状态/领域包筛选） |
| GET | `/api/studio/{wid}` | `?rev=` 取图 + 元信息 |
| PATCH | `/api/studio/{wid}/nodes/{nid}` | 改参数/改名/锁定/替换原子 → 新 rev（`origin=human`，`locked=true`） |
| POST | `/api/studio/{wid}/nodes` | 新增节点 → 新 rev |
| DELETE | `/api/studio/{wid}/nodes/{nid}` | 删除节点（连同相关边）→ 新 rev |
| POST | `/api/studio/{wid}/edges` | `{from,to,via,kind}` → 新 rev |
| POST | `/api/studio/{wid}/replan` | `{instruction}` → `{diff, proposed_rev}`；**不自动应用**；锁定节点冲突返回 409 + `locked_nodes[]` |
| POST | `/api/studio/{wid}/apply-diff` | `{proposed_rev, accepted:[node/edge 变更 id]}` → 新 rev |
| POST | `/api/studio/{wid}/validate` | `{rev?}` → `{errors[], warnings[], risks[]}` |
| POST | `/api/studio/{wid}/debug` | `{rev, node_id, mode:"only|from_start|downstream"}` → `{trace[], variables{}}`；**不入正式执行目录** |
| POST | `/api/studio/{wid}/review` | `{base_rev, rev}` → `{diff, risks, audit_tail[]}` |
| POST | `/api/studio/{wid}/publish` | `{rev, message}` → 校验 `state=approved` + 非空 message → `{workflow_ref}` |
| POST | `/api/studio/{wid}/state` | `{to:"review|approved|rejected", note}` → 状态迁移 + 审计 |

**P1 预留**（本轮不实现，仅留字段）：`as_atom: true`、`atom_name`、`domain_pack` 写入用户包。

### 2.3 关键规则

1. **锁定优先**：`locked=true` 的节点，任何 replan/apply-diff 都不得修改；冲突必须 409 并列出。
2. **发布不可变**：publish 生成新的 `workflow_ref = "<wid>@<rev>"`；旧 rev 不改。
3. **调试隔离**：debug 用独立 `RunContext` 快照，写 `.panguverse/studio/<wid>/debug/`，不进 `/api/runs` 历史。
4. **调试环境受限**：只能用**项目已配置 env**（或工作区 `last_env` 基线）；请求里出现任意 URL 覆盖一律 400。
5. **审计 append-only**：状态迁移、发布、diff 应用各写一条。
6. **打码**：所有出口复用 `mask_env_payload`；调试 inputs/outputs 同样打码。

## 3. 前端（React 应用）

### 3.1 路由

```
#/projects                         项目列表（迁移现有）
#/p/:pid/{overview,run,history,settings}
#/p/:pid/runs/:rid
#/studio/new                       意图识别
#/studio/:wid                      画布 + 节点面板 + 调试抽屉
#/studio/:wid/review               审阅 / diff / 发布
#/atoms  #/tasks  #/executions  #/history  #/reports
#/platform/{packs,tasks}            旧入口重定向
```

### 3.2 画布（React Flow）

- 自定义节点组件：原子名 + 业务名 + 状态点 + `AI/人工` 徽标 + 锁 + 副作用徽标。
- 边：数据边（实线，label=变量名）/ 顺序边（虚线，label=`order-only`）。
- 拖拽连线 → `POST /edges`；节点拖动只更新 `ui.x/y`（不进 rev，避免刷版本）。
- 选中节点 → 右侧面板（原子替换、参数控件、端口绑定、风险确认、最近调试结果）。
- 校验问题以角标 + 右侧列表联动；点击定位节点。

### 3.3 调试抽屉

- 三个模式按钮（only / from_start / downstream）；结果表格（节点/状态/耗时/错误），点开看 inputs/outputs 树。
- 明确标注「调试执行，不影响历史」。
- 变量快照树：来源节点 + 是否来自快照。

### 3.4 审阅发布

- diff 三态（新增/删除/改参），逐条接受/拒绝 → `apply-diff`。
- 风险清单（破坏性/写副作用/密钥）必须勾选确认才可 `approved`。
- 发布必须填版本说明；发布后跳到任务详情。

### 3.5 必须保留的既有行为

登录/退出/401 回登录（保留 hash 意图）、相对路径、markdown 转义、AI 总结来源标注、204 空体处理、历史筛选 URL 持久化。

## 4. 验收（W1–W9 + R1–R4）

| # | 项 | 判定 |
| --- | --- | --- |
| W1 | 意图→图 | 5 秒内出图 ≥3 节点；节点有原子名/参数摘要/来源徽标 |
| W2 | 边语义 | 有 `capture`/`${var}` 的为数据边；其余为 `order-only` 且标注 |
| W3 | 改节点即时校验 | 删除/改参后未定义变量与受影响下游立刻标出 |
| W4 | 单步调试 | 每步 inputs/outputs/耗时/错误齐全，密钥打码 |
| W5 | AI 不越权 | replan 后锁定节点逐节点比对零变化（冲突返回 409） |
| W6 | 门禁 | 未 approved 不可 publish；空 message 不可 publish；未确认风险不可 approve |
| W7 | 审计 | 从报告/执行可回溯到 `wid@rev` 与审计条目 |
| W8 | 六模块 CRUD | 原子/任务/执行/历史/项目/报告 均有查/增/改/删（删=归档或作废）且可见 AI/人工痕迹 |
| W9 | 领域包 | 切换 `api_testing`/`local_fs` 时能力面板与意图模板变化，不改代码 |
| R1 | 回归 | `pytest` 全绿、`ruff` 0；A17–A62 既有接口与行为不变 |
| R2 | 构建 | `npm run build` 后 `git diff` 为空（产物已提交） |
| R3 | 子路径 | `/panguverse/` 下登录、画布、调试、发布全通 |
| R4 | 实战 | 用工作台造一个流程 → 发布 → 绑定项目 → 运行 → 报告可回溯 rev |

## 5. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/web/graph.py`、`studio.py`、`api.py`、`service.py`、`jobs.py` | `webui/**`（全部源码与配置） |
| `tests/test_web_studio.py`、`docs/web-api.md` | `panguverse/web/static/**`（构建产物） |
| `pyproject.toml`（仅当需要，保持 `dependencies=[]`） | `README` 构建说明（可选） |

## 5.1 W8 收口（v5.1，QA 缺口裁决）

QA 判定 W8 未达成。裁决如下（**P0 本轮补齐**，P1 明确延后并写入非目标）：

| 模块 | 缺口 | 裁决 | 端点 |
| --- | --- | --- | --- |
| 执行 | 列表/详情无接口（页面是空壳） | **P0** | `GET /api/executions?project=&status=&offset=&limit=`、`GET /api/executions/{eid}` |
| 执行 | 无法取消（长任务/批量失控） | **P0** | `POST /api/executions/{eid}/cancel`（job 级；batch 取消剩余套件；语义写入文档） |
| 任务 | 硬删除会丢配置，契约要求"删=归档" | **P0** | `POST /api/tasks/{id}/archive`、`/unarchive`；列表默认排除已归档 |
| 项目 | 同上 | **P0** | `POST /api/projects/{pid}/archive`、`/unarchive` |
| 报告 | 无列表；无人工签署/作废（AI+人工确认的终点） | **P0** | `GET /api/reports?...`、`POST /api/reports/{run_id}/sign {verdict,note}`、`POST /api/reports/{run_id}/void {reason}` |
| 原子 | 启用/停用 | **P1（明确不做）** | 原子由代码/领域包定义，工作区级开关会造成"同包不同行为"，改由包级治理；UI 只读并注明 |

约束：
- 归档是软删：`archived_at` 字段；已归档对象默认不出现在列表，但历史运行/报告保留。
- 签署后报告锁定：再签需 `force`（并在审计里留痕）；作废需理由且不可撤销。
- 取消语义：job 在当前步骤结束后停止；batch 取消后**已完成的 run 保留**，未开始的套件标记 `cancelled`。
- 所有操作写审计（`actor/action/target/from/to/note`）。

## 6. 里程碑（同一轮内）

1. M1 后端：graph/studio/审计/调试 + 单测（coder）。
2. M2 前端：React 脚手架 + 六模块迁移（ui 并行）。
3. M3 画布与审阅发布（ui 依赖 M1 接口，按契约先写 mock）。
4. M4 联调 + QA（W1–W9、R1–R4）。
