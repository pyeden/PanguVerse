# 贡献指南 / Contributing

盘古斧的力量来自社区的每一个原子。欢迎贡献。

## 开发环境

```bash
git clone https://github.com/pyeden/PanguVerse.git
cd PanguVerse

# 核心零依赖，直接就能跑
python -m panguverse demo e2e

# 开发/测试依赖
pip install -r requirements.txt
python -m pytest -q
```

要求：Python 3.10+；**核心代码不得引入第三方运行时依赖**（可选增强必须能优雅降级）。

## 怎么跑、怎么测

```bash
python -m pytest -q                              # 全部单测 + 端到端闭环验收
python -m pytest tests/test_e2e_loop.py -q       # 只跑闭环验收
python -m panguverse demo e2e                    # 一键闭环（含报告与回归复用）
python -m panguverse demo api --port 8899        # 只起内置 mock 被测量
python -m panguverse atoms                       # 看当前原子库
```

## 贡献方向

1. **定义新的原子**（场景不限：运维、开发、财务、生活……万物皆可原子化）
   - 简单场景用 **YAML 声明式包**即可（`*.pack.yaml`，无需 Python），见 `examples/packs/ops_kit.pack.yaml`；
   - 需要真实 I/O 时用 `@atom(...)` 写 Python 原子，并声明契约：
     `inputs/outputs`（类型）、`effects`（副作用）、`capabilities`（能力）、`secret_outputs`（密钥）；
   - 登记到对应的**领域包**（`panguverse/packs/`），补单测并在 `docs/platform-architecture.md` 的表格里登记。
2. **新增一个行业领域包**：实现 `AtomSemantics` 的钩子（失败类别/重试/恢复/建议/作用对象/语法糖/规划提示），
   组装 `AtomPack` 即可——**不需要改内核**（有架构守护测试保证）。
3. **领域场景模板**：给领域包加 `intent_templates`（无 LLM 时的自然语言兜底规划），或直接在 `scenarios/` 加 YAML。
4. **规划/自愈策略**：`axe/planner.py`、`axe/healer.py`——请保持「可解释、可审计、有边界」。
5. **报告与可视化**：`report/`。
6. **文档**：`docs/`、README。

## 硬性约定（评审会看）

- **内核领域中立**：`engine/` `axe/` `core/` `spec/loader.py` 里不得出现任何具体领域的词汇
  （`status_code`/`Bearer`/`server_error`/`BASE_URL`…）——领域知识必须放进 `panguverse/packs/*`。
  这条由 `tests/test_packs.py::TestKernelStaysDomainNeutral` 自动拦截。
- **新领域只加包**：需要新协议/新语义时，实现 `AtomSemantics` 钩子并组装 `AtomPack`，不要改内核。
- **契约要完整**：新原子必须声明类型、副作用（`effects`）、能力（`capabilities`）；
  涉及凭据的还要声明 `secret_outputs`——否则重试安全、能力授权与打码都会失效。
- **区分 `failed` 与 `error`**：断言不符是 `failed`，环境/原子异常是 `error`。别把两者混为一谈。
- **不吞异常、不静默降级**：降级必须写进 `plan.warnings` / `notes` / 报告，让人看得见。
- **自愈有边界**：只对可判定的瞬态/凭证问题动手；非幂等副作用不自动重试；动作次数有预算且全部落报告。
- **密钥不入产物**：新增任何输出前，确认经过 `core/redact.py`；报告相关改动请补一个「明文不出现」的测试。
- **零依赖承诺**：核心链路在只有标准库的环境下必须可用（`HttpClient` 的 urllib 回退、内置 YAML 子集就是例子）。
- 新增行为请配套测试；提交前 `python -m pytest` 与 `python -m ruff check panguverse tests` 必须全绿。

## 提交

- 提交信息用中文或英文皆可，格式建议 `类型(范围): 说明`，例如 `feat(atoms): 新增 assert_db_row 原子`。
- 一个 PR 只做一件事；行为变更请在描述里写清「为什么」和「怎么验证」。

## 协议

贡献即表示同意以 Apache 2.0 协议发布。
