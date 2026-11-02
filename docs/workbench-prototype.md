# PanguVerse 工作台 · 设计原型稿 v1（待审查）

> 定位：**AI 驱动的万物自动化平台**，不是 CRUD 管理后台。
> 技术栈：前端 React 生态（自实现画布交互，参考 n8n 的编辑体验）；后端 Python 生态（复用现有零依赖内核）。
> 本文 + `docs/prototype/index.html`（可点击线框）一起评审；**评审通过前不动实现代码**。

---

## 1. 一句话

用户说一句人话 → AI 把它拆成原子组合并画出来 → 人在画布上改/调/锁 → 单步调试验证 → 审阅确认 → 发布成任务 → 运行、审计、出报告。
**每一步都留"AI 提议"与"人工裁决"的痕迹**，这是产品的主线，也是它和 CRUD 后台的根本区别。

---

## 2. 四个阶段的闭环（状态机）

```
①意图识别          ②AI 规划/可视化审核       ③草案/审阅/发布           ④任务运行
intent ──► draft(rev0, ai) ──► review ──► approved ──► published ──► execution ──► report
              ▲                 │              │             │              │
              │                 │              │             │              └─► audit
              └─(重规划, 仅未锁定节点)          │             └─► 绑定项目/套件
                                                └─► 拒绝 → 回 draft
```

| 阶段 | 允许的操作 | 门禁 |
| --- | --- | --- |
| 意图识别 | 输入/选领域包/看 AI 预选原子 | 必须非空；领域包必须存在 |
| AI 规划 | 重规划、逐条接受/拒绝 diff、锁定节点 | **锁定节点不得被覆盖** |
| 草案编辑 | 增删节点、改参数、重排、绑定变量、单步调试 | 校验错误未清零不可提交审阅 |
| 审阅 | 看 diff、风险清单、填写审阅意见 | 破坏性原子必须显式勾选确认 |
| 发布 | 发布为流程（可绑项目）/ 封存为原子（进领域包） | 必须填版本说明；必须 approved |
| 运行 | 运行、取消、重跑、单步调试（不影响历史） | 未发布不可运行 |

**审计链**：每次状态迁移写一条 `audit` 记录（谁、何时、从什么状态到什么状态、依据哪个 rev、附言），执行与报告都可回溯到发布版本。

---

## 3. 信息架构（六模块 + 工作台）

```
┌ 工作台 Studio（造流程）
│   #/studio/new          意图识别
│   #/studio/:wid         画布编排 + 调试
│   #/studio/:wid/review  审阅 / diff / 发布
├ 原子 Atoms       #/atoms           能力清单、契约、封存来源
├ 任务 Tasks       #/tasks           已发布工作流 / 套件任务
├ 执行 Executions  #/executions      运行中与已结束的执行、单步调试会话
├ 历史 History     #/history         跨任务执行记录、筛选、对比、审计
├ 项目 Projects    #/projects        被测/被自动化的系统，绑定任务
└ 报告 Reports     #/reports         报告、AI 总结、导出、签署结论
```

侧栏顺序不是随意排的：**造（Studio）→ 料（Atoms）→ 活（Tasks/Executions）→ 账（History/Reports）→ 域（Projects）**。

---

## 4. 逐模块设计（每个都含 CRUD + AI/人工协作点）

### 4.1 原子管理 `#/atoms`

- **查**：按领域包/能力/副作用/关键字过滤；卡片显示端口数、副作用徽标（`mutate`/`destructive`/`needs_key`）、被引用次数。
- **改**：别名、描述、标签、启用/停用（不改实现代码）；显示"来源：`packs/api_testing`"。
- **增**：**从流程封存**——发布时把分子 seal 成新原子，出现在这里（`origin: sealed`）。
- **删**：停用（软删）而非物理删除；被引用时阻止停用并列出引用方。
- **AI 协作点**：意图输入时，右侧实时显示"AI 预选的原子"及理由；人工可 `排除` 某原子，AI 重规划时遵守排除名单。

### 4.2 任务管理 `#/tasks`

- 任务 = **已发布的工作流版本**（`workflow@rev`）或套件；有 `draft/approved/published/deprecated` 状态。
- **增**：从工作台发布；或"从历史执行创建任务"（把某次执行固化成任务）。
- **改**：改绑定的环境/项目、改说明；改图必须回到工作台出新版本（**不允许就地改已发布图**，这是可追溯的前提）。
- **删**：归档（`deprecated`），历史执行保留。
- **AI 协作点**：每个任务显示"AI 生成 / 人工修改 N 处"的指纹。

