# PanguVerse Web API（MVP）

> 契约来源：`tasks/web-app-mvp-spec.md` §3–§7；用户故事：`docs/web-app-user-stories.md`。
> 实现：`panguverse/web/`（纯标准库，零运行时依赖）。

## 1. 启动

```bash
panguverse serve [--host 127.0.0.1] [--port 7788] [--workspace .] [--no-open] [--no-auth] [--auth-user admin]
# 等价
python -m panguverse serve --port 7788 --workspace . --no-open
```

**登录认证默认开启**（v2.1）：`serve` 不带参数就要求登录；仅 `--no-auth` 关闭（会打印醒目警告，
本机调试用）。`--auth-user` 可覆盖账号名（密码仍来自环境变量或 `auth.json`）。

- 默认 `127.0.0.1:7788`，工作区默认当前目录；监听成功会打印 `http://127.0.0.1:<port>/`。
- `--no-open` 不尝试拉起浏览器（CI/测试用）。
- `--host 0.0.0.0` 允许但会打印警告：**无认证，仅限可信网络**。
- 所有产物落 `<workspace>/runs/<run_id>/`（`report.json` / `report.html` / `junit.xml` / `plan.json`），与 CLI 完全一致。
- 前端资源缺失时 `GET /` 返回 `503` + `前端资源未就绪…` 文案，API 不受影响。

### 安全边界（务必阅读）

- **只监听本机**：默认 `127.0.0.1`；本服务没有认证、没有会话，**不可暴露公网**。
- 静态资源走白名单（`index.html` / `app.js` / `style.css` / `favicon.svg`），并用 `realpath` 前缀校验兜底，`/static/../../cli.py` 一律 404。
- `/api/*` 默认要求登录（`/api/auth/*` 白名单）；`--no-auth` 仅用于本机调试，暴露非本机地址会打印危险警告。
- 场景路径经 `realpath` 前缀校验，只能读 `<workspace>` 或仓库内置 `scenarios/`；越界 400。
- `run_id` 必须匹配 `^[0-9a-f]{6,32}$`，`task_id` 必须匹配 `^t-[0-9a-f]{6,12}$`，杜绝用 `..` 拼接路径。
- 错误消息经 `core/redact.py` 打码（不出现密钥/token/Cookie 原文）；`/api/status` 只回 `llm_enabled: bool`，绝不回显密钥值。
- **HTTP 层 env 打码（v1.3/v1.4，A16）**：所有出口共用一个 `mask_env_payload`——JSON 响应里的 `env` 子树
  （含 `plan.env`、`report.env`、`cases[].variables.env`）中，键名命中
  `(?i)(password|passwd|secret|token|api[_-]?key|credential)` 的值一律替换为 `***`，键名保留；递归处理嵌套 dict/list。
  `report.html` / `junit.xml` 不再直读磁盘原文，而是用打码后的报告数据**重新渲染**（HTML 标题沿用磁盘文件），
  因此 `<pre>` 里的 JSON 仍是合法结构、只换值不换键。磁盘上的 `report.json` / `plan.json` / HTML / XML
  与 `last_env.json` 保持原样（回归回放依赖），只在 HTTP 出口打码。

## 1.1 登录认证（v2.1，契约 `tasks/web-auth-spec.md`）

- **默认开启**：除 `/api/auth/*` 外，所有 `/api/*` 在**路由分派前**统一校验会话，未登录一律
  `401 {"error":{"code":"unauthorized","message":"需要登录"}}`；`GET /` 与静态资源始终 200，
  由前端自己看 `GET /api/auth/status` 渲染登录页（不靠 302）。
- **凭据来源**（优先级从高到低）：
  1. 环境变量 `PANGUVERSE_WEB_USER` + `PANGUVERSE_WEB_PASSWORD`（不落盘）；
  2. `<workspace>/.panguverse/web/auth.json`（`0600`，PBKDF2-SHA256，200k 迭代，salt 32 字节）；
  3. 都没有 → 首次启动生成 16 位随机密码（去掉易混字符），只把哈希写入 `auth.json`，
     明文仅打印一次：`[auth] 初始账号 admin，初始密码 <pwd>（首次启动生成，仅显示这一次）`。
- **会话**：Cookie `pv_session` = `base64url(payload).base64url(hmac_sha256(payload, key))`，
  `payload={u,exp,nonce}`，12h 有效；密钥独立存 `<workspace>/.panguverse/web/session.key`（32 字节，`0600`），
  **不复用任何 env 值**。Cookie 属性 `HttpOnly; Path=/; SameSite=Lax; Max-Age=43200`，
  请求带 `X-Forwarded-Proto: https` 时追加 `Secure`。
- **会话密钥启动轮换（v2.1.1）**：每次进程启动都生成新的 `session.key`（覆盖旧文件），
  因此**重启后所有既有 Cookie 立即失效**；登出黑名单只在内存，也不需要落盘。
- **校验**：HMAC、`exp`、`u` 与当前账号一致、登出黑名单（内存态 nonce，重启即清空）；
  任一项不过按未认证处理，绝不 500。
- **登出**：`POST /api/auth/logout` → `200 {"ok":true}`，`Set-Cookie: pv_session=; Max-Age=0 ...`，
  旧 cookie 立即失效（nonce 进黑名单直到自然过期）。
- **限流**：同 IP 5 分钟内失败 5 次 → 后续请求 `429` + `Retry-After`（秒）；
  成功登录清零。登录失败**不区分**账号不存在/密码错，日志只记
  `[auth] login failed user=<name> ip=<ip>`，绝不出现密码。
- **真实客户端 IP（v2.1.1）**：限流桶按真实客户端计。TCP 对端不是回环/受信代理时**忽略**
  `X-Forwarded-For`（防伪造）；对端是回环/受信代理（nginx 在本机反代）时取
  `X-Forwarded-For` 的**最右一跳**（nginx `proxy_add_x_forwarded_for` 追加的那一跳），
  非法/缺失回退对端 IP。这样反代后不会退化成「全体用户共用一个桶」的登录 DoS。
- **接口**：
  - `GET /api/auth/status` → `{"authenticated": false}` / `{"authenticated": true, "user": "admin"}`
    （`--no-auth` 时恒为 `{"authenticated": true, "user": "", "auth_disabled": true}`）；
  - `POST /api/auth/login` `{"username","password"}` → 成功 `200 {"ok":true,"user":"admin"}` + `Set-Cookie`；
    失败 `401 bad_credentials`；限流 `429 too_many_attempts`；
  - `POST /api/auth/logout` → `200 {"ok":true}`（幂等）。
- 认证文件（`auth.json`/`session.key`）目录 `0700`、文件 `0600`，位于已被 gitignore 的 `.panguverse/`。

## 2. 响应约定

- 统一前缀 `/api`，响应 `application/json; charset=utf-8`，`Cache-Control: no-store`。
- 错误体固定：

```json
{"error": {"code": "not_found", "message": "run 不存在"}}
```

| code | HTTP | 场景 |
| --- | --- | --- |
| `bad_request` | 400 | 参数缺失/非法、路径越界、run_id 非法 |
| `not_found` | 404 | run/job/pack/静态资源不存在 |
| `conflict` | 409 | 预留（MVP 不排队，未启用） |
| `internal_error` | 500 | 未捕获异常（消息已打码） |

## 3. 接口

### `GET /api/status`

```json
{
  "workspace": "/abs/path",
  "atoms": 24,
  "packs": 3,
  "regression_count": 7,
  "lessons_count": 1,
  "llm_enabled": false,
  "http_backend": "requests",
  "scenarios": 4,
  "version": "0.1.0"
}
```

