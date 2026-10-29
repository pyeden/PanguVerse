<p align="center">
  <a href="https://github.com/pyeden/PanguVerse/actions/workflows/ci.yml"><img src="https://github.com/pyeden/PanguVerse/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/status-alpha-orange" alt="status">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="license">
  <img src="https://img.shields.io/badge/python-3.10%2B-green" alt="python">
</p>

<h1 align="center">🪓 PanguVerse</h1>
<p align="center"><strong>劈开混沌，万物自驱。</strong></p>
<p align="center"><em>Cleave the chaos. Automate the universe.</em></p>

---

## 这是什么？ / What is PanguVerse?

> 盘古开天，一斧劈开混沌。天地分离，日月轮转，万物生息——**不需要任何人干预**。
> 
> 这是世界上的第一个自动化系统。
> 
> **PanguVerse** 继承了这一哲学：**AI 就是那把盘古斧**，它将世界逻辑劈成最小的 **原子步骤（Atom）**，然后让它们自组装、自运行、自进化。原子可以递归组合成新的原子——当原子足够多时，整个宇宙自行运转。
> 
> Pangu cleaved the chaos with a single strike of his axe. Heaven and earth separated. Sun and moon began their cycles. All things came to life — **with no human intervention needed**.
> 
> That was the world's first automation system.
> 
> **PanguVerse** inherits this philosophy: **AI is Pangu's axe**. It cleaves world logic into the smallest **atomic steps (Atoms)**, then lets them self-assemble, self-execute, and self-evolve. Atoms compose into new atoms recursively — with enough atoms, the entire universe runs itself.

---

## 盘古创世 → 原子创世 / Chaos → Cosmos

```
混沌 (Chaos)
    │
    ▼
  🪓 盘古斧 (AI)
    │
    ├── 劈开混沌 ──→ 天地分离 ──→ 世界结构显现
    │
    ▼
  ⚛️ 原子 (Atoms) ──→ 场景化定义的最小操作单元
    │
    ├── 自组装 ──→ 🧬 分子 (Molecules) ──→ 原子组合 = 新原子
    │
    ├── 递归 ──→ 🔗 高分子 (Macromolecules) ──→ 复杂业务流
    │
    └── ∞ ──→ 🌌 PanguVerse ──→ 万物自驱，无需干预
```

> 就像盘古一斧定乾坤，**AI 一斧定原子，万物自此有序**。
> 
> Just as Pangu's axe shaped the cosmos, **AI's axe defines atoms — and from them, order emerges**.

---

## 核心理念 / Core Philosophy

| 层级  | 概念  | 定义  |
| --- | --- | --- |
| 🪓 **盘古斧** | AI Orchestrator | 理解意图、拆解任务、编排原子的 AI 大脑 |
| ⚛️ **Atom** | 原子步骤 | 场景化定义的最小操作单元 |
| 🧬 **Molecule** | 分子  | 原子的组合，`.seal()` 后成为新的 Atom |
| 🔗 **Macromolecule** | 高分子 | 多层递归组合，复杂业务自动化 |
| 🌌 **PanguVerse** | 宇宙  | 原子足够多时，万物自动有序运转 |

> **核心洞见**：原子不是全局通用的——"原子"这一概念**针对具体场景具体定义**。每个领域定义自己的原语，AI 负责发现、组合、进化它们。
> 
> **Key insight**: Atoms are not universal — the concept of "atom" is defined **per domain, per context**. Each domain defines its own primitives; AI discovers, composes, and evolves them.

---

## 和传统 RPA / Workflow 引擎的区别

|     | 传统 RPA / Workflow | PanguVerse |
| --- | --- | --- |
| **步骤定义** | 人工录制 or 静态脚本 | AI 动态理解意图，场景化拆解为原子 |
| **组合方式** | 固定 DAG 编排 | 原子递归组合，分子生分子，无限层级 |
| **适应性** | 流程硬编码，变更成本高 | AI 实时决策，容错自愈 |
| **可扩展性** | 每新增场景重新开发 | 原子复用，生态指数级增长 |
| **终局** | 单场景自动化 | 混沌 → 有序 → 万物自驱 |

---

## 架构 / Architecture

