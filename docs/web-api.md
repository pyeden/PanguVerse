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
- **校验**：HMAC、`exp`、`u` 与当前账号一致、登出黑名单（内存态 nonce，重启即清空）；
  任一项不过按未认证处理，绝不 500。
- **登出**：`POST /api/auth/logout` → `200 {"ok":true}`，`Set-Cookie: pv_session=; Max-Age=0 ...`，
  旧 cookie 立即失效（nonce 进黑名单直到自然过期）。
- **限流**：同 IP 5 分钟内失败 5 次 → 后续请求 `429` + `Retry-After`（秒）；
  成功登录清零。登录失败**不区分**账号不存在/密码错，日志只记
  `[auth] login failed user=<name> ip=<ip>`，绝不出现密码。
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
- 执行顺序与 `PanguAxe.loop` 一致：`plan → validate → executor.run（含重试/自愈）→ report（json/html/junit + plan.json）→ evolve → regression`。
- 报告一律经 `panguverse.report.write_all` 生成（内部按密钥打码）；`/api/runs/{id}` 直接回磁盘 `report.json`，不重新序列化。

## 6. 已知限制

- job 表在内存：重启后历史只剩磁盘上的 `runs/`（前端应回退到「运行历史」）。
- 无认证、无多用户、无 SSE：仅本机单人使用；实时进度靠轮询。
- 长任务只做「超时标记」，不强行中断执行线程（线程为 daemon，进程退出即回收）。
- 回归库/经验库/`last_env.json` 是工作区文件，多个 job 并发写入时以「最后写入」为准（单用户场景可接受）。
- env 基线只在「成功运行」后更新；一次失败的运行不会污染下一次的默认目标。
- `panguverse/http/client.py` 之外的出站调用（如 `axe/llm.py` 的 LLM 请求）不在此次修复范围内。