### 4.3 执行管理 `#/executions`

- **查**：运行中/成功/失败/已取消；实时阶段与进度；可取消。
- **增**：手动触发（任务或流程）；**调试执行**单独标记，不进正式历史。
- **改**：不允许改已发生的执行；只能"基于此执行重跑"（生成新执行，`parent_execution_id` 关联）。
- **删**：不能删；可"隐藏"（不影响审计）。
- **AI 协作点**：执行结束后 AI 给"失败归因 + 建议"，人工在每条建议上标 `已处理/忽略/转任务`，记录处置人与时间。

### 4.4 历史管理 `#/history`

- **查**：项目/任务/状态/时间/触发方式筛选；分页；支持**两次执行对比**（用例级 diff：新增失败、修复、耗时变化）。
- **改**：只允许追加批注（审计注记）。
- **删**：不可删（审计要求）；可导出。
- **AI 协作点**：AI 对比两次执行时给"回归风险"判断（哪些失败是新引入的），人工确认结论。

### 4.5 项目管理 `#/projects`

- 项目 = 被自动化的系统（API 服务、文件系统目录、某个 SaaS…），绑定 base_url/env/任务。
- **CRUD**：建/改/删（删不删历史）；**绑定**任务或套件。
- **AI 协作点**：项目概览的"AI 体检"——基于最近 N 次执行给健康度与首要风险，人工勾选"接受为待办"。

### 4.6 报告管理 `#/reports`

- 报告 = 一次执行的可交付物（HTML/JUnit/MD/AI 总结）。
- **查**：按项目/任务/结论筛选；预览；下载；分享链接（可选有效期）。
- **改**：追加"人工结论签署"（通过/有条件通过/不通过 + 意见），签署后报告锁定。
- **删**：不可删；可标记"作废"（附原因）。
- **AI 协作点**：报告页 AI 总结（已有），数字必须来自报告本体（已实现的校正层）；人工签署才生效。

---

## 5. 画布与调试交互规格（Studio）

### 5.1 画布
- 节点卡：原子名、业务名、状态点（未校验/已校验/调试验证过）、`AI/人工` 徽标、锁图标、副作用徽标。
- 边：**数据边**（实线，标注变量名）/ **顺序边**（虚线，标 `order-only`）。
- 布局：自动分层（左侧入口、右侧出口），可手动拖动；缩放到 40–200%。
- 交互：框选、多选、`Delete` 删除、`Cmd+Z` 撤销（前端本地撤销栈）、双击进参数面板。
- 校验问题以节点角标呈现，点击跳到右侧问题列表对应项。

### 5.2 节点面板（右侧）
- 原子选择（可替换，替换时提示端口不兼容项）。
- 参数编辑：按 `input_ports` 类型给控件（string/number/bool/json）；`${var}` 支持引用补全。
- 端口绑定视图：`outputs` → 下游 `with` 的可视化连线。
- 风险区：`mutate`/`destructive`/`needs_key` 明确文案 + 需要勾选确认。
- 最近一次调试的 inputs/outputs（打码）与耗时。

### 5.3 调试面板（底部抽屉）
- 模式：`只跑此节点` / `从头跑到此节点` / `从此节点跑下游`。
- 每次执行一行：节点、状态、耗时、错误摘要；点开看 inputs/outputs（树形）。
- 变量快照：全部变量、来源节点、是否来自快照。
- 与正式运行的区别必须显式标注："调试（不影响历史）"。

### 5.4 审阅与发布
- 左：变更清单（AI 提议 vs 当前 / rev A vs rev B），逐条接受/拒绝。
- 中：图 diff（新增绿/删除红/改参黄）。
- 右：风险清单 + 校验结果 + 审阅意见输入。
- 底：`提交审阅` → `批准` → `发布`（勾选"封存为原子"则填原子名与领域包）。

---

## 6. 数据模型（Python 侧，落盘）