### `GET /api/atoms?domain=&tag=&category=`

返回数组，元素与 `panguverse atoms --json` 完全一致（`name/description/category/domain/inputs/outputs/input_ports/output_ports/required/tags/aliases/version/effects`…）。服务端过滤，无匹配返回 `[]`。

### `GET /api/packs` / `GET /api/packs/{name}`

列表项：`{name, version, domain, builtin, description, atom_count, capabilities, semantics}`。
详情额外带 `atoms: [name…]` 与 `manifest`（`packs manifest` 里该包的原始 dict）。未知包 404。

### `GET /api/scenarios`

```json
[{"id": "scenarios/api_smoke.yaml", "name": "API 冒烟与鉴权", "source": "builtin", "cases": 3}]
```

- `source=workspace`（`<workspace>/scenarios/**`）优先，其余 `builtin`；工作区文件 id 为工作区相对路径，可直接回传给 `POST /api/runs`。
- 解析失败的文件仍会返回，带 `"error": "<原因>"` 且 `cases: 0`。

### `POST /api/runs` → `202`

```json
{"mode": "demo | scenario | intent | regression",
 "scenario": "scenarios/api_smoke.yaml",
 "intent": "检查健康状态并验证登录下单",
 "env": {"BASE_URL": "http://127.0.0.1:8899"},
 "regression": true,
 "no_llm": false}
```

返回 `{"job_id": "3f9c1a2b7d4e", "status": "queued"}`。校验：

- `mode` 必须是四种之一；`demo` 忽略 `scenario`/`intent`（等同 `demo e2e`，默认含回归复用，`regression=false` 可关）。
- `scenario` 模式必填且路径必须落在工作区/内置场景内（越界 400）。
- `intent` 模式要求 trim 后非空。
- `env` 必须是 `string → (string|number|bool)` 的对象。
- `regression` 模式忽略其余字段，直接读沉淀库（不重新规划，`plan.source=memory`）。

#### env 解析与持久化（v1.3）

1. **基线**：每次「成功的运行」（产出报告且 `summary.success=true`）后，把解析后的 env 原子写入
   `<workspace>/.panguverse/web/last_env.json`（权限 `0600`）。
2. **优先级**：`mode=regression` 以基线 env 为准、请求 `env` 覆盖同名键；`demo|scenario` 请求 `env` 优先、
   缺失的同名键回落基线。执行时最终 env = 基线 < 计划 `env`（含场景 `env`） < 请求 `env`。
   `mode=intent` 在**请求未提供任何 env** 时**不**回落基线（A8 要求确定性失败，且静默继承上一个目标可能打错服务）；
   提供了部分 env 时，缺失的同名键仍会回落基线。
3. **FS_ROOT 边界**：Web 层始终注入 `FS_ROOT=<workspace>/.panguverse/fs-demo`（请求显式给出 `FS_ROOT` 时尊重请求），
   保证快照/归档等文件绝不写到 workspace 之外（`packs/local_fs.py` 缺省回退 `Path.cwd()` 的行为保持不变）。
4. **demo 的基线可复现（v1.3）**：demo 用进程内临时 mock 做被测目标，端口随进程消失，所以基线**不写临时端口**，
   而是写入目标标记：

   ```json
   {
     "version": 2,
     "env": {"BASE_URL": "http://127.0.0.1:8899", "API_USERNAME": "demo", "API_PASSWORD": "***", "...": "..."},
     "target": {"kind": "mock", "flaky_failures": 2, "token_ttl": 3600.0}
   }
   ```

   `mode=regression` 读到 `target.kind == "mock"` 时，按同样参数**自动拉起进程内 mock** 并把 `BASE_URL`
   改写为新端口后再执行（不复用旧端口）；请求显式给 `BASE_URL` 时尊重请求。因此「干净工作区 → demo（无 env）
   → regression（无 env）」不依赖任何外部服务即可 7/7。
5. **缺变量即失败**：规划完成后扫描计划里仍未解析的 `${env.X}`（`|default:` 兜底的除外），
   若有则 job 置 `failed`，`error="缺少环境变量: BASE_URL"`（多个逗号分隔），`validate` 阶段标 `failed`，
   **不会**产出「done 但全部用例 errored」的假成功。

### `GET /api/jobs` / `GET /api/jobs/{job_id}`

最近 20 条（`created_at` 倒序）。字段：

```json
{
  "job_id": "3f9c1a2b7d4e",
  "mode": "demo",
  "status": "queued | running | done | failed",
  "created_at": 1793367213.7,
  "started_at": 1793367213.7,
  "finished_at": null,
  "elapsed_ms": 500,
  "run_id": "4afa3f90737e",
  "phases": [{"name": "plan", "status": "ok", "detail": "7 个用例来自 spec"}],
  "summary": {"total": 7, "passed": 7, "failed": 0, "errored": 0, "skipped": 0, "healed_cases": 1, "duration_ms": 140},
  "conclusion": {"verdict": "通过", "success": true, "exit_code": 0},
  "error": null
}
```

- 内存态，服务重启即清空；`phases` 在执行中逐步追加：`plan → validate → execute → heal → report → evolve → regression`。
- 单 job 超时 300s（`PANGUVERSE_WEB_JOB_TIMEOUT` 可覆盖），超时置 `failed` + `error="timeout"`；异常置 `failed` + 打码后的一行消息。
- 未知 `job_id` → 404。

**状态语义（重要）**：
- `done`：执行完成并写出了报告。真实断言失败（有 `passed` 或 `failed > 0`）仍是 `done`，结果看 `conclusion.success`。
- `failed`：拿不到可用结果——规划/校验异常、超时、缺少必需环境变量、回归库为空，以及
  **`summary.passed == 0` 且全部用例 `errored`**（基础设施不可达不是「测试没通过」，v1.3）。此时 `error` 给首个错误原因。

### 运行历史与报告

- `GET /api/runs?limit=50` → `[{run_id, dir, started_at, finished_at, duration_ms, summary, conclusion, meta}]`；
  扫描 `<workspace>/runs/`，不存在时退回 `<workspace>/.panguverse/runs/`；忽略没有 `report.json` 的目录，按 `started_at` 倒序。
- `GET /api/runs/{run_id}` → `{"report": <report.json 原文>, "plan": <plan.json 或 null>}`；`run_id` 非法 400。
- `GET /api/runs/{run_id}/report.html` → `text/html; charset=utf-8`。**服务时按同一套 env 打码规则重新渲染**
  （标题沿用磁盘文件），磁盘 HTML 保持原样（A16，v1.4）。
- `GET /api/runs/{run_id}/junit.xml` → `application/xml` + `Content-Disposition: attachment; filename="panguverse-<run_id>.xml"`；
  同样从打码后的报告数据重新渲染，避免 XML 旁路。
- 文件缺失 → 404。

### 测试报告 AI 总结（v3.0，契约 `tasks/web-ai-insight-spec.md`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/runs/{run_id}/insight` | 有缓存 → `200 {available: true, ...}`；无缓存 → `200 {"available": false}`，**不自动生成** |
| `POST` | `/api/runs/{run_id}/insight` | body `{"refresh": false}`；`refresh=true` 强制重算，否则缓存有效时直接复用 |

- 缓存 `<workspace>/.panguverse/web/insights/<run_id>.json`（目录 `0700`、文件 `0600`、临时文件 + `os.replace` 原子写），
  内容就是响应体；`report.json` 的 `mtime` 晚于缓存即视为过期。
