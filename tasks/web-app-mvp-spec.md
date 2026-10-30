# PanguVerse Web 版 MVP 规格（开发契约）

> 依据：`docs/web-app-user-stories.md`（P0 故事 US-01…US-11）
​> 修订：v1.4（2026-10-31）A16 扩展到 report.html/junit.xml（QA 发现 HTML 旁路）
> 修订：v1.3（2026-10-31）demo 基线可复现（mock 标记）、全 errored 判 failed、API 层 env 打码（A16）
> 修订：v1.2（2026-10-31）intent 空 env 不回落基线，与 A8 的确定性要求对齐
> 修订：v1.1（2026-10-30）按首轮 QA 结论补充 env 缺失处理与 workspace 边界（A8/A9/A15）
> 目标：`panguverse serve` 起一个本机 Web 应用，覆盖「概览 → 运行 → 报告 → 原子/包浏览 → 回归」闭环。
> 硬约束：**零新增运行时依赖**（`pyproject.toml` 的 `dependencies` 保持 `[]`），无前端构建步骤（不引入 npm）。

---

## 1. 交付物与文件归属（禁止越界改文件）

|coder（后端/服务）|ui（前端静态资源）|
| --- | --- |
| `panguverse/web/__init__.py` | `panguverse/web/static/index.html` |
| `panguverse/web/server.py` | `panguverse/web/static/app.js` |
| `panguverse/web/api.py` | `panguverse/web/static/style.css` |
| `panguverse/web/jobs.py` | |
| `panguverse/web/service.py` | |
| `panguverse/http/client.py`（代理直连修复） | |
| `panguverse/cli.py`（新增 `serve` 子命令） | |
| `tests/test_web_api.py` | |
| `docs/web-api.md` | |

- 都**不得**修改 `panguverse/core|engine|axe|packs|spec|report` 的行为语义；如确需重构，必须保证现有 184 个测试全绿。
- 都**不得**改 `pyproject.toml` 的 `dependencies`。
- 都**不得**执行 `git push`；只提交自己归属的文件。

---

## 2. 启动方式

```bash
panguverse serve [--host 127.0.0.1] [--port 7788] [--workspace .] [--no-open]
```

- 默认 `host=127.0.0.1`，`port=7788`，`workspace=.`。
- 监听 `127.0.0.1` 时自动打印 `http://127.0.0.1:<port>/`；`--no-open` 不尝试拉浏览器（CI/测试用）。
- `--host 0.0.0.0` 允许但打印警告：**无认证，仅限可信网络**。
- 服务器：`http.server.ThreadingHTTPServer` + 自定义 `BaseHTTPRequestHandler`；`allow_reuse_address`。

---

## 3. HTTP API 契约

统一前缀 `/api`。请求/响应均为 `application/json; charset=utf-8`。

错误体：

```json
{"error": {"code": "not_found", "message": "run 不存在"}}
```

| code | HTTP | 场景 |
| --- | --- | --- |
| `bad_request` | 400 | 参数缺失/非法（如 mode 未知、intent 为空） |
| `not_found` | 404 | run/job/pack 不存在，静态资源不存在 |
| `conflict` | 409 | 同名 job 正在运行（可选，见 §5） |
| `internal_error` | 500 | 未捕获异常（消息需打码，见 §7） |

### 3.1 `GET /api/status`（US-01/US-02）

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

数据来源：`PanguAxe.status()` + `len(registry)` + 工作区扫描；`llm_enabled` 依据 `PANGUVERSE_API_KEY`/`OPENAI_API_KEY` 是否存在（**不得回显密钥值**）。

### 3.2 `GET /api/atoms?domain=&tag=&category=`（US-09）

返回数组，元素字段与 `panguverse atoms --json` 完全一致（`name/description/category/domain/inputs/outputs/input_ports/output_ports/required/tags/aliases/version/effects/...`）。过滤为服务端过滤；无匹配返回 `[]`（不是 404）。

### 3.3 `GET /api/packs`、`GET /api/packs/{name}`（US-09）

- 列表：`[{name, version, domain, builtin, description, atom_count, capabilities, semantics}]`
- 详情：上述字段 + `atoms: [<atom_name>...]` + `manifest`（`packs manifest` 中该包的原始 dict）
- 未知包 → 404 `not_found`

