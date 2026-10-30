# PanguVerse Web 版：用户故事与产品设计

> 状态：设计稿（v1，2026-10-30）
> 结论先行：CLI 的能力已经够用，但**使用门槛卡在三处**——记命令、改 YAML、找报告文件。
> Web 版不是"给 CLI 套个壳"，而是把「意图 → 计划 → 执行 → 自愈 → 报告 → 沉淀 → 回归」这条闭环变成**可看见、可点击、可复现**的一条流水线。

---

## 1. 定位

**一句话**：给「要自动化某个 HTTP 业务闭环、但不想写脚本」的工程师，一个打开浏览器就能跑通全链路、并能直接看到每次运行证据的界面。

**不是**：在线 IDE、持续集成平台、多租户 SaaS。第一版只服务**本机单人使用**。

---

## 2. 人物（Persona）

| Persona | 是谁 | 出问题的时刻 | 现在怎么做 | 痛点 |
| --- | --- | --- | --- | --- |
| **P1 测试工程师 小林** | 接手一个已有 API 服务的 QA，不熟 PanguVerse | 第一次要跑通演示，验证工具能不能用 | 翻 README，复制 `python -m panguverse demo e2e` | 装环境、看终端刷屏、报告在哪要自己 `find`；失败原因埋在 JSON 里 |
| **P2 后端开发 老周** | 自己改完接口想快速回归一遍 | 提交前想跑一次冒烟 + 回归 | 记住 `loop -i "..."` 的参数和 `--env` 写法 | 参数一多就要 `--help`；跑完只看得到"7/7 通过"，失败步骤要点开 HTML 才知道 |
| **P3 平台维护者/评审者 阿May** | 评估这套平台要不要推广到团队 | 想知道"原子库现在有什么、领域包边界在哪" | `atoms --json` 后自己拼表格 | 24 个原子的契约、领域包语义是文本流，不好评审也不好演示 |

---

## 3. 旅程（Before / During / After）

| 阶段 | 用户想做的事 | CLI 现状 | Web 版目标 |
| --- | --- | --- | --- |
| Before | 确认"这东西现在能跑吗" | `status` 四个数字，看不出健康度 | 首屏概览：原子数、领域包、回归库、LLM 状态、**一键自检**（跑内置 demo） |
| During | 选场景 / 说意图 → 跑一次 | 背子命令 + `--env` 键值对 | 表单：选场景或写一句话意图，环境变量用键值行填；点「运行」看阶段进度 |
| During | 失败了想知道**哪一步、为什么** | 终端一行 `0/7 通过`，细节在 `report.html` 文件里 | 运行详情页：用例 → 步骤树，失败步骤高亮，带 `message/expected/actual` 与自愈事件 |
| After | 把结果给别人看 / 归档 | 找到 `runs/<id>/` 目录，手动拷 html | 报告页内联预览 + 下载 HTML/JUnit，运行历史可回看 |
| After | 沉淀之后跑回归 | `regression` 子命令 | 概览页「回归库 N 条 · 跑一次」 |

---

## 4. 当前能力与差距（先看清有什么）

已有能力（CLI 实测）：

- 12 条子命令：`demo e2e` / `demo api` / `run` / `loop` / `plan` / `regression` / `atoms` / `packs {list,show,validate,manifest}` / `status` / `report` / `init`
- 24 个原子、3 个领域包（core / api_testing / local_fs）
- 产物固定：`.panguverse/runs/<run_id>/{report.json,report.html,junit.xml,plan.json}`、`lessons.jsonl`
- 零依赖内核（stdlib），有 `requests`/`PyYAML` 时自动增强

三条硬限制（必须写进验收）：

1. **运行是同步阻塞的**：`loop` 一次 0.14s（内置 demo）但真实目标可能几十秒，Web 请求不能干等 → 必须后台任务 + 轮询。
2. **报告是文件**：Web 只是"读文件 + 结构化展示"，不能重新设计数据模型；新字段一律不进 MVP。
3. **LLM 可选**：没密钥时规则兜底，界面必须显示"当前规划方式"，不能让用户以为 AI 在跑。

### 4.1 勘察时发现的真实缺陷（P0 必修）

本机 macOS 系统代理（`127.0.0.1:9981`）会被 `urllib` 套到 **localhost** 请求上：

- `python -m panguverse demo e2e` → **0/7 通过，5 个 error，耗时 76s**（全部 502 Bad Gateway）
- `pytest` → **24 项失败**
- 加 `NO_PROXY=127.0.0.1,localhost` 后：demo **7/7 通过 / 0.2s**，pytest **184 全绿**

原因：`panguverse/http/client.py` 用默认 opener，`urllib.request.getproxies()` 在 macOS 上读系统代理设置，回环地址未排除。

这是 Web 版第一天就会踩的坑（前端连 `127.0.0.1:7788`、后端连本机 mock API 都会被代理劫持），因此列为 P0：**回环地址强制直连**。

---

## 5. 用户故事（P0 闭环）

闭环定义：**打开 → 自检 → 跑一次 → 看懂失败 → 拿到报告**。