```
                      混沌 (User Intent)
                     自然语言描述需求
                            │
                            ▼
              ┌─────────────────────────┐
              │      🪓 盘古斧           │
              │    AI Orchestrator       │
              │                         │
              │  ┌────────┐ ┌─────────┐ │
              │  │ 理解意图│→│ 拆解为  │ │
              │  │ Intent │ │  Atoms  │ │
              │  └────────┘ └─────────┘ │
              │        ↓                │
              │  ┌────────┐ ┌─────────┐ │
              │  │ 编排组合│→│ 验证执行 │ │
              │  │Compose │ │ Validate│ │
              │  └────────┘ └─────────┘ │
              └────────────┬────────────┘
                           ▼
              ┌─────────────────────────┐
              │     ⚡ 执行引擎           │
              │   Execution Engine       │
              │                         │
              │  ┌──────┐ ┌──────┐     │
              │  │取原子│→│ 执行  │     │
              │  │Fetch │ │ Run   │     │
              │  └──────┘ └──────┘     │
              │       ↓                 │
              │  ┌──────────┐          │
              │  │ 自愈重试  │          │
              │  │Self-Heal │          │
              │  └──────────┘          │
              └────────────┬────────────┘
                           ▼
              ┌─────────────────────────┐
              │    ⚛️ 原子库              │
              │    Atom Registry          │
              │                         │
              │  ┌────┐ ┌────┐ ┌────┐  │
              │  │内置 │ │自定义│ │组合 │  │
              │  │Built│ │Custom│ │Mole.│  │
              │  │-in  │ │     │ │cule │  │
              │  └────┘ └────┘ └────┘  │
              │         ...∞            │
              └────────────┬────────────┘
                           ▼
              ┌─────────────────────────┐
              │    🔄 开天辟地循环        │
              │   Chaos → Cosmos Loop    │
              │                         │
              │  执行 → 观察 → 沉淀 → 进化│
              │  Run → Watch → Learn → Evolve │
              └─────────────────────────┘
```

---

## 快速开始 / Quick Start

```bash
# 克隆项目
git clone https://github.com/pyeden/PanguVerse.git
cd PanguVerse

# 🎯 零依赖跑通第一条业务闭环：API 自动化测试（内置被测量，无需任何外部服务）
python -m panguverse demo e2e
```

输出（真实运行结果）：

```
🪓 PanguVerse 闭环完成
  运行: 6c08697153ee  计划来源: spec（spec）
  结果: 6/6 通过，失败 0，异常 0，跳过 0，自愈 1，HTTP 19 次，耗时 0.14s
  沉淀: 回归原子 +6，经验 +1，回归库共 6 条
  回归复用: 6/6 通过（无需重新规划）
  报告[html]: .panguverse/runs/6c08697153ee/report.html
```

接自己的系统：

```bash
# 用自然语言驱动（没有 AI 密钥时自动用规则模板兜底，闭环不断）
python -m panguverse loop -i "检查服务健康状态，并验证登录下单支付闭环" \
    --env BASE_URL=https://api.your-company.com --regression

# 或用场景 YAML（推荐进版本库，作为 CI 主线）
python -m panguverse run scenarios/order_flow.yaml --env BASE_URL=https://api.your-company.com
```

> 核心能力（原子引擎 / 执行器 / 规划器 / 报告）**只用 Python 标准库**，可离线运行；
> 可选增强：`pip install -r requirements.txt`（`requests` 更好的 HTTP 客户端、`PyYAML` 完整 YAML、`pytest` 开发测试）。

---

## 🎯 第一块落地业务：API 自动化测试业务闭环

把「人写测试脚本」换成 **意图 → 计划 → 执行 → 断言 → 报告 → 自愈 → 沉淀 → 复用** 的自动循环：

```
意图 / 场景规格 ──① 规划──► 测试计划（用例=分子，步骤=原子）──② 校验──► 执行引擎
                                                                            │
                       ⑥ 回归复用 ◄──⑤ 沉淀（回归原子 + 经验库）◄── ④ 自愈 ◄─┘
                                        │                              （步骤级重试 + 重新认证）
                                        └──► ③ 报告：JSON / 自包含 HTML / JUnit XML
```