### 3.4 `GET /api/scenarios`（US-04）

```json
[{"id": "scenarios/api_smoke.yaml", "name": "api_smoke", "source": "builtin", "cases": 3}]
```

- `source`: `workspace`（`<workspace>/scenarios/**`）优先，其余为 `builtin`（仓库内置 `scenarios/`）。
- `id` 为可直接回传给 `POST /api/runs` 的标识；工作区文件用工作区相对路径。
- 解析失败的文件仍要返回，带 `"error": "<原因>"`，`cases` 置 0 —— 前端置灰而不是崩。

### 3.5 `POST /api/runs`（US-03/US-04/US-05/US-10）

请求：

```json
{
  "mode": "demo | scenario | intent | regression",
  "scenario": "scenarios/api_smoke.yaml",
  "intent": "检查健康状态并验证登录下单",
  "env": {"BASE_URL": "http://127.0.0.1:8899"},
  "regression": true,
  "no_llm": false
}
```

校验规则：

- `mode=demo`：忽略 `scenario`/`intent`；等同 `demo e2e`（含回归复用，除非 `regression=false`）。
- `mode=scenario`：`scenario` 必填，否则 400；路径必须在 `workspace` 或内置 `scenarios/` 内（`realpath` 前缀校验），越界 400。
- `mode=intent`：`intent` 非空字符串（trim 后长度 ≥ 1），否则 400。
- `mode=regression`：忽略其余字段，走已沉淀回归库。
- `env` 必须为 `string → (string|number|bool)` 的对象；非对象 400。
- **env 解析与缺失处理（QA 补充条款，v1.1）**：
  1. `mode=regression`：以「最近一次成功运行的 env」为基线（持久化在 `<workspace>/.panguverse/web/last_env.json`），请求里的 `env` 覆盖同名键。
  2. `mode=demo|scenario`：请求 `env` 优先；缺失键回落最近一次成功运行的 env。
  2b. `mode=intent`：**请求 env 为空时不回落基线**——意图模式必须显式给出目标，避免对着过期的旧目标静默运行；缺失变量直接 `failed`，并在 `error` 里提示「可在表单填写，或先跑一次 demo/scenario 生成基线」。请求给了部分 env 时，缺的键仍回落基线。（v1.2 拍板项）
  3. Web 层始终注入 `FS_ROOT=<workspace>/.panguverse/fs-demo`（除非请求显式给出）。**任何运行产生的文件不得写到 workspace 之外**。
  3b. `mode=demo` 的基线必须可复现：demo 用进程内 mock 时，基线里不能只写场景声明的 `BASE_URL`，必须写入 `target: {"kind": "mock", ...}` 标记；`mode=regression` 遇到该标记时**自动拉起同款进程内 mock** 并改写 `BASE_URL`，不得回放到已失效的端口。（v1.3 拍板，QA F1）
  4. 规划完成后若计划里仍存在无法解析的 `${env.X}`，job 置 `failed`，`error` 明确写出 `缺少环境变量: X`（可多个），前端提示去运行表单填写。**禁止**产出「done 但全部用例 errored」的假成功。

响应：`202 Accepted`

```json
{"job_id": "3f9c1a2b7d4e", "status": "queued"}
```

### 3.6 `GET /api/jobs`、`GET /api/jobs/{job_id}`

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

- `GET /api/jobs` 返回最近 20 条（按 `created_at` 倒序），**内存态**，服务重启即清空。
- `phases` 在任务执行中**逐步追加**（plan/validate/execute/heal/report/evolve/regression），供前端显示进度。
- 任务失败（异常/非零退出）时 `status=failed` 且 `error` 为打码后的一行消息；**不能**只返回 200 而隐藏失败。
- 未知 `job_id` → 404。

### 3.7 运行历史与报告

