# PanguVerse 平台架构：从「API 测试工具」到「通用原子编排平台」

> 目标：**各行各业把自己的业务劈成原子步骤，AI 负责组合成脚本，脚本再组合成更复杂的业务**。
> 社区的长期资产不是某个工具，而是**原子库与领域包**——越多人在维护和共享原子，能自动化的业务就越多。

这份文档说明平台为了承载这个目标做了什么架构选择、现在到哪一步、以及下一步怎么走。

---

## 1. 一句话架构

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  生态层（未来）   原子/领域包的共享、版本、依赖、签名、注册中心                  │
├──────────────────────────────────────────────────────────────────────────────┤
│  领域包  panguverse/packs/*                                                  │
│    core（通用）· api_testing（HTTP）· local_fs（文件系统）· 你的行业包          │
│    提供：原子 + 领域语义 + 语法糖 + 意图模板 + 规划提示 + 能力声明               │
├──────────────────────────────────────────────────────────────────────────────┤
│  契约  panguverse/core/contract.py                                           │
│    类型化 I/O · 副作用 · 能力 · 密钥 · 中立失败类别 · 恢复策略 · 作用对象标识     │
├──────────────────────────────────────────────────────────────────────────────┤
│  内核（领域中立）                                                             │
│    engine 执行器 · axe 规划/自愈/记忆 · core 分子与模板 · report 报告           │
│    规则：**内核不认识任何具体协议**，一切领域知识通过契约向包索取                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

内核与领域包之间只有一个接口：**`AtomSemantics`**（`core/contract.py`）。
包回答内核四个问题：这次失败是什么性质、能不能重试、作用对象是谁、该怎么救/怎么建议。

---

## 2. 为什么内核必须领域中立

第一个业务闭环（API 自动化测试）实现完成后，我们做了一次耦合体检，结果是 **153 处**领域词汇散落在内核里：

| 位置 | 当时的领域知识 |
| --- | --- |
| `engine/executor.py` | `classify_status()`：401/403/404/409/429/5xx 的语义；按 `status_code` 判断「软失败」 |
| `axe/healer.py` | `TRANSIENT_KINDS`、`AUTH_KINDS`、「id/name 含 login/token 就是认证步骤」的猜测、HTTP 修复建议文案 |
| `axe/memory.py` | 用 **URL 路径**当经验标识（`request_target`）、HTTP 失败类型白名单 |
| `axe/planner.py` | 规划提示词里写死 `Bearer ${token}`、状态码断言惯例 |
| `spec/loader.py` | 只为 `http_request` 应用套件默认值；`assert:` 糖写死 status/header/latency |
| `report/` | 直接渲染 `status_code`、`http_requests` |

这段代码能跑，但**加一个行业就要改内核**——"各行各业"就成了空话。所以做了三件事：

1. **契约化**：把「类型、副作用、能力、失败类别、恢复策略、作用对象」抽成显式声明（§3）；
2. **迁移**：HTTP 的全部知识搬进 `packs/api_testing.py`，新增 `packs/local_fs.py` 作为**非 API 领域**的对照实现；
3. **立规**：加了一条架构守护测试——内核文件里出现 `status_code`/`Bearer`/`server_error` 等词汇就 fail
   （`tests/test_packs.py::TestKernelStaysDomainNeutral`，用 AST 剔掉文档字符串后再扫）。
   这条测试当天就抓到一次真实回流：LLM 规划提示词里还写着 `Bearer ${token}`。

---

## 3. 原子契约

原子不再只是「一个函数」，而是一份**可被静态分析、可被跨领域组合**的声明：

| 契约字段 | 作用 | 谁消费 |
| --- | --- | --- |
| `inputs` / `outputs` + `required` | 类型化 I/O | 静态校验、LLM 规划、报告 |
| `effects` / `effect_resolver` | 副作用强度：`pure`/`read`/`write`/`mutate`/`destructive` | 重试安全性、并发与回滚决策（未来） |
| `idempotent` / `retry_safe_for()` | 显式幂等声明，或由副作用推导 | 自愈器：**非幂等原子不会被自动重试** |
| `capabilities` | 运行时能力：`network`/`filesystem`/… | 能力授权（§8） |
| `secret_inputs` / `secret_outputs` | 密钥标记 | 报告打码（按值替换） |
| `declares_variables` | 「名字即变量名」的输入（如 `set_var.name`、`json_get.save_as`） | 静态校验，避免误报未知变量 |
| `domain` / `pack` / `version` | 归属与版本 | 包管理、清单导出 |
| 领域语义钩子（见下） | 失败分类/重试/恢复/建议/作用对象 | 执行器、自愈器、经验库 |

### 中立失败类别（内核决策依据）

`FailureClass`：`transient` · `credentials` · `permission` · `missing_resource` · `conflict` ·
`invalid_input` · `expectation` · `internal` · `unknown`

- **领域标签**（`server_error`、`file_not_found`…）由包定义，用于报告与经验；
- **中立类别**用于跨领域决策，也让经验可以跨领域复用（`transient` 在任何行业都成立）。

原子也可以自己报错得很具体：抛 `AtomFailure(kind="file_not_found", failure_class="missing_resource")`
即可，内核无需认识文件系统。

---

## 4. 领域包（Atom Pack）

一个包 = 原子 + 语义 + 语法糖 + 意图模板 + 能力声明 + 清单（可导出、可评审）：

```
📦 api_testing v0.1.0 领域=api     原子 6 个  能力 network      语义 api_testing  模板 7 个
📦 core        v0.1.0 领域=core    原子 8 个  能力 -            语义 generic      模板 0 个
📦 local_fs    v0.1.0 领域=filesystem 原子 10 个 能力 filesystem 语义 local_fs     模板 3 个
```

包的清单里包含每个原子的完整契约（类型/副作用/能力/密钥），因此可以直接交给评审或共享：

```bash
panguverse packs list            # 有哪些包
panguverse packs show local_fs   # 某个包的原子、语义、能力
panguverse packs validate --strict
panguverse packs manifest --out platform.json   # 导出全平台清单
```

### 语义钩子一览（内核 ↔ 包的唯一接口）

| 钩子 | 内核在什么时候问 | api_testing 的回答 | local_fs 的回答 |
| --- | --- | --- | --- |
| `failure_class` / `failure_kind` | 失败发生后 | 503 → (`transient`, `server_error`) | 文件不存在 → (`missing_resource`, `file_not_found`) |
| `is_retryable` | 决定重试前 | 5xx/429/超时 → 可重试 | 资源繁忙 → 可重试 |
| `result_status` | 成功返回后 | 5xx/429 是「藏在结果里的失败」 | 无此概念（出错就抛异常） |
| `target` | 记录经验时 | **URL 路径模板** | **文件路径** |
| `recoveries` | 重试耗尽后 | 401/403 → 重新认证后重试 | 锁等待 → 退避重试 |
| `advice` | 只给建议时 | HTTP 专属排查建议 | 文件系统专属建议 |
| `sugar` / `sugar_keys` | 加载场景时 | `assert: {status, json, schema, latency_max_ms, header, body_contains}` | `expect: {exists, contains}` |
| `step_defaults` | 加载场景时 | 套件 `headers`/`timeout` 作用于 http_request | 无（可由包自行扩展，如 encoding） |
| `planning_hints` | LLM 规划时 | 认证链路、状态码断言的写法 | 沙箱路径、expect 糖的写法 |
| `known_input_roots` | 静态校验时 | `in: body/headers/response` | — |

---

## 5. 声明式原子：不写 Python 也能贡献原子

平台的「共享」不能只面向会写代码的人。原子可以用**数据**描述（`*.pack.yaml`），
加载后与 Python 原子完全等价（同样的契约、同样的执行、同样的报告）：

```yaml
pack:
  name: ops_kit
  domain: ops
  version: 0.1.0
  description: 运维原子（纯数据定义）
  requires: [filesystem]

atoms:
  - name: snapshot_file
    description: 把源文件复制到目标位置，并断言目标存在
    inputs:      {source: string, target: string}      # 类型声明（给人和 LLM 看）
    outputs:     {target: string, exists: boolean}     # 类型声明
    steps:                                             # 用已有原子拼出来
      - id: copy
        use: fs_copy
        with: {source: "${source}", target: "${target}"}
      - id: verify
        use: assert_file_exists
        with: {path: "${copy.target}"}
    outputs_map:                                       # 对外输出映射
      target: ${copy.target}
      exists: true
```

```bash
panguverse run scenario.yaml --packs examples/packs/   # 加载并使用声明式包
panguverse packs validate examples/packs/ops_kit.pack.yaml
```

完整示例见 [`examples/packs/ops_kit.pack.yaml`](../examples/packs/ops_kit.pack.yaml)，
对应测试见 `tests/test_packs.py::TestDeclarativePack`。

---

## 6. 静态计划校验：在执行之前发现问题

平台化后，计划由很多人写、原子由很多人维护，错误必须在 `plan` 阶段就暴露：

| 代码 | 级别 | 含义 |
| --- | --- | --- |
| P001 | error | 原子不存在（拼写错误 / 包没加载） |
| P002 | error | 缺少必填输入 |
| P101 | error | 模板引用了**没有任何步骤产出**的变量（带「是否想写 `${token}`？」提示） |
| P102 | error | 用了**别的领域包**的语法糖（例如在文件原子上写 `assert:`） |
| P201 | warning | 字面量与声明的端口类型不符 |
| P202 | warning | 非幂等副作用（mutate/destructive）的步骤开了重试 |
| P203 | warning | 缺少运行时能力授权 |
| P204 | warning | 相对根取值不在该原子支持的范围内 |

```bash
panguverse plan -i "备份配置文件并校验" --json     # 校验结果随计划一起输出
```

---

## 7. 安全与治理

平台化的前提是「敢让别人共享的原子跑在自己的数据上」，因此安全是设计的一部分而不是补丁：

| 机制 | 说明 |
| --- | --- |
| **能力授权** | 原子声明 `capabilities`；`--allow-capabilities network,filesystem` 授权后严格校验，未授权的原子直接拒绝执行（测试：`TestCapabilities`） |
| **沙箱根** | 文件系统包默认只允许读写 `FS_ROOT` 内的路径，越界即 `path_escape`（`permission` 类别），并带测试 |
| **密钥打码** | 场景 `secrets:` / 原子 `secret_outputs` / `capture: {secret: true}` 三处声明都会触发**按值**全文替换；有回归测试扫描全部产物要求零明文（含 token 轮换场景） |
| **非幂等不自动重试** | 自愈器读取原子契约：`mutate`/`destructive` 默认不做自动重试（避免重复扣款/重复建单），只给建议；计划里显式写 `retry` 才重试 |
| **凭证失败例外** | 401/403 说明请求已被拒绝、没有副作用，因此「重新认证后重试」是安全的——这条判断写在领域包里，内核只执行 |
| **不做魔法修补** | 语义类失败（断言不符/404/409）不猜、不改写计划，只产出可执行建议并沉淀成经验 |

---

## 8. 组合与进化（跨领域通用）

- **分子即原子**：一个用例（分子）`.seal()` 之后就是一个原子，可以被更大的分子引用，层级无限；
- **回归库**：跑通的用例自动沉淀（带版本与运行次数），`panguverse regression` 直接复用，不再经过规划器；
- **经验库**：失败/重试用例沉淀为 `原子@作用对象:失败类型` 的经验；
  检索用**中立类别**，所以「瞬态故障要重试」这条经验对 HTTP、文件、数据库同样成立；
- **经验回灌**：下次规划命中**同一作用对象**时自动补上已验证的重试策略，并在计划备注里说明来源。

跨领域组合的真实例子（仓库自带场景 `scenarios/ops_snapshot.yaml`，跑通 7/7）：

```
登录(API) → 下单(API) → 订单落盘(fs) → 回读(fs) → 字段断言(通用) → 列目录(fs) → 归档(fs)
```

---

## 9. 如何新增一个行业（5 步）

```bash
# 1. 定义领域语义（把本行业的信号翻译成中立概念）
class BillingSemantics(GenericSemantics):
    name = "billing"
    def failure_kind(self, atom, outputs, exc): ...      # 例如 insufficient_balance
    def failure_class(self, atom, outputs, exc): ...     # → FailureClass.CONFLICT
    def target(self, atom, inputs): ...                  # 例如 account_id
    def recoveries(self, atom, **kw): ...                # 例如「先充值再重试」（谨慎）
    def sugar(self, step_id, step, *, atom=None): ...    # 例如 amount: 与 currency: 的糖

# 2. 写原子（Python 或 YAML），声明类型/副作用/能力
# 3. 组装包
pack = AtomPack(name="billing", domain="billing", semantics=BillingSemantics(),
                atoms=[...], requires=("network",), intent_templates=[...])
# 4. 加载
panguverse run scenario.yaml --packs ./my_billing_pack/
# 5. 校验与自检
panguverse packs validate ./my_billing_pack --strict
```

**不需要改内核一行代码。** 上面的架构守护测试会保证这条约定不被破坏。

---

## 10. 现状与边界（诚实清单）

已经具备：

- ✅ 领域中立的执行/自愈/记忆/报告 + 契约、领域包、声明式原子、静态校验、能力授权、沙箱、密钥治理
- ✅ 三个包（core / api_testing / local_fs）、24 个原子、10 个意图模板
- ✅ 183 个测试（含跨领域组合、能力授权、沙箱越界、架构守护）
- ✅ 零第三方依赖即可离线跑通完整闭环（含自我进化的回归复用）

还没做（按重要性排序，也是下一步的重点）：

1. **包的分发与依赖**：目前包靠「同一进程注册 + `--packs` 目录」加载；缺版本约束、依赖求解、签名与来源校验（生态的地基）；
2. **并发与 DAG 调度**：分子目前顺序执行、`foreach` 串行；缺并行分支、汇聚、超时与取消传播；
3. **补偿与回滚**：缺少 `compensate()` 约定，长流程失败后无法自动回滚已产生的副作用；
4. **人机协同**：缺少审批门（approval gate）与「危险动作二次确认」，这是生产环境落地的必需项；
5. **跨步骤类型推断**：静态校验目前做引用检查与字面量类型检查，还没有基于端口的类型推导；
6. **可视化与调试**：没有编排画布、断点重跑、单步执行；
7. **分布式执行**：没有远程 worker / 凭据托管，能力授权还只是进程内约束。

---

## 11. 路线图

| 版本 | 主题 | 关键交付 |
| --- | --- | --- |
| `v0.1` ✅ | **开天** | 原子/分子/盘古斧 + API 测试业务闭环（规划→执行→自愈→报告→沉淀→复用） |
| `v0.1.x` ✅ | **通神** | 原子契约、领域包体系、声明式原子、静态校验、能力授权、架构守护测试 |
| `v0.2` | **造物** | 包注册中心：版本约束与依赖求解、签名与来源校验、`panguverse packs install`、原子集市 |
| `v0.3` | **观天** | 编排画布与原子图谱、断点重跑与单步调试、运行回放 |
| `v0.4` | **自驱** | 并发/DAG、补偿回滚、审批门、自治循环（执行→观察→沉淀→进化）自动开 PR |
| `v1.0` | **宇宙** | 多行业包生态 + 分布式执行 + 原子经济：谁的原子被用得多，谁的能力就长得快 |

---

## 12. 目录映射

```
panguverse/
├── core/contract.py     # 原子契约：类型/副作用/能力/失败类别/恢复/作用对象/意图模板
├── core/pack.py         # 领域包与包仓库：加载、校验、清单、声明式原子
├── core/validation.py   # 静态计划校验（P001…P204）
├── core/registry.py     # 原子库 + 每个原子的领域语义
├── engine/executor.py   # 执行器（领域中立：只调用契约与语义）
├── axe/planner.py       # 规划器：规格 / LLM（提示词由包提供）/ 领域包模板
├── axe/healer.py        # 自愈器（决策领域无关，策略来自包）
├── axe/memory.py        # 记忆与进化（经验按中立类别检索）
├── report/              # JSON / HTML / JUnit
├── packs/               # 领域包：core / api_testing / local_fs（+ 模板文件）
└── spec/loader.py       # 场景加载（默认值与语法糖委托给包的语义）
examples/packs/          # 声明式领域包示例（纯 YAML，可共享）
scenarios/               # 仓库自带场景（含跨领域组合）
tests/test_packs.py      # 平台能力 + 架构红线测试
```