```
<ws>/.panguverse/studio/<workflow_id>/workflow.json
<ws>/.panguverse/studio/<workflow_id>/revisions/<rev>.json
<ws>/.panguverse/studio/<workflow_id>/audit.jsonl
<ws>/.panguverse/executions/<exec_id>/execution.json      # 含 parent/trigger/debug 标记
<ws>/.panguverse/packs_user/<pack>/pack.yaml              # 封存的原子里落这里
```

`revisions/<rev>.json`：`{rev, intent, domain_pack, nodes[], edges[], env_schema, message, created_by, created_at}`
- `nodes[]`：`{node_id, atom, name, with{}, capture{}, origin, locked, verified_at, ui:{x,y}}`
- `edges[]`：`{from, to, kind:"data|order", via}`
- `audit.jsonl`：`{ts, actor, action, from_state, to_state, rev, note}`

---

## 7. 后端处理流程（Python）

```
POST /api/studio/drafts {intent, domain_pack, exclude_atoms[]}
   → Planner(LLM/规则) → TestPlan(线性) → Graphifier → rev0(origin=ai) 落盘 + 校验报告
PATCH /api/studio/{wid}/nodes/{nid}     → 新 rev（origin=human, locked=true）+ 增量校验
POST  /api/studio/{wid}/replan          → 仅未锁定区域；产出 diff（不自动应用）
POST  /api/studio/{wid}/validate        → 静态校验（原子存在/必填端口/未定义变量/环/风险）
POST  /api/studio/{wid}/debug           → 调试执行（独立上下文，不入正式历史）
POST  /api/studio/{wid}/review          → diff(base_rev, rev) + risks
POST  /api/studio/{wid}/publish         → approved → 发布为 workflow@rev；可选 seal 成原子
```

关键约束：
- **锁定节点参与任何自动改写一律拒绝**（返回 409 并列出被锁节点）。
- 发布是**不可变**的：新版本 = 新 rev，不改旧 rev。
- 调试与正式执行分离存储；调试永不进 `/history`。
- 所有出口打码；审计记录写入即不可改。

---

## 8. 技术栈与工程约束

| 层 | 选型 | 理由 |
| --- | --- | --- |
| 前端 | React 19 + TypeScript + Vite；画布**自实现**（SVG 或 React Flow，MIT，二选一，见 §9 决策） | 你指定 React 生态；画布交互参考 n8n，但代码自研不 fork |
| 状态/请求 | TanStack Query + 轻量 store | 执行状态轮询天然适合 query 缓存 |
| 后端 | Python 3.10+，复用现有内核（原子/执行器/规划器） | 零依赖内核已经跑通并被测试覆盖，重写等于自废武功 |
| Web 框架 | 先保留 `http.server` 薄 API 层；触发条件是"并发>50 或需要异步任务队列"再迁 FastAPI | 避免为框架而框架；迁移动机写进文档 |
| 许可 | 仓库 Apache-2.0；依赖白名单 MIT/Apache/BSD，CI 扫描 | 开源项目底线（n8n SUL 已排除） |

---

## 9. 需要你拍板（三条，定了我就写实现契约）

1. **画布实现**：自绘 SVG（零构建依赖，慢 2–3 周）vs React Flow（MIT，快，但引入 npm 构建链）。你说"前端代码自己实现"，我理解为**不 fork n8n**，而不是"不许用画布库"——请确认。
2. **发布形态 P0**：只发布为"流程（任务）"，还是同时支持"封存为新原子"？后者才体现递归组合。
3. **调试权限**：只允许用项目已配置环境，还是允许临时填任意 URL（方便但危险，尤其公网部署时）。

---

## 10. 验收要点（原型评审时请看这些是否达标）

- W1 一句话 → 5 秒内出图，节点数 ≥3，每个节点可点开看参数。
- W2 数据边与顺序边可区分，且顺序边不假装是数据依赖。
- W3 改节点后，受影响下游与未定义变量**立刻**标出。
- W4 单步调试每一步都有 inputs/outputs/耗时/错误，且密钥打码。
- W5 AI 重规划后，锁定节点逐节点比对**零变化**。
- W6 未 approved 不可运行；未填版本说明不可发布。
- W7 审计链可从报告一路回溯到"哪个 rev、谁改的、AI 提的哪些被接受/拒绝"。
- W8 六模块都具备 CRUD，且每个模块都能看到 AI 与人工的动作痕迹。
- W9 切换领域包（api_testing / local_fs / 自定义），能力面板与模板随之变化，不改代码。
