# 更新日志 / Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] — 2026-10-29

首个公开版本：AI 编排内核 + 可调用原子 + 领域包体系。

### 新增

- **原子契约**：类型化 `inputs/outputs`、`effects`（副作用）、`capabilities`（能力）、`secret_outputs`（密钥）的显式声明
- **领域包体系**：`core` / `api_testing` / `local_fs` 三个包；新增行业只需实现 `AtomSemantics`，不改内核
- **声明式原子**：`*.pack.yaml` 即可贡献原子，无需编写 Python
- **原子递归组合**：分子可 `seal()` 成为新原子，支持无限层级复用
- **静态计划校验**：执行前检测非法引用、类型不匹配、跨领域误用
- **自愈执行**：分类重试、重新认证、经验沉淀与下次规划复用；非幂等动作不自动重试
- **治理能力**：能力授权、文件系统沙箱、密钥按值打码、报告脱敏
- **报告与回归**：结构化执行报告、失败归类、可回归复用的用例资产
- **CLI**：`demo e2e` / `demo api` / `atoms` / `packs` / `plan` 等命令
- **多 AI 后端**：OpenAI / Anthropic 兼容接口与本地 LLM；无密钥时由领域包意图模板兜底
- **架构守护测试**：内核出现领域词汇即失败，保证长期领域中立

### 变更

- 内核领域中立化：HTTP 相关语义从内核迁入 `packs/api_testing.py`，并新增 `local_fs` 作为非 API 领域对照实现
- 首次业务闭环锁定为 API 自动化测试，内置 mock 被测量，克隆即可离线跑通

### 安全

- 修复密钥打码遗漏：自愈触发的重新认证不再让首个 token 明文进入报告

### 文档

- 新增 `AGENTS.md`：确立「万物原子化、递归组合」为开发指导思想，原子必须是固定签名的可调用执行体，LLM 只做意图识别与编排
- 新增 `docs/platform-architecture.md`：内核与领域包的边界、契约与扩展步骤
- 新增 `docs/api-testing.md`：首个业务闭环的完整说明
- 新增 `SECURITY.md` / `CODE_OF_CONDUCT.md` / Issue 与 PR 模板

### 已知限制

- 尚无包注册中心（`v0.2` 路线），领域包依靠本地路径或显式加载
- 无可视化编排面板（`v0.3` 路线）
- 并发 / DAG / 补偿回滚尚未实现（`v0.4` 路线）
- 文档以中文为主，英文文档在补齐中
