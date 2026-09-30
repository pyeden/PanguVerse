# API 自动化测试业务闭环

这是 PanguVerse 的第一条落地业务闭环：**用「原子编排」把 API 自动化测试从「人写脚本」变成「意图 → 计划 → 执行 → 自愈 → 沉淀 → 复用」的自动循环**。

```bash
# 克隆后零安装即可跑通（只用标准库）
python -m panguverse demo e2e
```

---

## 1. 闭环长什么样

```
   意图 / 场景规格
        │  ① 规划（Spec / LLM / 规则三层降级）
        ▼
   测试计划 TestPlan（用例 = 分子 Molecule，步骤 = 原子 Atom）
        │  ② 校验（原子是否存在、必填输入是否齐全 → 执行前 fail fast）
        ▼
   执行引擎 Executor
        │  ③ 执行：变量传递 · capture · foreach · when · 断言
        │  ④ 自愈：步骤级 retry（5xx/超时/网络）+ 自愈器（重新认证 / 修复重试 / 修复建议）
        ▼
   报告 Report（JSON · 自包含 HTML · JUnit XML）
        │  ⑤ 沉淀 Evolution：跑通用例 → 回归原子；失败与自愈 → 经验 lessons
        ▼
   回归复用 Regression（直接跑沉淀用例，不再规划）
        └────────────► 下次运行带着经验出发（计划自动补强）
```

一次真实运行（`panguverse demo e2e`，内置 mock API 作为被测量）：

```
🪓 PanguVerse 闭环完成
  运行: 6c08697153ee  计划来源: spec（spec）
  结果: 6/6 通过，失败 0，异常 0，跳过 0，自愈 1，HTTP 19 次，耗时 0.14s
  沉淀: 回归原子 +6，经验 +1，回归库共 6 条
  回归复用: 6/6 通过（无需重新规划）
  报告[json]: .panguverse/runs/6c08697153ee/report.json
  报告[html]: .panguverse/runs/6c08697153ee/report.html
  报告[junit]: .panguverse/runs/6c08697153ee/junit.xml
```

---

## 2. 三个核心概念

| 概念 | 对应 | 在 API 测试里的样子 |
| --- | --- | --- |
| ⚛️ **Atom 原子** | `panguverse.Atom` | `http_request` / `assert_status` / `assert_json` / `json_get` … |
| 🧬 **Molecule 分子** | `panguverse.Molecule` | 一个测试用例（步骤序列）；`.seal()` 后成为**新原子**，可被更高层复用 |
| 🪓 **PanguAxe 盘古斧** | `panguverse.PanguAxe` | 规划 + 执行 + 自愈 + 沉淀的编排大脑 |

关键点：**原子不是全局通用的**（README 的核心洞见）。API 测试领域的原语就上面这些；
其他领域（运维、财务、生活）定义自己的原子，`AtomRegistry` 负责统一寻址，LLM 只从清单里选原子。

---

## 3. 快速上手

### 3.1 跑通闭环（零依赖）

```bash
python -m panguverse demo e2e                    # 一键：内置 mock API + 全部场景 + 报告 + 回归复用
python -m panguverse demo e2e --no-regression    # 不跑回归复用
python -m panguverse demo api --port 8899        # 只启动内置被测量，自己拿 curl/Postman 玩
```

### 3.2 用自然语言驱动（无密钥也能跑）

```bash
python -m panguverse loop -i "检查服务健康状态，并验证登录下单支付闭环" \
  --env BASE_URL=http://127.0.0.1:8899 --regression
```

- 配置了 LLM 密钥 → **AI 拆解**成计划（结构化 JSON + 计划校验 + 一次自修复）；
- 没配置 → **规则模板**兜底（关键词 → 健康检查/认证/订单/CRUD/负向/自愈模板），闭环不断。

```bash
export PANGUVERSE_AI_BACKEND=openai      # 或 anthropic
export PANGUVERSE_API_KEY=sk-xxx
export PANGUVERSE_MODEL=gpt-4o-mini
```

### 3.3 用场景文件（推荐进版本库）

```bash
python -m panguverse plan -f scenarios/order_flow.yaml     # 只看计划与校验
python -m panguverse run  scenarios/order_flow.yaml --env BASE_URL=http://127.0.0.1:8899
python -m panguverse loop -f scenarios/ --regression       # 目录 = 合并多个场景文件
python -m panguverse regression                            # 只跑已沉淀的回归用例
```