| 闭环环节 | 实现 | 关键点 |
| --- | --- | --- |
| **拆解** | `axe/planner.py` | 场景规格 → LLM 规划（严格 JSON + 计划校验 + 一次自修复）→ 规则模板，**三层降级永不空转** |
| **原子** | `packs/` | 原子带**契约**（类型/副作用/能力/密钥）；HTTP 的 4xx/5xx 是合法被测结果，不是异常 |
| **执行** | `engine/executor.py` | 变量模板 `${...}`、`capture`、`foreach` 数据驱动、`when` 条件、失败即停与 `optional` |
| **自愈** | `engine` + `axe/healer.py` | 5xx/超时/网络退避重试；401/403 自动重新执行认证步骤后重试；非幂等原子不自动重试 |
| **报告** | `report/` | JSON（机器）/ 自包含 HTML（人看）/ JUnit XML（CI）；**密钥按值打码，有测试守着** |
| **沉淀** | `axe/memory.py` | 跑通的用例 `seal` 成回归原子；失败与自愈沉淀成经验 `lessons.jsonl` |
| **进化** | `apply_lessons_to_plan` | 下次规划命中同一作用对象时自动补强计划（并在备注里说明来源） |
| **组合** | `Molecule.seal()` | 分子即原子：登录流程 seal 后可当步骤用在更大的流程里，层级无限 |

细节文档：[docs/platform-architecture.md](./docs/platform-architecture.md)（平台架构与路线图）、
[docs/api-testing.md](./docs/api-testing.md)（场景语法 / 原子清单 / 报告 / 扩展自定义原子 / FAQ）。

---

## 🧩 平台通用能力：换行业 = 加一个包

API 测试只是第一个垂直场景。平台的目标是**各行各业把自己的业务劈成原子并共享**，
所以内核被刻意做成**领域中立**的：原子契约（类型化 I/O、副作用、能力、密钥、失败类别、恢复策略、
作用对象标识）与领域语义由**领域包**提供，执行/自愈/记忆/报告内核不认识任何具体协议。

```
📦 core        v0.1.0  领域=core        原子  8 个  能力 -            语义 generic
📦 api_testing v0.1.0  领域=api         原子  6 个  能力 network      语义 api_testing   意图模板 7 个
📦 local_fs    v0.1.0  领域=filesystem  原子 10 个  能力 filesystem   语义 local_fs      意图模板 3 个
```

```bash
panguverse packs list / show <name> / validate --strict / manifest --out platform.json
panguverse atoms --domain filesystem          # 按领域查看原子

# 非 API 领域的自然语言自动化：命中 local_fs 包的模板，内核零改动
panguverse loop -i "备份配置文件并校验备份存在" --env FS_ROOT=./demo
```

**跨领域组合**（自带场景 `scenarios/ops_snapshot.yaml`，跑通 7/7）：
接口取数 → 订单落盘 → 回读 → 字段断言（通用原子）→ 列目录 → 归档。

**不写 Python 也能贡献原子**（纯 YAML，见 [`examples/packs/ops_kit.pack.yaml`](./examples/packs/ops_kit.pack.yaml)）：

```yaml
pack: {name: ops_kit, domain: ops, version: 0.1.0, description: 运维原子, requires: [filesystem]}
atoms:
  - name: snapshot_file
    description: 复制文件并断言目标存在
    inputs:  {source: string, target: string}
    outputs: {target: string, exists: boolean}
    steps:
      - {id: copy,   use: fs_copy,            with: {source: "${source}", target: "${target}"}}
      - {id: verify, use: assert_file_exists, with: {path: "${copy.target}"}}
    outputs_map: {target: "${copy.target}", exists: true}
```

平台还内建了治理能力：**能力授权**（`--allow-capabilities network,filesystem`）、
**文件沙箱**（越界即拒绝）、**静态计划校验**（引用/类型/语法糖归属/非幂等重试，`panguverse plan --json`），
以及一条**架构守护测试**（内核文件里出现 HTTP 词汇就 fail）。

---

## 概念示例 / Conceptual Example

