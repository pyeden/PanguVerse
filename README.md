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
git clone https://github.com/your-org/panguverse.git
cd panguverse

# 安装依赖
pip install -r requirements.txt

# 设置 AI 后端
export PANGUVERSE_AI_BACKEND=openai
export OPENAI_API_KEY=your-key

# 劈开混沌：用自然语言驱动自动化
panguverse run "每天上午 9 点检查所有服务器健康状态，异常时通知钉钉群"
```

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

- **🪓 盘古斧 AI 编排** — 自然语言描述意图，AI 自动拆解、选原子、编排执行
- **♾️ 原子递归组合** — 原子组合成分子，分子 seal 成新原子，层级无限
- **🧩 场景化原子定义** — 不预设"万能原子"，每个领域定义自己的原语
- **🛡️ 自愈执行** — 失败自动重试、降级、回滚，混沌中保持有序
- **📈 宇宙膨胀** — 原子库越用越丰富，自动化能力指数级增长
- **🌐 多 AI 后端** — 支持 OpenAI / Claude / 本地 LLM

---

## 路线图 / Roadmap

- [ ] `v0.1` — **开天**：核心抽象 Atom、PanguAxe、Atom Registry
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