- `source` 只有 `llm` / `rules` 两种。无 LLM 密钥（常见情况）→ `rules` +
  `degraded_reason="未配置 LLM 密钥，使用离线规则总结"`；LLM 异常、超时（单次上限 25s）或返回不是合法 JSON
  → 仍然 `200`，`source="rules"` 并在 `degraded_reason` 写明原因。**只有 `run_id` 非法（400）或 run 不存在（404）才报错。**
- `stats` 永远取自 `report.summary` 的**全字段**（含 `steps` / `http_requests` / `success`，逐字相等），
  LLM 不参与任何统计数字。
- 规则内容只从 `report.json` 取数：失败按 `failure_kind` / `error.kind` / 状态码归为
  `config`（沙箱文件缺失）、`auth`（401/403）、`engine`（capture 路径不存在 / 目标不可达）、`assertion`（正文或期望不符）、
  `flaky`；`findings`/`suggestions` 里的 `case_ids` 一律来自报告。`plan.source == "memory"` 且存在失败时，
  追加 `mixed_target`（回归库混跑不同被测目标）并给出一条 P1「按场景分组回归」建议。
- **LLM 输出确定性校正（v3.0.1 §3.5）**：模型只负责措辞，数字/归类/优先级不采信——
  `stats` 全字段覆盖；`findings` 的 `kind`/`case_ids`/`detail` 由报告重建（错归类的 case_id 剔除、模型漏掉的类用规则补齐）；
  有失败时强制至少一条覆盖配置/环境失败的 P0（模型没给就由规则注入）；`summary_md` 的计数句与用例清单按校正后数据重新渲染；
  标题含数字或与结果矛盾时回退规则结论句。每次校正写入响应/缓存的 `corrections[]`（`{"field","reason"}`）便于审计。
- LLM 输入只放打码后的摘要：`summary`、`plan.source`、失败用例/步骤的 `case/step/message/expected/actual/failure_kind`、
  env **键名**；复用 `mask_env_payload` + `core/redact.mask_obj`，体积 ≤ 40KB。输出落盘/返回前再按同一套规则打码，
  因此响应与缓存文件里不会出现 env 值、密码或 token。

### 任务定义 CRUD（v2.0，契约 `tasks/web-tasks-crud-spec.md` §2–§3.3）

