# 用 PanguVerse 给两个项目做自动化 API 测试 · 契约 v1.0

> 目标：用 PanguVerse 自己的场景机制，覆盖 **PanguVerse Web** 与 **coding-log-web** 的对外 API，
> 产出可重复执行的场景文件 + 报告 + 明确的通过/失败结论。这是 dogfooding：平台先测自己。

## 1. 被测对象

| 项目 | 目标地址 | 说明 |
| --- | --- | --- |
| PanguVerse Web | `http://127.0.0.1:7788` | 本机调试端点，**已开启登录认证** |
| coding-log-web | `https://seres-ai.com` | 公网站点 + 旅行助手后端 `/api/travel/*` |

## 2. 场景文件（放 `scenarios/`，两个文件，不要改内置四个）

### `scenarios/panguverse_web.yaml` — 平台自测

| 用例 | 步骤 | 断言 |
| --- | --- | --- |
| `unauth_guard` | `GET /api/status`（不带 cookie） | `401`；body `error.code == unauthorized` |
| `login_flow` | `POST /api/auth/login`（环境变量凭据） | `200`；`ok == true`；随后 `GET /api/status` → `200` 且 `atoms >= 24` |
| `catalog` | `GET /api/atoms`、`GET /api/packs` | `200`；原子数组长度 `>= 24`；包数组长度 `>= 3` |
| `task_lifecycle` | `POST /api/tasks` → `GET /api/tasks/{id}` → `PATCH`（改 name + env 三态）→ `DELETE` → `GET /api/tasks/{id}` | 201/200/200/204/404；详情 env 中密钥值为 `***`；`PATCH` 后 name 生效 |
| `runs_api` | `GET /api/runs?limit=5` | `200`；返回数组（可为空） |
| `logout_guard` | `POST /api/auth/logout` → `GET /api/status` | 200；随后 `401` |

- **凭据不得写进仓库**：`env` 里只用占位 `API_USERNAME` / `API_PASSWORD`，执行时用 `--env` 传入。
- **限流红线**：错误密码最多试 **1 次**（阈值 5 次会锁 IP 5 分钟），且要在正确登录之前做，避免把后续用例锁死。
- `task_lifecycle` 必须删除自己创建的任务（幂等、可重复跑）。

### `scenarios/codinglog_web.yaml` — 站点 + 旅行助手

| 用例 | 步骤 | 断言 |
| --- | --- | --- |
| `site_pages` | `GET /`、`GET /projects/panguverse/` | `200`；HTML 含 `三木`；项目页含 `PanguVerse` |
| `travel_health` | `GET /api/travel/health` | `200`；JSON 结构合法 |
| `travel_root` | `GET /api/travel/` | `200` |
| `travel_weather` | `GET /api/travel/weather?...` | `200`；字段存在（按 `backend/travel_assistant/app.py` 的真实签名构造参数） |
| `travel_share_negative` | `GET /api/travel/share/does-not-exist-<随机>` | `404`（负向） |

- 只读接口为主。**不要**打 `POST /api/travel/plan`（重计算）与 `POST /api/travel/feedback`（写数据）。
- 断言要真校验内容（状态码 + 关键字段），禁止只断 `200`。

## 3. 执行与产物

```bash
python -m panguverse run scenarios/panguverse_web.yaml \
  --env BASE_URL=http://127.0.0.1:7788 --env API_USERNAME=admin --env API_PASSWORD=<运行时传入> \
  --workspace .panguverse/api-tests
```

- 产物落 `.panguverse/api-tests/runs/<run_id>/`（report.json/html/junit）。
- 两个场景都要能在**连续两次执行**下稳定通过（无残留任务、无随机失败）。

## 4. 验收（A34–A39）

| # | 项 | 判定 |
| --- | --- | --- |
| A34 | 两个场景文件存在且能被执行 | `run` 退出码 0；报告 `summary.passed == summary.total` |
| A35 | 断言非空壳 | 每条用例至少 1 个内容断言（状态码之外的字段/结构）；负向用例真的拿到 401/404 |
| A36 | 幂等 | 连跑两次结果一致；跑完 `GET /api/tasks` 不含本次创建的任务 |
| A37 | 不泄密 | 场景文件、报告、日志中 `grep` 不到真实密码 |
| A38 | 不越界 | 不调用 `POST /api/travel/plan`、`feedback`；不修改仓库内 `.panguverse/` 以外任何文件 |
| A39 | 报告可读 | `report.html` 能打开，失败步骤带 message |

## 5. 文件归属

|coder|说明|
| --- | --- |
| `scenarios/panguverse_web.yaml`、`scenarios/codinglog_web.yaml` | 只新增，不改内置场景 |
| `docs/api-tests.md` | 如何跑、传什么 env、已知限制 |
| `.gitignore` 追加 `.panguverse/api-tests/`（如需） | 产物不入库 |

## 6. 明确不做

性能/压力测试、并发、UI 端到端、Artalk 评论写入、支付/预订类接口。