### 3.4 作为库使用

```python
from panguverse import PanguAxe, PlanRequest

axe = PanguAxe()                       # 工作区默认 .panguverse/
result = axe.loop(
    PlanRequest(intent="测试用户 CRUD 与 401 负向路径", env={"BASE_URL": "http://127.0.0.1:8899"}),
    regression=True,
)
print(result.text())
print(result.run.summary)              # {'total': .., 'passed': .., 'healed_cases': ..}
```

---

## 4. 场景语法

```yaml
name: 订单业务闭环
env:
  BASE_URL: http://127.0.0.1:8899
  API_USERNAME: demo
  API_PASSWORD: demo123

defaults:                     # 作用于所有 http_request 步骤
  headers:
    Content-Type: application/json
  retry: { attempts: 3, delay_ms: 100, on: [server_error, timeout, network] }

cases:
  - id: order_flow            # 用例 id（回归库里的主键）
    name: 登录 → 下单 → 支付
    tags: [smoke, order]      # 可用 --tag 过滤
    secrets: [token]          # 明文在报告里打码
    steps:
      - id: login             # 步骤 id：输出会挂到 vars["login"]
        use: http_request     # 原子名（可写成 atom:）
        tags: [auth]          # tags 含 auth → 自愈时可被重新执行
        with:
          method: POST
          url: ${env.BASE_URL}/api/login
          json: { username: "${env.API_USERNAME}", password: "${env.API_PASSWORD}" }
        capture:              # 从本步骤输出里取变量
          token: body.token
          user: body.user.name
        assert:               # 断言糖：展开成 assert_* 原子步骤
          status: 200
          latency_max_ms: 1500
          json:
            - path: token
              exists: true
              type: string
      - id: create            # 用上一步的变量
        use: http_request
        foreach: ${skus}      # 数据驱动：对列表每个元素执行一次
        as: sku
        retry: { attempts: 2, delay_ms: 50, on: [server_error] }
        with:
          method: POST
          url: ${env.BASE_URL}/api/orders
          headers: { Authorization: "Bearer ${token}" }
          json: { sku: "${sku}", quantity: 1 }
          expect_status: 201
        capture:
          order_ids: body.data.id     # foreach 下 capture 会累积成列表
      - id: pay
        use: http_request
        when: ${order_ids|len}        # 条件步骤（false/0/空 → 跳过）
        with:
          method: POST
          url: ${env.BASE_URL}/api/orders/${order_ids[0]}/pay
          headers: { Authorization: "Bearer ${token}" }
        assert: { status: 200 }
```

### 模板语言（极简、可调试）

| 写法 | 含义 |
| --- | --- |
| `${env.BASE_URL}` | 环境变量；整串匹配时保留原始类型 |
| `${login.body.data.id}` | 步骤输出（`vars["login"]["body"]...`） |
| `${order_ids[0]}` / `${items.0.id}` | 列表下标 |
| `${skus|len}` | 过滤器：`default/int/float/str/bool/json/len/upper/lower/strip/urlencode` |
| `${env.MS|default:2000|int}` | 链式过滤器 |
| `Bearer ${token}` | 混排 → 字符串拼接 |

### 断言糖 `assert:` 的完整字段

```yaml
assert:
  status: 200              # 200 / [200,201] / "2xx" / "200,201"
  latency_max_ms: 1500
  body_contains: "order"
  header: { name: Content-Type, contains: application/json }
  schema: { type: object, required: [data] }     # JSON Schema 子集
  json:
    - { path: data.status, equals: CREATED }
    - { path: data.id, exists: true, type: string }
    - { path: data.amount, gt: 0 }
    - { path: data.name, matches: "^SKU-" }
```

---

## 5. 内置原子一览

`panguverse atoms` 可随时查看；`panguverse atoms --json` 输出结构化清单。

