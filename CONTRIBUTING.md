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
   - 放在 `panguverse/atoms/` 下，用 `@atom(...)` 装饰器声明 `inputs/outputs/required/tags`；
   - 在 `panguverse/atoms/__init__.py` 里注册；
   - 补单测（`tests/test_engine.py`）并在 `docs/api-testing.md` 的原子表里登记。
2. **领域场景模板**：在 `scenarios/` 加 YAML，或给 `axe/templates.py` 加规则模板（无 LLM 时的兜底规划）。
3. **规划/自愈策略**：`axe/planner.py`、`axe/healer.py`——请保持「可解释、可审计、有边界」。
4. **报告与可视化**：`report/`。
5. **文档**：`docs/`、README。

## 硬性约定（评审会看）

- **区分 `failed` 与 `error`**：断言不符是 `failed`，环境/原子异常是 `error`。别把两者混为一谈。
- **不吞异常、不静默降级**：降级必须写进 `plan.warnings` / `notes` / 报告，让人看得见。
- **自愈有边界**：只对可判定的瞬态/凭证问题动手，动作次数有预算，全部落报告。
- **密钥不入产物**：新增任何输出前，确认经过 `core/redact.py`；报告相关改动请补一个「明文不出现」的测试。
- **零依赖承诺**：核心链路在只有标准库的环境下必须可用（`HttpClient` 的 urllib 回退就是例子）。
- 新增行为请配套测试；提交前 `python -m pytest -q` 必须全绿。

## 提交

- 提交信息用中文或英文皆可，格式建议 `类型(范围): 说明`，例如 `feat(atoms): 新增 assert_db_row 原子`。
- 一个 PR 只做一件事；行为变更请在描述里写清「为什么」和「怎么验证」。

## 协议

贡献即表示同意以 Apache 2.0 协议发布。