任务定义落盘 `<workspace>/.panguverse/tasks/<task_id>.json`（目录 `0700`、文件 `0600`，含 env 明文，
与 `last_env.json` 同策略），写文件走「临时文件 + `os.replace`」原子替换；目录不存在时自动创建。
**刻意不放 `<workspace>/tasks/`**：仓库里 `tasks/` 是契约文档目录，`--workspace .` 时会撞名，
而 `.panguverse/` 已被 `.gitignore` 覆盖，运行时数据集中存放。
`task_id` **只能由服务端生成**（`^t-[0-9a-f]{6,12}$`），所有路径拼接前都先做格式校验，
客户端字符串永远不会被当成路径。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tasks` | `{"tasks": [<摘要>]}`，按 `updated_at` 倒序 |
| `GET` | `/api/tasks/{task_id}` | 完整任务（env 值经 `mask_env_payload` 打码） |
| `POST` | `/api/tasks` | 新建，`201` + 打码后的任务 |
| `PATCH` | `/api/tasks/{task_id}` | 部分更新，`200`；未知 id `404` |
| `DELETE` | `/api/tasks/{task_id}` | `204`，无响应体且不带 `Content-Type`；**只删任务定义，不删 `runs/` 产物** |
| `POST` | `/api/tasks/{task_id}/run` | `202 {job_id}`；可选 `{"env": {...}}` 仅本次覆盖 |
| `GET` | `/api/runs/{run_id}/config` | 从磁盘还原该次运行的配置（env 打码） |
| `POST` | `/api/runs/{run_id}/rerun` | `202 {job_id}`；可选 env 覆盖 / `save_as_task` |
| `POST` | `/api/runs/{run_id}/save_as_task` | `201` 打码后的 task；**只建任务、不运行、不产生新 run** |

摘要字段固定为 `task_id,name,mode,scenario,intent,updated_at,last_run_id,last_status,env_keys`
——**只给 env 键名，不给值**。

请求体（`POST`；`PATCH` 为同一批字段的子集）：

```json
{
  "name": "订单闭环冒烟",
  "mode": "demo | scenario | intent | regression",
  "scenario": "scenarios/api_smoke.yaml",
  "intent": "",
  "env": {"BASE_URL": "http://127.0.0.1:8899", "API_PASSWORD": "demo123"},
  "regression": false,
  "no_llm": false
}
```

校验规则：

- `name` 必填，trim 后 1–80 字符；同名允许（用 `task_id` 区分）。
- `mode` 必须是四种之一；`mode=scenario` 时 `scenario` 必填且经 `realpath` 前缀校验（越界/不存在 400）；
  `mode=intent` 时 `intent` 非空。
- `env` 为 `string → (string|number|bool)`；`null` 只在 `PATCH` 里有意义（删除该键）。
- `task_id/created_at/updated_at/last_run_id/last_status` 由服务端维护，请求里出现会被忽略。

**PATCH 的 env 键级合并**（最易写错的一条）：

| 传入值 | 语义 |
| --- | --- |
| `"***"` | 沿用已保存的旧值（前端把密钥显示成 `***`，用户不动就不覆盖） |
| `""` | 同上，沿用旧值（表单留空表示不改） |
| `null` | 显式删除该键 |
| 其他 string/number/bool | 覆盖/新增 |

**运行任务**：`POST /api/tasks/{id}/run` 等价于用任务里保存的 `mode/scenario/intent/env/regression/no_llm`
提交 `/api/runs`；body 里的 `env` 只作用于本次运行，**不写回任务定义**。覆盖值里 `"***"`、`""`、`null`
均表示「本次不覆盖该键」（与 PATCH 的占位语义一致，表单留空不会把已保存的值清空）。运行结束后服务端只在 job 进入
`done`/`failed` 时回写 `last_run_id` / `last_status`（不改 `updated_at`，也不动其它字段；回调在状态切终态时同步执行，
客户端看到终态即可读到最新值）。

**从历史取配置**：`GET /api/runs/{run_id}/config` 返回

```json
{
  "mode": "scenario",
  "scenario": "scenarios/api_smoke.yaml",
  "intent": "",
  "env": {"BASE_URL": "http://127.0.0.1:8899", "API_PASSWORD": "***"},
  "regression": false,
  "no_llm": false,
  "plan_source": "spec",
  "error": {"case": "认证链路…", "step": "登录", "message": "…", "expected": null, "actual": null}
}
```

- 数据源是磁盘 `report.json` 的 `meta.run_config`/`meta.plan_source` 与 `plan.json`（后者未打码，是重跑的真实基线）。
  `meta.run_config` 是 v2.0 的 **additive** 字段（mode/scenario/intent/regression/no_llm/uses_mock），旧字段语义未变。
- **旧报告的退化规则**：没有 `run_config` 时按 `plan_source` 推断（`memory` → `regression`，其余 → `scenario`），
  并在 `plan_source` 里返回实际来源；旧 demo 运行的 `mode` 无法还原时会得到 `scenario` 且缺 `scenario`，
  rerun 直接 400（不猜目标，避免打错服务）。
- `env` 在 JSON 出口统一走 `mask_env_payload`（键名保留、敏感值 `***`）。
- `error` 取**第一个非 passed 步骤**（用例名 + 步骤名 + `message/expected/actual`，消息再经 `jobs.sanitize_message`（`core/redact.py`）打码）；
  全部通过时为 `null`。缺字段给默认值，不返回 500。

**从运行直接存为任务（不运行）**：`POST /api/runs/{run_id}/save_as_task`，body 仅 `{"name": "可选"}`

- `201` 返回**打码后**的任务（`env` 值 `***`）；不产生新 run，`runs/` 目录数不变。
- `env` 由服务端从磁盘（`plan.json` 优先）复制**真实值**，前端回传的 `"***"` 一律忽略、不会当真值；
  demo/mock 目标继承的 `BASE_URL` 不会写进任务定义。
- `mode/scenario/intent/regression/no_llm` 由 `meta.run_config` 推断，旧报告走上面的退化规则；
  无法判定目标（如缺 `scenario`/`intent`）→ `400`，不猜。
- `name` 缺省为 `重跑 <run_id>`；未知 run → `404`。

**重跑**：`POST /api/runs/{run_id}/rerun`

```json
{"env": {"API_PASSWORD": "***"}, "save_as_task": true, "name": "修正后的任务"}
```

- 服务端从磁盘取真实 env 作基线，请求里的 `"***"`/空串视为沿用，`null` 视为该键不传递；
- `mode=demo`（或 `regression` 且上次目标是进程内 mock）时，继承的 `BASE_URL` 会被丢弃并重新拉起 mock
  ——旧端口随进程消失，沿用必然连不上；
- `save_as_task=true` 时先创建任务定义（写入本次合并后的 env 真值）再运行，`name` 缺省为 `重跑 <run_id>`；
- 未知 run → 404；无法还原场景（缺 `scenario`）→ 400。

### 项目 / 套件 / 批量运行（v4.0，契约 `tasks/web-refactor-spec.md`）

三级结构：**Project → Suite → Run**。项目与套件落盘，运行仍落 `runs/`。

**落盘**：`<workspace>/.panguverse/projects/<project_id>.json`（目录 `0700`、文件 `0600`、原子写）。
`project_id` / `suite_id` 一律**服务端生成**（`^p-[0-9a-f]{6,12}$` / `^s-[0-9a-f]{6,12}$`），
客户端传了也忽略；路径拼接前先做正则校验。项目文件含 env 明文（与 `tasks/`、`last_env.json` 同策略），
**只有 HTTP 出口打码**。

```json
{
  "project_id": "p-3f2a1c", "name": "coding-log-web",
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

**env 优先级**（高 → 低）：`suite.env` > `project.env` > 本次请求 `env`；Web 层注入的默认值
（如 `FS_ROOT` 指向 workspace 内 `fs-demo`）**只在三层都没声明该键时兜底**，绝不覆盖任何显式值，
也不被上一次成功运行留下的 `last_env` 基线顶掉。
PATCH 的 env 是**键级合并**，三态与任务 CRUD 完全一致：`"***"`/空串 = 沿用旧值、`null` = 删键、其余 = 覆盖。
`scenario` 与 `POST /api/runs` 同源校验（`realpath` 前缀：仅 workspace 或仓库内置 `scenarios/`），越界 400。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/projects` | `{"projects":[{project_id,name,base_url,suite_count,last_run,pass_rate}]}`，按 `updated_at` 倒序 |
| `POST` | `/api/projects` | `{name, base_url?, env?}` → `201` 项目（env 打码） |
| `GET` | `/api/projects/{pid}` | 项目详情（env 打码、`suites` 原样），额外带 `suite_count/last_run/pass_rate` |
| `PATCH` | `/api/projects/{pid}` | 部分更新 `name/base_url/env` → `200` |
| `DELETE` | `/api/projects/{pid}` | `204`，**不删任何 run**（运行归属存在报告 `meta` 里） |
| `POST` | `/api/projects/{pid}/suites` | `{name, scenario, env?, tags?}` → `201` suite |
| `PATCH` | `/api/projects/{pid}/suites/{sid}` | 部分更新 `name/scenario/env/tags` → `200` |
| `DELETE` | `/api/projects/{pid}/suites/{sid}` | `204` |
| `GET` | `/api/projects/{pid}/summary?limit=5` | `{recent_runs, pass_rate, failure_top}` |
| `POST` | `/api/projects/{pid}/run` | `{suite_ids?, env?}` → `202 {batch_id, jobs:[{job_id,suite_id}]}` |
| `GET` | `/api/batches/{batch_id}` | `{batch_id, project_id, status, suites[], aggregate}` |
| `GET` | `/api/runs/query?...` | `{total, items[]}` |

**运行归属**：从项目/套件发起的运行，报告 `meta` 里新增 `project_id` / `suite_id`（additive，旧字段不变）。
旧报告没有这两个键 → 历史里归为「未归类」，列表与筛选都**不报错**。

- `last_run`：该项目最新一条运行 `{run_id,status,verdict,at,passed,total}`，无运行 → `null`；
  `status` 由报告 `summary` 推导（`errored` > `passed` > `failed`，缺 `summary` 的老报告 → `done`）。
- `pass_rate`：`{passed,total}` = 该项目**全部**运行按用例数累加，无运行 → `null`。
- `summary.recent_runs`：最新 `limit` 条（`limit` 1–50，默认 5）；`pass_rate` / `failure_top` 仍按**全量**统计。
- `failure_top`：遍历项目全部运行里 `failed/error` 的用例，按报告 `error.failure_kind`（缺失则退回 `kind`，再缺 `unknown`）
  聚合，`count` = 命中用例数，`cases` = 去重后的 `case_id`；按 `count` 降序、同数按 kind 升序，**最多 3 条**。

**批量运行语义**：

- 缺省跑该项目全部套件；`suite_ids: []` 也视为全部（前端「一键全跑」）；显式给出未知 id → `400`。
- **串行**：batch 内前一个 job 进入终态后才启动下一个；每个套件一个 job + 一个 run，响应里 `jobs` 先返回全部 job_id。
- 单套件失败（场景文件损坏、目标不可达、计划为空等）**不中断**其余套件。
- `GET /api/batches/{id}` 的 `status`：`queued|running|done|failed`；只有「有套件拿不到报告」（job 失败）才 `failed`，
  全部跑完但有用例失败仍是 `done`。
- `aggregate` 逐项来自各 run 的磁盘报告 `summary`：`passed`/`total` 为求和，`suites_passed` = 全部用例通过的套件数，
  `suites_failed` = 其余套件数（两者之和 = 套件总数），`duration_ms` = 各 run 真实 `duration_ms` 之和（串行，可逐条对账）。
- batch 与 job 都在内存，重启即丢；`runs/` 落盘不受影响。

**历史筛选分页**：`GET /api/runs/query?project=&suite=&status=&offset=0&limit=20`

- `project` / `suite`：省略或 `all` = 不限；`unclassified`（也接受 `none`/`_`）= 没有归属的旧报告；
  其余值按 id 正则校验，非法 → `400`（已删除项目的运行仍可按原 id 查到）。
- `status`：`all`（默认）/`passed`/`failed`/`errored`/`done`；`done` = 已跑完的任意结论（磁盘上的运行都算）；
  其余按报告 `summary` 精确匹配。非法值 → `400`。
- `offset` ≥ 0（负数 `400`）；`limit` 1–200（默认 20）。`total` 是筛选后的总数，与分页无关；
  `items` 字段与 `/api/runs` 数组元素一致，另加 `status` / `project_id` / `suite_id`。
- **兼容**：`/api/runs` 仍返回数组（形状未变），筛选分页只走 `/api/runs/query`。

### 静态资源

- `GET /` → `panguverse/web/static/index.html`；资源未就绪 → 503（文案「前端资源未就绪」）。
- `GET /static/<file>` → 只允许白名单四个文件，其余 404。
- 前端使用 hash 路由（`#/overview`），因此只有 `/overview`、`/run`、`/history`、`/runs/<id>`、`/atoms`、`/packs` 这些深链会回落到 `index.html`；
  其余未知路径返回 **JSON 404**。这是对规格 §3.8 的收紧：避免 `/etc`、`/cli.py` 之类路径被 200 掩盖（A10/A11 要求 404）。

## 4. 代理直连（US-11）

`panguverse/http/client.py` 对回环目标（`127.0.0.0/8`、`::1`、`localhost`、`*.localhost`、`0.0.0.0`）**强制直连**：

- `requests`：逐请求传 `proxies={"http": None, "https": None}`（不改 `session.trust_env`，避免污染同一会话的后续非回环请求）；
- `urllib`：使用 `build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))` 的专用 opener；
- 非回环地址行为不变，仍尊重系统/环境代理。

因此 `HTTP_PROXY=http://127.0.0.1:1`（必死端口）+ `NO_PROXY` 清空时，demo 仍 `7/7`，`pytest` 无需 `NO_PROXY` 全绿。

## 5. 任务模型与并发

- `panguverse/web/jobs.py`：`dict[job_id] → Job` + `threading.Lock`；每个 job 一条 daemon 线程，不排队（MVP 单用户，前端限制一次只发一个）。
- `panguverse/web/batches.py`：批次表（内存）+ 串行调度线程；套件逐个 `JobManager.start`，前一个终态后才启动下一个。
- 执行顺序与 `PanguAxe.loop` 一致：`plan → validate → executor.run（含重试/自愈）→ report（json/html/junit + plan.json）→ evolve → regression`。
- 报告一律经 `panguverse.report.write_all` 生成（内部按密钥打码）；`/api/runs/{id}` 直接回磁盘 `report.json`，不重新序列化。

## 6. 已知限制

- job 表与 batch 表都在内存：重启后历史只剩磁盘上的 `runs/`（前端应回退到「运行历史」）。
- 无认证、无多用户、无 SSE：仅本机单人使用；实时进度靠轮询。
- 长任务只做「超时标记」，不强行中断执行线程（线程为 daemon，进程退出即回收）。
- 回归库/经验库/`last_env.json` 是工作区文件，多个 job 并发写入时以「最后写入」为准（单用户场景可接受）。
- env 基线只在「成功运行」后更新；一次失败的运行不会污染下一次的默认目标。
- `axe/llm.py` 的 LLM 请求回环地址（本机 ollama）已走 `ProxyHandler({})` 直连，与 `http/client.py` 规则一致；非回环地址仍尊重系统/环境代理。

## 7. 工作台（Studio，契约 `tasks/workbench-accuracy-spec.md` v6.0）

工作台把 planner 的线性计划转成图，并提供「草案 → 人工裁决 → 隔离调试 → 审阅发布 → 绑定项目运行」的后端能力。
实现：`panguverse/web/graph.py`（纯函数）、`panguverse/web/studio.py`（落盘/审计/调试）。

### 7.1 落盘

```
<ws>/.panguverse/studio/<wid>/workflow.json           # 指针：state/current_rev/locked/published_ref
<ws>/.panguverse/studio/<wid>/revisions/<rev>.json    # 不可变：nodes/edges/env_schema/message/created_by
<ws>/.panguverse/studio/<wid>/audit.jsonl             # append-only：state/publish/diff/revision/debug
<ws>/.panguverse/studio/<wid>/proposals/<rev>.json    # replan 提案（未应用）
<ws>/.panguverse/studio/<wid>/debug/<sid>.jsonl      # 异步调试会话增量（0600，不进 runs）
<ws>/.panguverse/studio/<wid>/debug/<sid>.json       # 完成后的调试快照（0600）
<ws>/.panguverse/studio/<wid>/ui.json                 # 画布坐标（不产生新 rev）
<ws>/.panguverse/studio/<wid>/published/<rev>.json    # 发布记录（只写一次）
<ws>/.panguverse/executions/<run_id>/execution.json   # trigger=task|studio|debug|manual
```

目录 `0700`、文件 `0600`，均原子写（临时文件 + `os.replace`）。版本文件与发布记录**只写一次**；
除 `workflow.json` 指针外，任何接口都不修改既有版本。

### 7.2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/studio/drafts` | `{intent, domain_pack?, exclude_atoms?, allow_llm?}` → 201 `{wid, rev:0, state, planner, plan_source, confidence, assumptions, capability_gaps, graph, validation}`；无法表达 → **422 `capability_gap`**（无 graph） |
| GET | `/api/studio/drafts` | `?status=&domain_pack=` → `{drafts:[...]}` |
| GET | `/api/studio/{wid}` | `?rev=` 取图 + 元信息 + `ui` |
| PATCH | `/api/studio/{wid}/nodes/{nid}` | 改参数/改名/锁定/替换原子/`case_id` → 新 rev（`origin=human`） |
| POST | `/api/studio/{wid}/nodes` | `{atom, params?, ..., after?}` → 新 rev（201） |
| DELETE | `/api/studio/{wid}/nodes/{nid}` | 删除节点与相关边 → 新 rev |
| POST | `/api/studio/{wid}/edges` | `{from,to,via?,kind?}` → 新 rev（201） |
| POST | `/api/studio/{wid}/replan` | `{instruction}` → `{diff, proposed_rev}`；不自动应用 |
| POST | `/api/studio/{wid}/apply-diff` | `{proposed_rev, accepted:[change_id]}` → 新 rev |
| POST | `/api/studio/{wid}/validate` | `{rev?, checks?:{intent?,env?,capability?}}` → `{errors,warnings,risks,affected_nodes,coverage,missing_env,capability_gaps}` |
| POST | `/api/studio/{wid}/debug` | `{rev?, node_id, mode, project_id?, env?}` → **202** `{session_id,total,cursor,done:false}` |
| GET | `/api/studio/debug/{session_id}` | `?cursor=` → `{session_id,cursor,done,nodes:[{node_id,status,reason,attempts,duration_ms,error,inputs,outputs}],summary}` |
| POST | `/api/studio/{wid}/review` | `{base_rev?, rev}` → `{diff, risks, validation, audit_tail}` |
| POST | `/api/studio/{wid}/state` | `{to:"review|approved|rejected", note?, risk_ack?, confirmed_risks?}` |
| POST | `/api/studio/{wid}/publish` | `{rev, message}` → `{workflow_ref:"<wid>@<rev>", reused, task}` |

节点坐标：`PATCH .../nodes/{nid}` 只带 `{"ui":{"x":..,"y":..}}` 时只更新 `ui.json`，**不产生新 rev**。
其它字段变更一律产生新 rev，并把 `state` 重置为 `draft`（旧批准失效）。

### 7.2a 规划来源与能力缺口（v6.0 §2）

- 草稿响应显式标注 `planner` / `plan_source`（`llm|rules|template`）、`confidence`、`assumptions[]`、
  `capability_gaps[]`；规则/模板来源固定低置信度（≤0.5），界面必须提示人工核对。
- 规则/模板必须**领域 + 动作双重命中**；不命中不产出图，直接 422：
  ```json
  {"error":{"code":"capability_gap","message":"...","missing_atoms":["web_screenshot","ocr_image"],
            "suggestions":["需要新增原子：..."],"intent":"..."}}
  ```
- LLM 规划（`allow_llm:true`）的提示词包含**原子清单 + 端口契约**；输出逐节点校验原子存在，
  发明原子按 `capability_gap` 处理，**不静默丢节点**；LLM 失败不回退演示模板。
- 领域包 `web_research` 提供 `http_get_page` / `html_extract_text` / `html_extract_structure` /
  `llm_summarize`，全部只读；`llm_summarize` 无密钥 → `missing_llm` 硬失败，不产出占位。

### 7.2b 语义校验与发布阻断（v6.0 §3）

`validate` 在三类检查默认全开（可用 `checks:{intent:false,...}` 关闭）：

| 检查 | 规则 | 错误码 |
| --- | --- | --- |
| 意图覆盖 | 非 helper 节点须映射到意图动作/实体词；`ratio < 0.6` → error | `intent_mismatch` |
| 环境就绪 | `${env.X}` 必须在基线 env / 请求 env / assumptions 中 | `missing_env` |
| 能力缺口 | 意图动作没有对应原子 | `capability_gap` |

响应固定带 `coverage:{matched,total,ratio}`（含 `matched_nodes/unmatched_nodes`）、`missing_env[]`、
`capability_gaps[]`。`publish` 遇到 `intent_mismatch` / `capability_gap` 返回 **409** 并回吐
`details.validation` 与 `details.blocking[]`。

### 7.3 图语义（不伪造依赖）

- 节点字段：`{id, atom, name, params, capture, when, foreach, as, retry, ..., case_id, origin, locked}`。
- **数据边**：扫描节点 `params/when/foreach/extra` 里的 `${var}`，以及 `capture` 键；只有能追到**前面**步骤
  （步骤 id 或 capture 名）时才建 `kind:"data"`、`via:"<变量根名>"` 的边。
- **顺序边**：相邻节点没有直接数据边时补 `kind:"order"`，固定 `order_only:true`、`via:null`。
- 引用了找不到生产者的变量 → **不建边**，由 `validate` 报 P101；绝不把未知变量伪装成数据依赖。
- `POST /edges` 若声明 `kind:"data"`，`via` 必须是 `from` 节点产出的变量，否则 400。

### 7.4 锁定 / 提案 / diff

- `locked=true` 的节点：`replan` 与 `apply-diff` 一律不得修改。
- `replan` 先逐节点比对签名（`atom/name/params/capture/when/foreach/extra/...`），
  锁定节点被删除或签名变化 → **409**，错误体额外带 `locked_nodes:["..."]`；不落提案。
- `apply-diff` 只应用 `accepted` 里的 `change_id`；接受删节点但拒绝删边时，悬空边会被丢弃并在
  `dropped_edges` 中列出。提案 `base_rev` 与当前 rev 不一致 → 409「提案已过期」。
- 每个变更都有稳定 id：`node:<id>:added|changed|removed`、`edge:<id>:added|removed`。

### 7.5 发布门禁

- `state` 迁移只接受 `review|approved|rejected`。
- `to:"approved"` 时若存在风险（`mutate/destructive` 副作用、密钥读写）：必须带 `risk_ack:true`，
  或用 `confirmed_risks` 覆盖全部 `risk.id`，否则 **409 `risk_unconfirmed`** 并返回 `risks[]`。
- `publish`：必须 `state=approved`、`rev=current_rev`、`message` 非空；否则 409/400。
- 发布生成 `workflow_ref="<wid>@<rev>"`，写 `published/<rev>.json`；旧版本文件永不回溯修改。
  同一 rev 重复发布幂等返回原记录（`reused:true`）。
- 发布成功会按 **`wid` 维度**幂等创建/复用一条 `mode:"workflow"` 任务定义，并在响应的
  `task` 字段返回（`task_id`、`name`、`mode`、`workflow_ref`、`env` 等；`env` 沿用统一打码规则）：
  - 首次发布（或原任务已删除）新建任务：`name="<工作流名> 任务"`、`env={}`、
    `regression=false`、`no_llm=false`。
  - 同一 `wid` 再次发布复用原任务：同一 rev 的 `reused:true` 或发布新 rev 都只更新
    `workflow_ref="<wid>@<rev>"` 与 `updated_at`，`task_id`/`name`/`env` 不变。
  - 已有手工创建的同 `wid` workflow 任务同样会被复用；发布后任务立即可在
    `GET /api/tasks` 看到。归档任务不参与复用。

### 7.6 调试隔离与环境限制

- 每次调试使用独立 `RunContext`（独立 run_id），结果只写 `studio/<wid>/debug/`，**不写** `runs/`，
  因此 `GET /api/runs` 与 `/api/runs/query` 永远看不到调试记录。
- `mode`：`only`（单节点）、`from_start`（从头到该节点）、`downstream`（从该节点到末尾）。
- 环境只能来自：工作区 `last_env` 基线，或 `project_id` 对应项目的 `env`（项目 env 优先）。
- 请求体若出现 `base_url` / `url` / `target` / `endpoint` 等 URL 类字段，或 `env` 里出现 URL 类键
  （`BASE_URL`、`*_URL`、`HOST`…）→ **400**；`env` 里给出基线不存在的键、或值与基线不同，同样 400。
- **异步会话**：`POST /debug` 立即返回 202 `{session_id}`；后台线程逐节点执行并写
  `debug/<sid>.jsonl`（0600）。`GET /api/studio/debug/{sid}?cursor=N` 增量返回
  `nodes:[{node_id,status,reason,attempts,duration_ms,error,inputs,outputs}]` 与 `summary:{passed,failed,skipped,by_reason}`。
- 节点状态：`pending|running|passed|failed|skipped`；`skipped/failed` 必带原因码：
  `when_false|foreach_empty|upstream_failed|optional|assertion|network|missing_env|missing_llm|missing_input|cancelled`。
- **`nodes` 是事件流，不是节点清单**：同一 `node_id` 会依次出现 `pending → running → passed/failed/skipped`
  多条记录（`cursor` 随事件递增）。消费方必须按 `node_id` 取**最新一条**作为当前状态，不要把它当成重复节点。
- 会话完成后额外写 `debug/<sid>.json` 快照（兼容旧读取）；两者都只存打码后的副本，**不进 `/api/runs`**。

### 7.7 审计

`audit.jsonl` 每行一条 JSON，append-only：

```json
{"action":"state|publish|diff_applied|revision|draft_created|debug","actor":"admin","wid":"w-...",
 "from":"draft|0|null","to":"approved|w-...@1|2","rev":1,"note":"...","ts":...,"at":"...Z"}
```

`actor` 取自登录会话（关闭认证/单测时为 `local`）。`review` 返回最近 20 条 `audit_tail`。

### 7.8 已发布流程绑定项目

- 项目套件新增可选字段 `workflow_ref`（与 `scenario` **二选一**）：
  `POST /api/projects/{pid}/suites {"name":"...","workflow_ref":"w-xxx@0"}`。
- `POST /api/projects/{pid}/run` 会为 `workflow_ref` 套件创建一个 `mode:"workflow"` job，只读发布版本执行，
  不再调用 planner/LLM。
- 报告 `meta`、`meta.run_config` 与 `GET /api/runs/{id}/config` 都带 `workflow_ref`；
  `.panguverse/executions/<run_id>/execution.json` 记录 `trigger`、`workflow_ref`、`project_id`、`suite_id`，
  可从报告/执行回溯到 `wid@rev`。
- 也可直接 `POST /api/runs {"mode":"workflow","workflow_ref":"w-xxx@0","env":{...}}`；
  任务定义同样支持 `mode:"workflow"` + `workflow_ref`。
- 运行仍走既有 `_execute`：真实报告、打码、`/api/runs` 数组形状、认证与错误体均不变。

### 7.9 错误体

统一为 `{"error":{"code","message", ...}}`；仅工作台冲突会追加结构化字段：

- 锁定冲突：`locked_nodes:["node_id"]`
- 未确认风险：`risks:[{id,node_id,kind,level,message}]`
- 悬空/非法变更：`edges:[...]`（如适用）

## 8. W8 收口（契约 v5.1 §5.1）

P0 补齐执行中心、软归档、报告签署/作废；原子启用/停用明确为 **P1，不在本轮实现**。
统一审计落 `<ws>/.panguverse/web/audit.jsonl`（append-only、`0600`），每条含
`actor/action/target/from/to/note` 与 `ts/at`。

### 8.1 执行列表 / 详情

`GET /api/executions?project=&status=&offset=&limit=`

- 数据源：磁盘 `runs/` + 内存 job **合并**；同一 `run_id` 只保留一条（job 状态覆盖运行态）。
- 字段：`eid/execution_id/run_id/job_id/batch_id/mode/trigger/project_id/suite_id/workflow_ref/status/run_status/passed/total/duration_ms/started_at/finished_at/error/conclusion/summary/phases/cancellable`。
- `eid`：有 `run_id` 时等于 `run_id`，否则为 `job_id`；详情/取消都接受 `eid`、`job_id`、`run_id`。
- `status` 过滤：`all|queued|running|done|failed|cancelled`，也兼容 `passed|failed|errored`（按报告结论）。
- `project`：项目 id；`unclassified|none|_` 表示无项目归属。
- `offset ≥ 0`，`limit` 1–500（默认 50）；返回 `{executions,total,offset,limit}`。

`GET /api/executions/{eid}` → 单条执行详情（含 `phases`、`summary`、`conclusion`）。

### 8.2 取消执行

`POST /api/executions/{eid}/cancel` → `{eid, job_id?, batch_id?, status, cancelled_suites?}`

- **job**：设置协作取消标记；执行器在**当前步骤结束后**停止，剩余步骤记 `skipped`；
  `status="cancelling"`，job 终态变为 `cancelled`，已产生的报告保留。
- **queued job**：立即 `cancelled`。
- **batch**：`eid` 可以是 batch id，或属于该 batch 的 job/run id。已完成的 run 保留；
  未开始的套件标记 `cancelled` 且不产生 run；正在运行的套件按 job 语义停。
- **已结束**（done/failed/cancelled）或不存在 → **409**；非法 id → 400。
- 审计 `action="execution.cancel"`，`target="execution:<eid>"` 或 `"batch:<batch_id>"`。

### 8.3 任务 / 项目软归档

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks/{task_id}/archive` | 写 `archived_at`，不删除配置 |
| POST | `/api/tasks/{task_id}/unarchive` | 移除 `archived_at` |
| POST | `/api/projects/{pid}/archive` | 写 `archived_at` |
| POST | `/api/projects/{pid}/unarchive` | 移除 `archived_at` |

- `GET /api/tasks`、`GET /api/projects` 默认排除已归档；`?include_archived=1` 可看全部。
- 归档后详情仍可读；历史运行、报告、批次产物不受影响。
- 列表里的 `archived_at` 只在归档对象上出现，未归档对象的既有字段集合不变。
- 审计 `task.archive/task.unarchive/project.archive/project.unarchive`。

### 8.4 报告列表 / 签署 / 作废

`GET /api/reports?project=&status=&offset=&limit=`

- 每条：`run_id/project_id/suite_id/suite_name/trigger/status/conclusion/summary/passed/total/duration_ms/workflow_ref/has_insight/insight_source/sign_state/signed/voided/verdict/note/signed_by/signed_at/void_reason/voided_by/voided_at/started_at/finished_at`。
- `has_insight` = 是否存在有效的 AI 总结缓存（不触发生成）。
- `status` 过滤：`all|unsigned|signed|voided`，也兼容运行状态 `passed|failed|errored|done`。
- 返回 `{reports,total,offset,limit}`。

`POST /api/reports/{run_id}/sign` `{verdict,note?,force?}`

- `verdict` 必填；`note` 可选。首次签署成功。
- 已签署再签 → **409**；带 `force:true` 可覆盖，历史签名保留在 `history` 且审计含 `from→to`。
- 已作废 → **409**，不可再签。

`POST /api/reports/{run_id}/void` `{reason}`

- `reason` 必填；作废后 `voided=true`，**不可撤销**，重复作废 → 409。
- 报告本体 `runs/<id>/report.json` 永不修改；人工结论落
  `<ws>/.panguverse/web/reports/<run_id>.json`（`0600`）。
- 审计 `report.sign/report.void`，`target="report:<run_id>"`。

### 8.5 零回归

- `/api/runs` 仍返回数组；`/api/runs/query` 分页不变；认证、studio、insight、错误体不变。
- 新增字段只在归档/签署/工作流等对象上出现；未归档任务/项目的既有列表字段集合不变。
- 新错误码沿用统一错误体：`{"error":{"code","message"}}`；取消已结束执行与重复签署返回 409。

## 9. 调度与触发（v8.0，契约 `tasks/schedule-artifacts-spec.md` §1）

### 9.1 数据模型与 cron

落盘 `<workspace>/.panguverse/schedules/<sid>.json`（目录 `0700`、文件 `0600`、原子写）。
`sid` 服务端生成（`^sc-[0-9a-f]{6,12}$`），**客户端传入的 sid 一律忽略**。

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

- `trigger.kind` ∈ `cron|interval|webhook`；`target.kind` 目前只支持 `project`，
  `suite_ids=[]` 表示该项目全部套件。
- `cron` 为 **5 字段**（分 时 日 月 周），自己解析、不依赖第三方库：
  支持 `*`、`*/n`、`a-b`、`a,b`，额外兼容 `a-b/n`；周字段 `0`/`7` 都表示周日。
  时区默认 `Asia/Shanghai`；`next_run_at` 为 epoch 秒。
- `interval` 用 `{"kind":"interval","minutes":30}`，最小 1 分钟。
- `webhook` 用 `{"kind":"webhook"}` 创建；服务端生成 32 字节随机 token，
  **只在创建/重置的响应里返回一次**（`webhook_token`）。磁盘只保存 SHA-256 与末 4 位提示，
  详情接口不再回显原文。PATCH `{"trigger":{"kind":"webhook","reset_token":true}}` 可重置。
- **禁止重叠**：同一 sid 的上一次 batch 仍 `queued/running` 时，本次触发只记录
  `skipped_overlap` 事件并返回 `{"status":"skipped_overlap"}`，不排队、不产生新 batch。
- **重启恢复**：`serve` 启动时按磁盘重算 `next_run_at`；已错过的时点不补跑，
  只向 `<workspace>/.panguverse/schedules/events.jsonl` 追加 `missed` 事件。
- 调度线程随 `serve` 启停（单进程 MVP，不加队列）；事件另有
  `created/updated/deleted/triggered/trigger_failed/skipped_overlap/missed`。

### 9.2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/schedules?include_disabled=1` | 默认只列 `enabled=true` 且未归档 |
| POST | `/api/schedules` | 201；返回含 `next_run_at`，webhook 另含一次性 `webhook_token` |
| GET | `/api/schedules/{sid}` | 详情（不含 token 原文） |
| PATCH | `/api/schedules/{sid}` | 改 `name/target/trigger/env_override/enabled` |
| DELETE | `/api/schedules/{sid}` | 204；不删历史运行 |
| GET | `/api/schedules/{sid}/events?limit=100` | 调度事件（调试用） |
| POST | `/api/schedules/{sid}/run-now` | 202；`{schedule_id,status,batch_id,job_id,trigger}` |
| POST | `/api/hooks/{token}` | webhook 触发；body 可带 `env` 覆盖 |

- `run-now` 是不受 `enabled` 限制的显式人工触发，执行记录写
  `meta.trigger="manual"` 与 `meta.schedule_id`；cron/interval 触发写 `schedule`，
  webhook 写 `webhook`。响应里的 `job_id` 为该批次第一个 job。
- webhook token 不存在 → **404**；调度 `enabled=false` → **409**。
- `/api/hooks/*` 与 `/api/share/*` 不要求登录（其余 `/api/*` 仍受认证保护）。

### 9.3 Workflow 级触发器（M1）

已发布 workflow（`workflow_ref = w-xxxx@N`）可以直接被手动、cron/interval 或
webhook 触发，**不经过 project/suite/task**。项目级调度（`.panguverse/schedules/`）
与 workflow 触发器（`.panguverse/triggers/`）两套存储并存，由同一个调度线程扫描。

落盘：`<workspace>/.panguverse/triggers/<trigger_id>.json`（目录 `0700`、文件 `0600`、
原子写），事件追加到 `<workspace>/.panguverse/triggers/events.jsonl`。

```json
{
  "trigger_id": "tr-1a2b3c4d",
  "workflow_ref": "w-8f3a1c@3",
  "wid": "w-8f3a1c",
  "rev": 3,
  "kind": "schedule",
  "enabled": true,
  "schedule": {"cron": "0 9 * * *", "tz": "Asia/Shanghai"},
  "env_override": {},
  "last_run_at": null, "last_status": null, "last_job_id": null,
  "next_run_at": 0, "created_at": 0, "updated_at": 0
}
```

- `trigger_id` 由服务端生成（`^tr-[0-9a-f]{8}$`），客户端传值一律忽略。
- `workflow_ref` 必须通过 `studio.load_published` 校验，且必须属于 URL 里的 `wid`。
- `kind` ∈ `schedule|webhook|manual`。`schedule` 必须且只能给
  `schedule({cron,tz})` 或 `interval({minutes})` 之一；cron 复用 §9.1 的 5 字段解析，
  不引入第三方库。
- `webhook` 创建时生成随机 token，**只在创建/重置的响应里返回一次**；
  磁盘只保存 `sha256` 与末 4 位 `token_hint`，列表/详情只回显 `token_hint`。
- `enabled=false` 不参与定时/webhook 触发，但仍允许显式 run-now。
- **禁止重叠**：同一 trigger 上次 job 仍 `queued/running` 时，本次触发只写
  `skipped_overlap` 事件并返回 `{"status":"skipped_overlap"}`，不产生新 job。
- **重启恢复**：启动时按磁盘重算 `next_run_at`；已经错过的时点只写 `missed`，
  不补跑。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/studio/{wid}/triggers` | `{triggers:[...]}`；不含 token 原文/哈希 |
| POST | `/api/studio/{wid}/triggers` | 201；body `{workflow_ref,kind,schedule?|interval?,env_override?}`；webhook 另含一次性 `webhook.token` |
| PATCH | `/api/triggers/{tid}` | 200；`{enabled?,schedule?,interval?,env_override?,regenerate_token?}`；重置 token 时返回一次新 `webhook.token` |
| DELETE | `/api/triggers/{tid}` | 204；只删 trigger，保留历史 run/execution |
| POST | `/api/triggers/{tid}/run` | 202；`{trigger_id,job_id,status:"queued",trigger:"manual"}` |
| POST | `/api/hooks/workflow/{token}` | 202；body `{env?,payload?}`，`payload` 原样写入 trigger 事件（按 §3 规则打码） |

- webhook token 无效 → **404**；trigger `enabled=false` → **409**。
- `/api/hooks/workflow/*` 与其他 `/api/hooks/*` 一样不要求登录。
- 归因：job/execution 记录 `mode="workflow"`、
  `trigger ∈ manual|schedule|webhook`、`workflow_ref`、`trigger_id`。
  `execution.json` 顶层含这些字段，报告 `meta` 与 `meta.run_config` 同样携带；
  `/api/runs/{id}` 可据此回溯到具体触发器。

## 10. 任务产出物（v8.0，契约 §2）

### 10.1 概念与落盘

产出物 = 一次运行中值得交付的结果，不是日志。来源：

1. **原子声明** `Atom.produces`：`llm_summarize → summary_md`、
   `web_screenshot → screenshot`、`android_ui_dump → ui_tree`、
   `web_extract_links → links`；`/api/atoms` 的 `produces` 字段可读；
2. **报告级** AI 总结：生成 insight 时自动落为 `ai_summary`（同 run 覆盖更新）。

```
<ws>/.panguverse/artifacts/<project_id|unclassified>/<run_id>/<name>.<ext>
<ws>/.panguverse/artifacts/<project_id|unclassified>/<run_id>/manifest.json
```

- 目录 `0700`、文件 `0600`；manifest 记录 `artifact_id/run_id/project_id/name/type/bytes/sha256/node_id/step_id/case_id/created_at`。
- 类型以**真实字节**为准：PNG/JPEG/GIF/WebP 魔数 → `image`；其余按
  `produces` 提示与内容嗅探得到 `markdown|image|json|xml|text|file`。
- 当输出值是工作区内文件路径（如 `web_screenshot` 的
  `artifacts/web/a.png`，相对 `.panguverse` 或 artifacts 根均可）时，收集器
  **读取文件字节**落盘，`bytes/sha256` 基于真实内容；路径不存在或不可读 →
  不产生产物，只记 `artifact_source_missing` 告警。
- `android_ui_dump.ui_tree` 落完整 XML（读取原子的 `xml_full`，而不是
  `xml` 的限长回显）；若只有 `nodes`，用节点重建最小 `<hierarchy>` XML。
- 同名产物按 `<name>-<seq>` 去重；单产物上限 **20MB**，超出只记元信息并标
  `truncated=true`（下载返回 409）。
- 收集是**旁路**：失败只写 job 阶段告警（`artifacts: warning`），运行结论不变（A7）。

### 10.2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/artifacts?project=&run=&type=&offset=&limit=` | `{total,items[]}`，按创建时间倒序 |
| GET | `/api/artifacts/{aid}` | 元信息 + 文本类 ≤64KB 的 `content`；含 `download_url` |
| GET | `/api/artifacts/{aid}/download` | 文件流 + `Content-Disposition` |
| POST | `/api/artifacts/{aid}/share` | `{url_token,expires_at}`；`expires_in` 支持 `1h/1d/7d/30d` 或秒数，默认 7 天 |
| DELETE | `/api/artifacts/{aid}/share/{token}` | 204，撤销分享 |
| GET | `/api/share/{token}` | 只读 HTML 预览页（**未登录可访问**） |
| GET | `/api/share/{token}/raw` | 分享文件的原始字节（图片预览用） |

- 分享 token 只读、不可列目录；不存在/已撤销 → **404**，过期 → **410**。
- `aid` 必须匹配 `^ar-[0-9a-f]{6,12}$`；文件名由服务端清洗，路径穿越、
  跨项目 `project` 过滤不匹配（含非法 project/run 过滤值）、直接读取
  `manifest.json` 均返回 **404 `not_found`**，不返回 400。