| 分类 | 原子 | 说明 |
| --- | --- | --- |
| HTTP | `http_request` | 发请求；**4xx/5xx 不抛异常**（404 是合法被测结果），只有连不上/超时才报错 |
| 提取 | `json_get` / `regex_extract` / `header_get` / `set_var` | 取变量、写变量，串起「登录 → 拿 token → 下单」 |
| 断言 | `assert_status` / `assert_json` / `assert_schema` / `assert_latency` / `assert_header` / `assert_body_contains` | 失败带 `expected`/`actual`/`path`，报告直接给差异 |
| 流程 | `sleep` / `log` / `fail` | 轮询等待、轨迹记录、负向用例「不该走到这里」 |

`http_request` 的关键输出：`status_code`、`body`（JSON 自动解析）、`headers`、`text`、`elapsed_ms`、`request`（打码后的请求记录）。
它还会把结果写到 `last_response`，所以 `assert_status` 之类的断言不写 `source` 也默认断言最近一次响应。

---

## 6. 自愈：两层机制

| 层 | 触发 | 行为 | 在哪看 |
| --- | --- | --- | --- |
| 步骤级重试 | 网络/超时/5xx/限流 + 步骤声明了 `retry` | 指数退避重试到成功或耗尽 | 报告 `步骤.retries/attempts` |
| 自愈器 Healer | 重试耗尽后仍失败 | 401/403 → 重新执行认证步骤（`tags: [auth]`）后重试；瞬态故障 → 有限次重试；命中经验库 → 复用已验证策略 | 报告 `自愈事件`（kind/action/success/reason/suggestion） |
| 建议 | 语义类失败（断言不符 / 404 / 409 / 400） | **不做魔法修补**，给出可执行排查建议（可选 LLM 增强） | 报告 `suggestion` |

内置的 `scenarios/self_healing.yaml` 就是这两条路径的可复现验证：

```bash
python -m panguverse run scenarios/self_healing.yaml --env BASE_URL=...
# ✅ flaky_retry_heal      抖动 500 → 重试 2 次后恢复
# ✅ auth_reauth_heal      token 被失效 → 自动重新登录 → 重试成功（自愈 1 次）
```

自愈的边界（刻意的）：
- 每个用例有自愈预算（默认 2 次动作），不会无限循环；
- 只对「可判定为瞬态或凭证问题」的失败动手，语义失败只给建议；
- 每个动作都落进报告，可审计、可关闭（`--no-heal`）。

---

## 7. 沉淀与进化（工作区 `.panguverse/`）

```
.panguverse/
├── registry.json      # 回归原子库：跑通的用例 seal 进来，带版本号/运行次数
├── lessons.jsonl      # 经验库：{signature: 原子:失败类型, kind, message, suggestion, fix}
└── runs/<run_id>/     # 每次运行的 plan.json / report.json / report.html / junit.xml
```

- **沉淀**：每次闭环结束，跑通的用例自动 seal 成回归原子（同 id 再跑会版本 +1）；
- **复用**：`panguverse regression` 或 `--regression` 直接执行回归库，**不再经过规划器**；
- **进化**：失败会沉淀成经验，签名是「**原子 @ 接口路径 : 失败类型**」，所以经验不会被套到别的接口上；
  - 瞬态失败（5xx/超时/网络/限流）→ 沉淀一条「重试策略」假设；
  - 下次规划命中**同一接口**且步骤没写 `retry` 时，**自动补上**并在计划备注里说明来源；
  - 重试后成功的步骤同样会被记住（已验证过的修复策略）。

端到端测试 `tests/test_e2e_loop.py::test_failure_creates_lesson_and_next_run_uses_it`
就验证了这条链路：第一次失败 → 沉淀策略 → 第二次自动补强后通过。

---

## 8. 报告

| 格式 | 用途 |
| --- | --- |
| `report.json` | 机器可读全量记录：计划来源、每步输入输出、HTTP 往返、自愈事件、变量快照（打码） |
| `report.html` | 单文件自包含（无外部资源）：闭环阶段时间线、用例卡片、步骤表格、失败差异、HTTP 详情 |
| `junit.xml` | 交给 CI（GitHub Actions / Jenkins / GitLab）消费 |

`--format json --format html` 可按需裁剪；`panguverse report <run_dir>` 能从 `report.json` 重新生成 HTML。

**安全**：报告只渲染打码后的数据。`secrets: [token]` 声明的变量会按值在全文替换为 `***`，
`Authorization`/`password`/`cookie` 等敏感字段名也会被遮蔽。

