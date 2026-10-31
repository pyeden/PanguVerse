# /panguverse/ 登录认证 · 开发契约 v2.1

> 修订：v2.1.1（2026-10-31）会话密钥启动轮换（F2）、受信代理链路取真实客户端 IP（F1）、日志口径澄清
> 背景：端点已公网暴露（`https://seres-ai.com/panguverse/`），`serve` 无认证，知道路径就能跑任务、读报告。
> 目标：**默认开启登录**，不依赖 nginx basic auth（省一次 root 重载），直接由应用层做。

## 1. 认证模型（一次性定死，别发挥）

- **默认开启**：`serve` 启动即要求登录；仅显式 `--no-auth` 才关闭（本地调试用，会打印警告）。
- **凭据来源（优先级从高到低）**：
  1. 环境变量 `PANGUVERSE_WEB_USER` + `PANGUVERSE_WEB_PASSWORD`；
  2. `<workspace>/.panguverse/web/auth.json`（`0600`）：`{algo, iterations, salt, hash, user, updated_at}`；
  3. 都没有 → **首次启动生成随机密码**（16 字符，去易混字符），把 PBKDF2 哈希写入 `auth.json`，并把明文密码打印到启动日志一行：`[auth] 初始账号 admin，初始密码 <pwd>（首次启动生成，仅显示这一次）`。
- 算法：`pbkdf2_hmac('sha256', pwd, salt, 200_000)`，salt 32 字节随机；比较用 `hmac.compare_digest`。
- **会话密钥轮换**：每次进程启动生成新的 `session.key`（不复用旧的），即**重启后所有既有会话失效**，彻底避免「登出黑名单只在内存、重启后旧 Cookie 复活」。（v2.1.1，QA F2）
- 会话：Cookie `pv_session`，值 `base64url(payload).base64url(hmac_sha256(payload, session_key))`；`payload={u,exp,nonce}`；有效期 12h；密钥存 `<workspace>/.panguverse/web/session.key`（32 随机字节，`0600`，缺失则生成）。
- Cookie 属性：`HttpOnly; Path=/; SameSite=Lax`；当 `X-Forwarded-Proto=https` 时追加 `Secure`。

## 2. HTTP 行为

| 路径 | 未认证 | 已认证 |
| --- | --- | --- |
| `GET /`、静态资源 | 200（前端自己渲染登录页） | 200 |
| `GET /api/auth/status` | 200 `{"authenticated": false}` | 200 `{"authenticated": true, "user": "admin"}` |
| `POST /api/auth/login` | 见 §3 | — |
| `POST /api/auth/logout` | 200（幂等） | 200，清 Cookie |
| 其余 `/api/*` | **401** `{"error":{"code":"unauthorized","message":"需要登录"}}` | 正常 |

- 前端不依赖 302：`GET /` 一律 200，由 SPA 先 `GET api/auth/status` 决定渲染登录页还是主界面。
- 401 时前端必须跳到登录视图，不能白屏。

### 登录

`POST /api/auth/login` body：`{"username":"admin","password":"..."}`

- 成功：`200 {"ok":true,"user":"admin"}` + `Set-Cookie: pv_session=...`
- 失败：`401 {"error":{"code":"bad_credentials","message":"账号或密码不正确"}}`（**不区分**用户不存在/密码错，防枚举）
- 限流：同 IP 5 分钟内失败 5 次 → `429 {"error":{"code":"too_many_attempts","message":"尝试过多，请 5 分钟后再试"}}` + `Retry-After`；成功登录清零。
- 日志**不得**出现密码；只记录 `[auth] login failed user=<name> ip=<ip>`。

### 登出

`POST /api/auth/logout` → `200 {"ok":true}`，`Set-Cookie: pv_session=; Max-Age=0`。

## 3. 安全细节（QA 会逐条查）

