# PanguVerse 交接说明（给 opencode）

> 生成时间：2026-11-10 ｜ 代码版本：`main@0751b94` ｜ 用途：让接手的 AI 编码代理在 30 分钟内建立准确心智模型。

## 1. 这是什么

**PanguVerse 是通用自动化任务平台。** 任何领域的任务，都可以拆成原子步骤、组合成任务，并沉淀为可复用资产：

- **原子（Atom）**：带类型化契约的可执行实现（输入/输出/副作用/能力），是能力的最小单位。
- **任务（Task）**：节点图 + 版本（`wid@rev`），可审阅、发布、运行。
- **执行态**：`debug`（LLM 可自定义节点、可写脚本）→ 跑成功后人工**固化**为 `frozen`（确定性执行，不调 LLM）。
- **通用性来自资产沉淀**：原子库持续积累 + 缺原子时的自举（脚本 → 验证 → 注册为原子），覆盖面随使用增长，目标是覆盖任意领域，而不是靠预置模板。
- **不做的事**：不做聊天助手；不复刻 OpenClaw 的会话与渠道能力。

## 2. 快速启动

```bash
cd ~/PycharmProjects/PanguVerse
.venv/bin/python -m panguverse serve --host 127.0.0.1 --port 7788 \
  --workspace ~/.panguverse-dev --no-open
```

- 生产式启动脚本：`~/.panguverse-dev/serve-llm.sh`（注入 `~/.panguverse-dev/llm.env`，含 DeepSeek 配置，0600，勿提交）
- 登录：`admin` + 密码（向项目所有者索取，不要写进仓库）
- 前端构建：`cd webui && npm run build` → 产物写入 `panguverse/web/static/`
- 测试：`.venv/bin/python -m pytest -o addopts="" -q tests/test_web_*.py`
- 全量：`.venv/bin/python -m pytest -o addopts="" -q`（约 627 项）

**约束**：构建只能在持有仓库的机器上做（当前 MacBook）；不要提交 `data/`、`runs/`、`.panguverse/` 运行产物。

## 3. 代码地图

| 层 | 路径 | 职责 |
| --- | --- | --- |
| 内核 | `panguverse/core/` | Atom/契约/上下文/注册表/工作区包 |
| 执行 | `panguverse/axe/` | 规划器（LLM/规则）、执行器、healer、explore agent、固化抽象 |
| 领域包 | `panguverse/packs/` | web_browser、web_research、api_testing、local_fs、android_adb、ios_sim |
| Web | `panguverse/web/` | HTTP API、studio（任务/版本/审阅/发布/固化）、jobs、service |
| 前端 | `webui/src/` | React + Vite；`pages/`（概览/任务/画布/运行/资产/设置）|
| 文档 | `docs/` | 架构、API、用户故事、原型 |

## 4. 当前状态（实测）

- 服务：`127.0.0.1:7788`，`llm_enabled=true`（DeepSeek `deepseek-chat`）
- 原子 57 / 领域包 7 / 场景 6 / 经验 24 / 回归用例 29
- 任务 47（其中冻结 1：`w-e892e0de@0`）/ 运行 50
- 代码：Python 约 40.7k 行，前端 TS/TSX 约 17.6k 行；测试文件 35 个
- 最近提交：`0751b94`（验收整改前端）、`a2bb996`（后端）、`61976cb`、`901aba0`、`e6f3b00`

## 5. 已验收通过的能力

1. 一句话 → LLM 真拆解（`plan_source=llm`），生成 8–13 节点任务
2. 审阅 → 批准 → 发布（`wid@rev`）状态机
3. 运行证据：步骤、耗时、失败原因、产物目录、goal_checks
4. 固化后确定性：两次运行 step/atom 序列一致，`llm_used=false`
5. 页面与数据一致：概览 KPI 与 `/api/runs` 对账；任务列表 `node_count` 一致

## 6. 已知缺口（按优先级，接手后从 G1 开始）

| ID | 问题 | 位置 | 验收标准 |
| --- | --- | --- | --- |
| G1 | 冻结态只有前端守卫，API 仍可改图 | `panguverse/web/studio.py` 的节点/边写入函数 | `frozen` 下改图返回 409 `frozen_read_only`，只能先 `unfreeze` |
| G2 | `capabilities` 只声明不拦截 | `core/atom.py` + `axe` 执行路径 | 未授权能力在调用前拒绝，记录审计 |
| G3 | 能力缺口不显式（Bing 复用百度原子→0 条） | `web/planner_gate.py`、执行错误分类 | 跨站原子→`capability_gap`，不得静默产空结果 |
| G4 | 无基线 diff / 用例集批量 | `web/service.py`、前端运行详情 | 报告能指出「哪一步、期望 vs 实际、证据」 |
| G5 | 产物登记 warning（文件在、日志说缺失） | 产物登记路径解析 | 不再出现 `artifact_source_missing` |
| G6 | LLM 规划不稳定（同句意 8/9 节点） | `axe/planner.py` | 固定 seed/温度或回归用例稳定 |

## 7. 工作方式约定

- 一次只做一件事；改完必须跑相关 pytest + `npm run build`（如动前端）。
- 提交信息用 `fix(...)` / `feat(...)`，按层拆分（后端/前端分开）。
- 不打印、不提交任何密钥；LLM 配置只放 `~/.panguverse-dev/llm.env`。
- 改动前端后必须核对公网 `app.js` SHA256 与本地产物一致。
- 运行/审阅/固化相关改动，必须补一条「先失败后成功」的回归用例。

## 8. 常用验收命令

```bash
# 状态与原子
curl -s -c /tmp/pv.jar -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<PW>"}' http://127.0.0.1:7788/api/auth/login
curl -s -b /tmp/pv.jar http://127.0.0.1:7788/api/status

# 数据边（回归 G1 相关）
curl -s -b /tmp/pv.jar -H 'Content-Type: application/json' \
  -d '{"from":"open_bing","to":"log_done","kind":"data","via":"open_bing"}' \
  http://127.0.0.1:7788/api/studio/<wid>/edges

# 运行任务
curl -s -b /tmp/pv.jar -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:7788/api/tasks/<task_id>/run
```

## 9. 第一步建议（给 opencode）

先做 **G1**：在 `studio.py` 里给所有图变更入口加冻结守卫，补 3 个测试（改节点/删节点/加边在 frozen 下均 409），再跑 `tests/test_web_studio.py` 与 `tests/test_web_execution_mode.py`。
不要顺手改 G2/G3，避免一次提交混多个语义。