- `GET /api/runs?limit=50` → `[{run_id, dir, started_at, finished_at, duration_ms, summary, conclusion, meta}]`，扫描 `<workspace>/runs/`（若不存在则退回 `<workspace>/.panguverse/runs/`），按 `started_at` 倒序，忽略无 `report.json` 的目录。
- `GET /api/runs/{run_id}` → `{"report": <report.json 原文>, "plan": <plan.json 或 null>}`；`run_id` 必须匹配 `^[0-9a-f]{6,32}$`，否则 400。
- `GET /api/runs/{run_id}/report.html` → `text/html; charset=utf-8`，直接回磁盘文件。
- `GET /api/runs/{run_id}/junit.xml` → `application/xml` + `Content-Disposition: attachment; filename="panguverse-<run_id>.xml"`。
- 文件缺失 → 404 `not_found`。

### 3.8 静态资源

- `GET /` → `panguverse/web/static/index.html`
- `GET /static/<file>` → 仅允许 `index.html`、`app.js`、`style.css`、`favicon.svg` 白名单；其余 404。
- 未知路径返回 `index.html`（SPA 前端路由），但 `/api/*` 未知路径必须返回 JSON 404。

---

## 4. 代理直连修复（US-11，P0 缺陷）

`panguverse/http/client.py`：

- 目标 host 属于 `127.0.0.1`、`localhost`、`::1`、`0.0.0.0`（含 `*.localhost`）时**强制直连**：
  - `urllib` 路径：`build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))`
  - `requests` 路径：`session.trust_env = False` 或显式 `proxies={}`（二者取其一并注释原因）
- 非回环地址行为不变（仍尊重系统/环境代理）。

必测：设置 `HTTP_PROXY=http://127.0.0.1:1`（必死端口）+ `NO_PROXY` 清空，请求本机 `ThreadingHTTPServer` 必须 200。

---

## 5. 任务执行模型

- `jobs.py` 维护 `dict[job_id] -> Job`，`threading.Lock` 保护。
- 每个 job 一个 `threading.Thread(daemon=True)`；MVP **不排队**，允许并发（前端限制一次只发一个）。
- 单 job 超时 300s（可 `PANGUVERSE_WEB_JOB_TIMEOUT` 覆盖）；超时置 `failed` + `error="timeout"`。
- 执行复用 `PanguAxe`：`plan → validate → run/loop → report`；需要复用的 `cli.py` 内部助手可抽到 `panguverse/web/service.py`，`cli.py` 改为从该处导入，**CLI 行为与输出不得变化**。
- 产物落到 `<workspace>/runs/<run_id>/`（与 CLI 一致）。

---

## 6. 验收标准（QA 照着跑）

| # | 项 | 判定 |
| --- | --- | --- |
| A1 | `python -m pytest -q`（**不设** `NO_PROXY`） | 全绿，含新增 `tests/test_web_api.py` |
| A2 | `ruff check .` | 0 error |
| A3 | `panguverse serve --port 7788 --workspace /tmp/pv-web-qa --no-open` | 启动无异常，`curl -s -o /dev/null -w '%{http_code}' localhost:7788/` = 200 |
| A4 | `GET /api/status` | 200；`atoms>=24`、`packs>=3`、`llm_enabled` 为布尔 |
| A5 | `POST /api/runs {"mode":"demo"}` → 轮询 `GET /api/jobs/{id}` | 终止态 `done`；`summary.passed == summary.total` 且 `total >= 7` |
| A6 | `GET /api/runs` | 含刚才 run；`GET /api/runs/{id}/report.html` 200 且 `<html` 存在；`junit.xml` 带 attachment 头 |
| A7 | `POST /api/runs {"mode":"scenario","scenario":"scenarios/api_smoke.yaml"}` | `done` 且 `plan.source`（从 run 详情读）为 spec/该场景 |
| A8 | `POST /api/runs {"mode":"intent","intent":"检查健康状态"}`（无 LLM 密钥） | 提供 `env.BASE_URL` 时 `done` 且 `summary.passed==summary.total`、`meta.llm_used==false`；**不提供**时 job 为 `failed` 且 `error` 含 `缺少环境变量: BASE_URL`（不得出现 done+errored 的假成功） |
| A9 | `POST /api/runs {"mode":"regression"}`（**不传 env**，前置：已成功跑过 demo；**不得**依赖外部 8899 服务） | `done` 且 `summary.passed==summary.total>=7`；`plan.source` 为回归库来源 |
| A16 | 凭据不外泄（**所有** HTTP 出口） | `GET /api/runs/{id}`、`/api/jobs/{id}`、`/api/runs/{id}/report.html`、`/api/runs/{id}/junit.xml` 的响应正文中均不出现 `demo123` 等 env 明文值；JSON 中键名仍在、值为 `***` |
| A10 | 非法输入：`{"mode":"x"}`、`{"mode":"scenario"}`、`{"mode":"intent","intent":"  "}`、`GET /api/runs/../../etc` | 分别 400/400/400/400 或 404，均为 JSON 错误体 |
| A11 | 目录穿越：`GET /static/../../cli.py` | 404，绝不返回源码 |
| A12 | 代理隔离：`HTTP_PROXY=http://127.0.0.1:1` 下跑 demo | 仍 `passed == total`（证明 US-11 修复生效） |
| A13 | 前端 | 概览/运行/历史/详情/原子/包 六个视图可达；失败步骤显示 `message`+`expected`+`actual`；无空白错误块；控制台无未捕获异常 |
| A15 | workspace 边界 | 跑完 demo + regression + intent 后，`git status --short` 仍为空；`find <workspace> -name snapshots -o -name archive` 全部位于 workspace 内 |
| A14 | 零依赖 | `grep -A2 '^dependencies' pyproject.toml` 仍为空；`node_modules` 不存在 |

