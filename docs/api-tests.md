# 用 PanguVerse 测两个项目的 API（dogfooding）

契约：[`tasks/api-test-suite-spec.md`](../tasks/api-test-suite-spec.md) §2–§4。
两个场景文件：

| 场景 | 被测对象 | 用例 | 展开后步骤 |
| --- | --- | --- | --- |
| `scenarios/panguverse_web.yaml` | `http://127.0.0.1:7788`（已开启登录认证） | 6 | 82 |
| `scenarios/codinglog_web.yaml` | `https://seres-ai.com`（站点 + 旅行助手） | 5 | 29 |

> 步骤数是 `assert:` 语法糖展开后的真实执行步数，可用 `panguverse plan -f <场景>` 查看。

## 1. 准备

1. **PanguVerse Web 已启动**（本机调试端点，认证开启）：

   ```bash
   .venv/bin/python -m panguverse serve --host 127.0.0.1 --port 7788 \
     --workspace ~/.panguverse-dev --no-open
   ```

   未登录访问 `GET /api/status` 应得到 `401 {"error":{"code":"unauthorized"...}}`。

2. **凭据文件**：`/tmp/pv-test-creds.env`，权限 `0600`，内容形如

   ```text
   API_USERNAME=admin
   API_PASSWORD=<16 位随机密码>
   ```

   密码由服务端 PBKDF2 校验；仓库、场景、文档、报告里都不出现明文。

3. **Python 环境**：仓库自带 `.venv`（Python 3.12）。系统 `python3` 是 3.9，跑不了本项目。

## 2. 怎么跑

```bash
cd /Users/futuredeng/PycharmProjects/PanguVerse

# 平台自测（6 用例）
.venv/bin/python -m panguverse run scenarios/panguverse_web.yaml \
  --env BASE_URL=http://127.0.0.1:7788 \
  --env FS_ROOT=/tmp \
  --env CREDS_FILE=pv-test-creds.env \
  --workspace .panguverse/api-tests

# 公网站点 + 旅行助手（5 用例，只读）
.venv/bin/python -m panguverse run scenarios/codinglog_web.yaml \
  --workspace .panguverse/api-tests
```

退出码：`0` 全通过 · `1` 有失败 · `2` 执行错误。报告在
`.panguverse/api-tests/runs/<run_id>/`（`report.json` / `report.html` / `junit.xml`）。

### 可传的 env

| 变量 | 默认值（场景内） | 说明 |
| --- | --- | --- |
| `BASE_URL` | `http://127.0.0.1:7788` / `https://seres-ai.com` | 目标地址 |
| `API_USERNAME` | `admin` | 非敏感，可直接放场景 |
| `FS_ROOT` | `/tmp` | 读凭据文件的文件系统沙箱根 |
| `CREDS_FILE` | `pv-test-creds.env` | 相对 `FS_ROOT` 的 0600 凭据文件名 |
| `WEATHER_DATE` | `2026-11-02` | 天气接口 `date` 参数；超出 AMap 预报窗口时后端走 `source=fallback`，字段契约不变 |
| `SHARE_PROBE_ID` | `e2e-does-not-exist-0001` | 负向分享 id（固定值保证可复现，不会命中真实分享） |

> **密码为什么不走 `--env API_PASSWORD`**：`--env` 的值会进入每次运行的
> `case.variables.env` 快照并写进 `report.json`；`API_PASSWORD` 这个键名不在
> 打码键名表里（表里是 `password`/`token`/`secret` 等），明文会落盘。
> 因此场景改用 `fs_read_text` 读取 0600 凭据文件，并把读到的内容与提取出的密码
> 登记为 `secrets`（`creds_raw`、`api_password`），报告层按**值**全量替换为 `***`。
> 这样即使某个断言失败、后续步骤没跑，凭据原文也不会进产物。

## 3. 断言清单

### `scenarios/panguverse_web.yaml`

顺序有意为之：`unauth_guard` 最前（干净 cookie jar），`logout_guard` 最后。

| 用例 | 关键断言 |
| --- | --- |
| `unauth_guard` | `GET /api/status` → `401`；`error.code == unauthorized`；`error.message` 为非空字符串 |
| `login_flow` | 错误密码 **1 次** → `401` + `error.code == bad_credentials`；正确登录 → `200` + `ok == true` + `user == admin`；随后 `GET /api/status` → `200`、`atoms >= 24`、`packs >= 3`、`version` 为字符串 |
| `catalog` | `GET /api/atoms` → `200`，JSON Schema：数组、`minItems 24`、每项含 `name/category/domain`；`GET /api/packs` → `200`，数组、`minItems 3`、每项含 `name/version/domain/atom_count` 且 `atom_count >= 1` |
| `task_lifecycle` | `POST` → `201`，`task_id` 匹配 `^t-[0-9a-f]{6,12}$`，回显 `env.password == "***"`；`GET` → `200` 且 `env.password == "***"`、`env.REGION == a`；`PATCH` → `200`，`name` 生效，`env.REGION` 覆盖为 `b-e2e`、`KEEP_ME` 保留 `b`（传 `"***"`）、`DROP_ME` 删除（传 `null`）、`password` 仍为 `***`；`DELETE` → `204`；再 `GET` → `404` + `error.code == not_found`；`GET /api/tasks` 任务数与创建前**一致**（无残留） |
| `runs_api` | `GET /api/runs?limit=5` → `200`，数组且 `maxItems 5` |
| `logout_guard` | 登录后 `POST /api/auth/logout` → `200` + `ok == true`；同一 cookie 再 `GET /api/status` → `401` + `unauthorized` |