| ID | 用户故事 | 验收标准（可执行） |
| --- | --- | --- |
| **US-01** | 作为小林，我想 `panguverse serve` 一条命令打开界面，这样我不用学任何子命令 | `panguverse serve --workspace <dir> --port 7788` 启动后 `GET /` 返回 200 + HTML；`GET /api/status` 返回 200 JSON，含 `atoms`、`packs`、`regression_count`、`lessons_count`、`llm_enabled` |
| **US-02** | 作为小林，我想在首屏看到"这套平台现在是什么状态"，这样我判断能不能用 | 概览页显示原子数/领域包数/回归库条数/规划方式（LLM 或离线规则）；数据与 `panguverse status` 输出一致 |
| **US-03** | 作为小林，我想点一下就跑通内置演示，这样我 30 秒内确认环境可用 | 点「运行内置演示」→ 后台任务 → 页面轮询到 `done`，结果 `total/passed/failed/errored` 与 CLI 同一次运行一致；默认断言 `passed == total`（本机无代理干扰时 demo 为 7/7） |
| **US-04** | 作为老周，我想选一个场景文件直接跑，这样不用记 `run` 的路径参数 | 场景下拉来自 `GET /api/scenarios`；选中后 `POST /api/runs {mode:"scenario"}` → 任务完成，报告 `plan.source` 为该场景的 spec |
| **US-05** | 作为老周，我想用一句话描述意图并带上环境变量，这样不用手写 YAML | `POST /api/runs {mode:"intent", intent:"...", env:{BASE_URL:"..."}}`；无 LLM 密钥时 `plan.planner` 必须是规则/离线兜底值，界面明确标注"离线规则规划" |
| **US-06** | 作为老周，失败时我想直接看到哪个用例的哪一步挂了、期望什么、实际什么 | 运行详情页可展开用例 → 步骤；失败/异常步骤显示 `message`、`expected`、`actual`（缺字段时显示"未提供"而非空白） |
| **US-07** | 作为小林，我想看到自愈发生过什么 | 运行详情页展示 `healed_cases` 计数与每个 `HealEvent`（步骤名、策略、结果） |
| **US-08** | 作为小林，我想把报告发给同事 | `GET /api/runs/<id>/report.html` 内联预览；`GET /api/runs/<id>/junit.xml` 触发下载；文件内容与磁盘一致 |
| **US-09** | 作为阿May，我想浏览原子库和领域包，这样我能评审扩展性 | 原子列表支持按 `domain`/`tag`/`category` 过滤；点开原子显示输入/输出端口、必填、副作用、能力；领域包页显示原子数与语义 |
| **US-10** | 作为老周，我想沉淀后一键跑回归 | 概览页显示回归库条数，点击后 `POST /api/runs {mode:"regression"}`，不重新规划（`plan.source` 为回归库） |
| **US-11** | 作为任何本机用户，我不希望"本机直连"被系统代理破坏 | 目标或服务地址为 `127.0.0.1`/`localhost`/`::1` 时**不走代理**；在系统代理开启的机器上，demo 走 Web 仍 `passed == total`，`pytest` 无需 `NO_PROXY` 全绿 |

### P0 明确不做

在线支付/账号体系、多人协作、云端存储、LLM 对话式改场景、实时日志流（SSE）、场景 YAML 在线编辑器、报告历史对比、定时调度。

---

## 6. P1 / P2（延期及理由）

| 优先级 | 项 | 延期理由 |
| --- | --- | --- |
| P1 | 场景 YAML 在线编辑 + 保存 | 写坏场景会污染工作区，需要 schema 校验 + 备份策略，先让 P0 稳定 |
| P1 | 实时日志流（SSE/WebSocket） | 轮询已能满足"跑完看结果"；流式日志需处理断线重连与缓冲 |
| P1 | 运行历史对比（两次 run 差异） | 需要稳定 run 元数据契约，先积累真实数据 |
| P1 | Web 端配置 LLM 密钥 | 涉及密钥落盘与打码，必须复用 `core/redact.py` 并单独评审 |
| P2 | 多用户/远程访问 | 认证与会话管理是另一个量级 |
| P2 | 定时回归 + 通知 | 应复用 OpenClaw automations 而非自建调度 |

---

## 7. 成功度量

- **首次价值时间**：新用户从 `git clone` 到看见第一份通过报告 < 2 分钟（当前需读 README + 记命令，估 5-10 分钟）。
- **失败可解释率**：报告中失败步骤 100% 带 `message`；界面不得出现空白错误块。
- **回归成本**：跑一次内置 demo 的点击次数 ≤ 2。
- **零依赖底线**：`pyproject.toml` 的 `dependencies` 仍为空数组；`pip install -e .` 后无需 npm/node 即可使用。
- **代理鲁棒性**：系统代理开启时 `pytest` 全绿（当前 24 项失败）。

---

## 8. 风险

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| 后台任务与进程生命周期 | 服务重启丢 job 状态 | MVP 只存内存，前端在 job 丢失时回退到"运行历史"读取磁盘 |
| 长任务无超时 | 页面一直转圈 | 每个 job 有 `timeout`（默认 300s）+ 前端展示已运行秒数 |
| `http.server` 并发能力 | 单人够用，多人吃力 | MVP 明确单用户；文档标注不作为共享服务 |
| 静态文件安全 | 目录穿越 | 静态路由白名单 + `os.path.realpath` 前缀校验 |
| 工作区污染 | demo 写 `.panguverse/` | 所有写操作限定在 `--workspace` 目录内 |

---

## 9. 与 API 契约的关系

本文只回答"为什么做、给谁做、做到哪一步"。
请求/响应字段、错误码、文件归属、QA 清单见 `tasks/web-app-mvp-spec.md`。