---

## 7. 安全与打码

- 报告中所有输出走 `core/redact.py` 打码；API 错误消息不得包含密钥、token、Cookie 原文。
- **新增（v1.3/v1.4，QA F2/F4）**：`GET /api/runs/{id}`、`/api/jobs/{id}`、`report.html`、`junit.xml` 等**任何** HTTP 响应里，`env` / `plan.env` 中键名命中 `(?i)(password|passwd|secret|token|api[_-]?key|credential)` 的值必须打码为 `***`（保留键名，便于排障）；磁盘上的 `report.json`/`plan.json` 保持原样（本地文件、回归回放依赖），但不得经 HTTP 原样回吐。
- **新增（v1.3）**：`summary.passed == 0` 且全部用例 `errored` 时，job 必须为 `failed`（基础设施不可达不是「测试没通过」），`error` 给出首个错误原因。
- `GET /api/runs/{id}` 返回的 `report.json` 已是打码后的结构，**不要**重新从原始运行对象序列化。
- 默认只绑 `127.0.0.1`；不实现认证（MVP），文档须写明不可暴露公网。

---

## 8. 前端要求（ui）

- 单页，无构建、无 CDN 依赖（离线可用）；原生 ES modules 或单文件 `<script>` 均可。
- 六个视图：`#/overview`、`#/run`、`#/history`、`#/runs/<id>`、`#/atoms`、`#/packs`。
- 概览：状态卡片 + 「运行内置演示」主按钮 + 回归库「跑一次」。
- 运行：模式切换（演示/场景/意图/回归）、场景下拉、意图输入、env 键值行、`no_llm` 开关；提交后显示阶段进度与已运行秒数。
- 详情：用例列表（状态色）→ 展开步骤表（atom/状态/耗时/attempts）→ 失败步骤的 `message/expected/actual` 与自愈事件；顶部按钮「下载 JUnit」「打开 HTML 报告」。
- 历史：表格（run_id/时间/通过率/耗时/结论），点击进详情；空态文案明确。
- 原子：domain/tag 过滤 + 表格 + 抽屉显示端口契约。
- 包：卡片列表 + 原子清单。
- 无障碍：主按钮可用键盘触发；错误用文本而非仅颜色表达。
- 交互失败（接口 4xx/5xx）要在页面上给出可读原因，禁止静默。

---

## 9. QA 计划

1. coder + ui 各自本地自测（分端口：后端 7788，UI 静态由同一后端提供，避免端口冲突）。
2. 双方提交后，qa 在干净工作区 `/tmp/pv-web-qa` 按 §6 逐条执行，附命令与关键输出。
3. 失败项原样退回原作者，修完只复验相关条目。
4. 通过后报告：PASS/FAIL、证据、遗留项。

## 10. 回滚

- 全部为新增文件 + `cli.py` 新增子命令 + `http/client.py` 局部修改；回滚 = `git revert <commit>`。
- `serve` 失败不影响既有 CLI 任何子命令（验收 A1 保证）。