### `scenarios/codinglog_web.yaml`

| 用例 | 关键断言 |
| --- | --- |
| `site_pages` | `GET /` → `200`、`Content-Type` 含 `text/html`、原文含 `vlog`；`GET /` 原文含 `charset="utf-8"`；`GET /projects/panguverse/` → `200`、`Content-Type` 含 `text/html`、原文含 `PanguVerse` |
| `travel_health` | `200`；`status == ok`、`service == travel-assistant`、`version` 为字符串 |
| `travel_root` | `200`；`service == travel-assistant`、`endpoints` 数组包含 `/api/travel/weather` |
| `travel_weather` | `200`；Schema：`text/temp_min/temp_max/rain_prob/uv/source` 齐全，`text`/`uv` 非空，温度整数，`rain_prob∈[0,100]`，`source∈{amap,fallback}`；并断言 `temp_max >= temp_min` |
| `travel_share_negative` | `GET /api/travel/share/<不存在 id>` → `404`；`detail` 为字符串且含「分享」 |

每条用例都至少有一个状态码之外的字段/结构/内容断言；两个负向用例（未登录 401、分享 404）是真实负向路径。

## 4. 为什么不测某些接口

- **`POST /api/travel/plan`**：重计算（可能触发外部地图/LLM），契约明确排除；只读巡检不碰。
- **`POST /api/travel/feedback`**：写生产数据，契约明确排除。
- **`POST /api/travel/share` / Artalk 评论**：写数据，且需要构造合法行程；不属于本次只读范围。
- **`GET /api/travel/poi`**：依赖 AMap key 与坐标，外部依赖不稳定，契约未要求。
- **性能/并发/UI 端到端**：契约 §6 明确不做。

## 5. 幂等与清理

- `task_lifecycle` 创建的任务名固定为 `e2e-api-suite`，`task_id` 由服务端随机生成；
  用例内先记 `tasks` 基线，再创建，最后 `DELETE` 并回读 `404`，最后对比任务数组长度与基线一致。
- 连跑两次结果一致；跑完 `GET /api/tasks` 不应出现本次创建的任务（`POST` 返回的 `task_id` 已删除）。
- 运行产物落在 `.panguverse/api-tests/`；仓库 `.gitignore` 已有 `.panguverse/` 规则，
  产物天然不入库，**无需追加**。

## 6. 已知限制与发现

1. **公网站点响应头缺 charset（真实问题）**：`https://seres-ai.com` 返回
   `Content-Type: text/html`，没有 `charset=utf-8`。页面内虽有
   `<meta charset="utf-8">`，但 `requests` 按 RFC 2616 对 `text/*` 默认用
   ISO-8859-1 解码，中文变成 mojibake（`三木` → `ä¸‰æœ¨`）。因此首页断言改用
   页面里 ASCII 可见的 `vlog` 与 `charset="utf-8"` 声明；直接断言汉字「三木」
   在当前客户端+服务端组合下不可能通过。
   - 站点侧修复：nginx 对 `text/html` 输出 `charset utf-8;`（或后端补 `charset=utf-8`）。
   - 框架侧可改进：`http/client.py` 在无 charset 时按内容嗅探（`apparent_encoding`）而非拉丁-1。
2. **错误密码限流**：服务端阈值 5 次失败 / 5 分钟 / IP。场景每次运行只试 **1 次**，
   且紧接一次成功登录（`record_success` 会清空该 IP 的失败窗口），所以连续跑安全；
   不要把 `login_bad` 挪到成功登录之后或复制多次。
3. **任务数基线**：`list_before`/`list_after` 使用「长度相等」判定，假设本次运行期间
   没有其他人并发创建/删除任务（单机调试环境成立）。
4. **天气日期**：`WEATHER_DATE` 固定；日期超出 AMap 3 天预报窗口时后端返回
   `source=fallback`，断言只校验字段契约与数值关系，不校验 `source` 具体取值。
5. **未验证**：`POST /api/runs`（异步跑任务）、`POST /api/tasks/{id}/run`、
   报告产物下载（`report.html`/`junit.xml` 的 HTTP 出口）、文件系统写原子；
   本次只覆盖契约 §2 列出的只读/CRUD 路径。

## 7. 凭据检查

跑完用下面命令确认产物与仓库里没有明文（不打印密码本身）：

```bash
set -a; . /tmp/pv-test-creds.env; set +a
grep -rlF "$API_PASSWORD" scenarios docs .panguverse/api-tests && echo LEAK || echo CLEAN
```

预期输出 `CLEAN`。