```python
from panguverse import Atom, PanguAxe

# 定义原子：最简操作单元
check_http = Atom(
    name="check_http",
    description="检查 HTTP 端点是否可达",
    inputs={"url": "string"},
    outputs={"status_code": "int", "latency_ms": "float"},
    run=lambda url: requests.get(url, timeout=5)
)

send_dingtalk = Atom(
    name="send_dingtalk",
    description="发送钉钉群消息",
    inputs={"webhook": "string", "message": "string"},
    run=lambda webhook, message: requests.post(webhook, json={"msg": message})
)

# 🪓 盘古斧：AI 自动编排原子
axe = PanguAxe()

# 自然语言描述意图，AI 自动选原子、组合、执行
axe.run("监控 https://api.example.com，挂了钉钉告警")

# 原子组合生成分子，分子可以 seal() 成为新原子
health_monitor = axe.compose(
    name="health_monitor",
    atoms=[check_http, send_dingtalk]
).seal()  # seal() → 注册为新的 Atom，可供更高层递归组合

# 分子即原子：health_monitor 现在可以用在更大的流程里
axe.run("对所有生产环境服务执行 health_monitor，汇总结果发邮件")
```

---

## 核心特性 / Key Features

- **🪓 盘古斧 AI 编排** — 自然语言描述意图，AI 自动拆解、选原子、编排执行（无密钥时领域包模板兜底，闭环不断）
- **♾️ 原子递归组合** — 原子组合成分子，分子 seal 成新原子，层级无限
- **🧩 领域包即扩展** — 内核领域中立；换行业 = 加一个包（原子 + 领域语义 + 语法糖 + 意图模板 + 能力声明）
- **📦 声明式原子** — 用 YAML 描述原子即可共享，不需要写 Python
- **🛡️ 自愈执行** — 失败自动重试、重新认证、经验复用；非幂等动作不自动重试；语义失败只给可执行建议
- **🔒 治理内建** — 能力授权、文件沙箱、密钥按值打码、静态计划校验（引用/类型/领域用错）
- **📈 宇宙膨胀** — 跑通的用例自动沉淀回原子库，经验按中立类别复用并被下次规划自动应用
- **🌐 多 AI 后端** — 支持 OpenAI / Anthropic 兼容接口 / 本地 LLM；核心链路零依赖、可离线
- **✅ 已落地闭环** — API 自动化测试 + 跨领域组合（API + 文件系统）均已端到端跑通

---

## 路线图 / Roadmap

- [x] `v0.1` — **开天**：核心抽象 Atom、PanguAxe、Atom Registry
      —— ✅ 第一块落地业务：**API 自动化测试业务闭环**（规划 / 原子执行 / 断言 / 报告 / 自愈 / 沉淀 / 回归复用），
      内置 mock 被测量与一键演示：`python -m panguverse demo e2e`，详见 [docs/api-testing.md](./docs/api-testing.md)
- [x] `v0.1.x` — **通神**：平台通用能力 —— 原子契约、**领域包体系**（core / api_testing / local_fs）、
      声明式（YAML）原子、静态计划校验、能力授权与沙箱、架构守护测试，
      详见 [docs/platform-architecture.md](./docs/platform-architecture.md)
- [ ] `v0.2` — **造物**：包注册中心 —— 版本约束与依赖求解、签名与来源校验、`packs install`、原子 Marketplace
- [ ] `v0.3` — **观天**：可视化编排面板、原子关系图谱、断点重跑与运行回放
- [ ] `v0.4` — **自驱**：并发/DAG、补偿回滚、审批门、自治循环（执行→观察→沉淀→进化）
- [ ] `v1.0` — **PanguVerse**：多行业包生态 + 分布式执行，万物自驱，混沌终成宇宙

---

## 贡献 / Contributing

盘古斧的力量来自社区的每一个原子。欢迎贡献：

- 定义新的原子步骤（场景不限：运维、开发、财务、生活……万物皆可原子化）
- 优化 AI 编排策略
- 提供领域原子模板
- 改进执行引擎和自愈机制

详见 [CONTRIBUTING.md](./CONTRIBUTING.md)

---

## 协议 / License

Apache 2.0 © 2026 PanguVerse Contributors

---

<p align="center">
  <sub>🪓 Built by developers who believe chaos can be cleaved — and order will follow.</sub>
</p>
