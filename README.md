<p align="center">
  <img src="https://img.shields.io/badge/status-alpha-orange" alt="status">
  <img src="https://img.shields.io/badge/license-Apache 2.0-blue" alt="license">
  <img src="https://img.shields.io/badge/python-3.10+-green" alt="python">
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
| **原子** | `atoms/` | `http_request`（4xx/5xx 不抛异常，404 是合法结果）/ 断言 / 提取 / 流程，共 14 个原语 |
| **执行** | `engine/executor.py` | 变量模板 `${...}`、`capture`、`foreach` 数据驱动、`when` 条件、失败即停与 `optional` |
| **自愈** | `engine` + `axe/healer.py` | 5xx/超时/网络退避重试；401/403 自动重新执行认证步骤后重试；语义失败只给可执行建议 |
| **报告** | `report/` | JSON（机器）/ 自包含 HTML（人看）/ JUnit XML（CI）；**密钥按值打码，有测试守着** |
| **沉淀** | `axe/memory.py` | 跑通的用例 `seal` 成回归原子；失败与自愈沉淀成经验 `lessons.jsonl` |
| **进化** | `apply_lessons_to_plan` | 下次规划自动把经验里的重试策略补进计划（并在计划备注里说明来源） |
| **组合** | `Molecule.seal()` | 分子即原子：登录流程 seal 后可当步骤用在更大的流程里，层级无限 |

细节文档：[docs/api-testing.md](./docs/api-testing.md)（场景语法 / 原子清单 / 报告 / 扩展自定义原子 / FAQ）。

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

- **🪓 盘古斧 AI 编排** — 自然语言描述意图，AI 自动拆解、选原子、编排执行（无密钥时规则模板兜底，闭环不断）
- **♾️ 原子递归组合** — 原子组合成分子，分子 seal 成新原子，层级无限
- **🧩 场景化原子定义** — 不预设"万能原子"，每个领域定义自己的原语
- **🛡️ 自愈执行** — 失败自动重试、重新认证、经验复用；语义失败给出可执行建议（全部可审计）
- **📈 宇宙膨胀** — 跑通的用例自动沉淀回原子库，经验库驱动下次计划自动补强
- **🌐 多 AI 后端** — 支持 OpenAI / Anthropic 兼容接口 / 本地 LLM；核心链路零依赖、可离线
- **✅ 已落地闭环** — API 自动化测试：场景/意图 → 计划 → 执行 → 断言 → 报告 → 自愈 → 沉淀 → 回归复用

---

## 路线图 / Roadmap

- [x] `v0.1` — **开天**：核心抽象 Atom、PanguAxe、Atom Registry
      —— ✅ 已完成第一块落地业务：**API 自动化测试业务闭环**（规划 / 原子执行 / 断言 / 报告 / 自愈 / 沉淀 / 回归复用），
      内置 mock 被测量与一键演示：`python -m panguverse demo e2e`，详见 [docs/api-testing.md](./docs/api-testing.md)
- [ ] `v0.2` — **造物**：Atom Marketplace，社区共享原子
- [ ] `v0.3` — **观天**：可视化编排面板，原子关系图谱
- [ ] `v0.4` — **自驱**：自治循环，执行→观察→沉淀→进化
- [ ] `v1.0` — **PanguVerse**：万物自驱，混沌终成宇宙

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