1. 所有 `/api/*` 保护必须在**路由分派前统一拦截**，不能靠每个 handler 自己记得加。
2. 常量时间比较；密码与哈希绝不进日志、不进错误消息、不进 `/api/*` 响应。
3. `auth.json`、`session.key` 权限 `0600`，目录 `0700`；不得被 git 跟踪（`.panguverse/` 已忽略）。
4. 会话签名用独立密钥，**不得**复用 env 里的任何值。
5. Cookie 校验要检查 HMAC、`exp`、`u` 与当前账号一致；任一项不过按未认证处理（不 500）。
6. `--host 0.0.0.0` 且未开启认证时必须打印醒目警告。
7. **客户端 IP 解析（v2.1.1，QA F1）**：限流桶必须按**真实客户端**计数，否则 nginx 反代后所有人共用一个桶，任意一人连错 5 次即可锁死全体登录（DoS）。
   - 直连（对端不是受信代理）→ 用 TCP 对端 IP；
   - 对端是回环/受信代理 → 取 `X-Forwarded-For` **最右一跳**（nginx `proxy_add_x_forwarded_for` 追加的那一跳），非法/缺失时回退对端 IP；
   - 绝不直接信任客户端可伪造的整串 `X-Forwarded-For`（只取最右，且仅当对端受信）。
8. **日志口径澄清**：首启生成的初始密码**允许打印一次**（这是唯一的凭据交付通道，不是日志泄漏）；除此之外——用户提交的密码、请求体、响应体——任何情况下不得出现在日志里。

## 4. 前端（ui）

- 启动流程：`GET api/auth/status` → 未登录渲染**登录视图**（居中卡片：账号、密码、登录按钮、错误提示），已登录渲染现有六视图。
- 登录视图不显示侧栏导航；登录成功后进入 `#/overview`。
- 顶栏加「退出登录」（`POST api/auth/logout` → 回到登录视图）。
- 任何接口返回 401（会话过期）→ 自动回登录视图并提示「登录已过期，请重新登录」，丢弃内存中的旧数据。
- 密码框 `type=password`、支持回车提交、失败时清空密码、按钮提交中禁用。
- 不得把密码写进 `localStorage`/URL/console。

## 5. 验收（A27–A33）

| # | 项 | 判定 |
| --- | --- | --- |
| A27 | 未登录访问 `/api/tasks`、`/api/runs`、`/api/status` | 均 401 + `unauthorized`；`GET /` 仍 200 |
| A28 | 错误密码 | 401 `bad_credentials`；响应与日志均无密码明文 |
| A29 | 正确密码 | 200 + `Set-Cookie: pv_session=...`，带 `HttpOnly`/`SameSite=Lax`；之后 `/api/status` 200 |
| A30 | 会话安全 | 篡改 cookie 签名 → 401；过期（改 exp）→ 401；logout 后再用旧 cookie → 401 |
| A31 | 限流 | 连续 5 次错误 → 第 6 次 429 + `Retry-After` |
| A32 | 前端 | 未登录只见登录卡片；登录后进 `#/overview`；退出回登录；401 自动回登录；控制台零异常；子路径 `/panguverse/` 下同样成立（相对路径） |
| A33 | 回归 | `pytest -q` 全绿、`ruff check .` 0；A17–A26（任务 CRUD）与 A15/A16 不回归；`--no-auth` 时行为与 v1.4 一致 |

## 6. 文件归属

|coder|ui|
| --- | --- |
| `panguverse/web/auth.py`（新） | `panguverse/web/static/app.js` |
| `panguverse/web/server.py`、`api.py`、`cli.py`（`--no-auth`/`--auth-user`） | `panguverse/web/static/index.html` |
| `tests/test_web_auth.py`（新） | `panguverse/web/static/style.css` |
| `docs/web-api.md` | |

## 7. 明确不做

多用户/角色权限、找回密码、OAuth/SSO、TOTP、审计日志落盘、会话持久化到磁盘（重启即失效可接受）。