密钥登记只有**一个**判定入口（`Executor._capture_values`），因此正常步骤、`foreach`、自愈重新认证
三条路径行为一致——包括「token 失效 → 自愈重新登录」这种**一次运行里出现两个 token** 的场景，
两个值都会被登记并打码。`tests/test_reports_cli.py::test_secrets_masked_across_token_rotation`
守着这条红线（它会扫描全部产物，要求零 token 明文残留）。

---

## 9. 命令行速查

```bash
panguverse demo e2e [--no-regression] [--scenarios DIR] [--flaky-failures N]
panguverse demo api [--port 8899] [--token-ttl 3600]
panguverse plan  (-i "意图" | -f 场景) [--json]
panguverse run   场景 [--tag T] [--case ID] [--env K=V] [--format json|html|junit]
panguverse loop  (-i "意图" | -f 场景) [--regression] [--no-evolve] [--no-llm] [--no-heal] [--fail-fast] [--retry N]
panguverse regression [--tag regression]
panguverse atoms [--category C] [--tag T] [--json]
panguverse status [--json]
panguverse report <run_dir 或 report.json> [--format html]
panguverse init [--dir .]
```

退出码：`0` 全通过 · `1` 存在失败 · `2` 执行错误/用法错误（可直接接 CI）。

---

## 10. 对接自己的系统

1. 改 `env.BASE_URL` 指向你的服务；认证步骤里的 `API_USERNAME/API_PASSWORD` 用 `--env` 覆盖或接密钥管理；
2. 场景 YAML 里的接口路径换成你的路径（模板规划器产出的默认路径对齐内置 mock API）；
3. 需要新原语（签名、加解密、数据库断言、消息队列…）就写一个原子：

```python
from panguverse import atom, AtomRegistry

@atom("assert_db_row", description="断言数据库里存在某行", category="assert",
      inputs={"sql": "string", "exists": "bool"}, required=("sql",))
def assert_db_row(ctx, sql, exists=True):
    row = my_db.query(sql)
    if (row is not None) != exists:
        raise AssertionError(f"期望存在性 {exists}，实际 {row is not None}")
    return {"row": row}

registry.register(assert_db_row)     # 之后场景里就能 use: assert_db_row
```

4. 想让 AI 也用上你的原子：把原子注册进 `AtomRegistry` 即可，`catalog()` 会自动出现在 LLM 提示词里。

---

## 11. 常见问题

**Q：没有 OpenAI / Claude 密钥能跑吗？**
能。规划层三层降级：场景规格 → LLM → 规则模板。`panguverse demo e2e` 全程离线、零第三方依赖。

**Q：为什么 `http_request` 对 404 不报错？**
因为 404 是 API 测试里常见的**预期结果**。错误只保留给真正的执行故障（连不上、超时），
预期用断言表达 —— 这样「failed（测试失败）」和「error（执行错误）」在报告和退出码里才分得清。

**Q：步骤输出和变量重名了怎么办？**
`capture` 与原子写入的同名变量优先，步骤输出只在名字未被占用时写入 `vars[step.id]`。
建议步骤 id 用动词（`login`、`create_order`），变量用名词（`token`、`order_id`）。

**Q：测试会不会写坏生产数据？**
本框架只发请求，不做清理承诺。建议用专用测试账号/环境，场景里用唯一数据（时间戳、随机后缀）。

---

## 12. 目录结构

```
panguverse/
├── core/       # Atom / Molecule / RunContext（模板引擎）/ Registry / 异常 / 打码
├── atoms/      # 内置原子：http / extract / assertions / schema / flow
├── http/       # 传输层：requests 优先，缺失回退 urllib
├── engine/     # 执行引擎与结果模型（步骤/用例/运行三层）
├── axe/        # 盘古斧：规划器（spec/llm/rules）、自愈器、记忆与进化
├── spec/       # 场景加载：零依赖 YAML 子集解析器 + 断言糖展开
├── report/     # JSON / HTML / JUnit 报告
├── demo/       # 内置 mock API（被测量）与一键闭环演示
└── cli.py      # 命令行
scenarios/      # 示例场景（冒烟鉴权 / 订单闭环 / 自愈）
tests/          # 单测 + 端到端闭环验收
```
