"""原子契约：**领域中立的**原子语义定义。

设计原则（这是平台通用能力的根）：

1. **内核不知道任何领域**。内核只认识「输入/输出、副作用、能力、失败类别、恢复策略、经验标识」
   这些抽象概念；HTTP 状态码、SQL 错误、文件系统 errno 全部属于**领域包**。
2. **一切以声明为准**。原子的 I/O 类型、副作用、幂等性、需要的运行时能力、输出的密钥标记，
   都由原子自己声明，而不是由引擎按名字猜。
3. **失败有两层词汇**：
   - :class:`FailureClass` —— 中立类别（跨领域通用，内核用来决策：能不能重试、要不要重新认证）；
   - ``kind`` —— 领域标签（``server_error`` / ``file_not_found`` / ``rate_limit``…），
     由领域包定义，用于报告、经验库与修复建议。

这样，新增一个行业（文件、数据库、消息队列、财务、RPA…）只需要写一个领域包，
**不需要改内核一行代码**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import StepError

# --------------------------------------------------------------------------- #
# 类型
# --------------------------------------------------------------------------- #
TYPE_NAMES = ("string", "integer", "number", "boolean", "object", "array", "null", "any")


def type_ok(value: Any, expected: str) -> bool:
    """按声明类型校验值（``any`` 永远通过）。"""
    expected = (expected or "any").strip().lower()
    if expected in {"any", ""}:
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "null":
        return value is None
    return True  # 未知类型名 → 不做限制，避免误伤自定义类型


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


@dataclass(frozen=True)
class Port:
    """原子的一个输入/输出端口。"""

    name: str
    type: str = "any"
    required: bool = True
    secret: bool = False
    description: str = ""

    def accepts(self, value: Any) -> bool:
        return type_ok(value, self.type)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.secret:
            data["secret"] = True
        if self.description:
            data["description"] = self.description
        return data


def ports_from(
    spec: Any,
    *,
    required: Iterable[str] | None = None,
    secret: Iterable[str] = (),
) -> tuple[Port, ...]:
    """把 ``{"name": "type"}`` / ``{name: {type/required/secret}}`` 解析成端口元组。"""
    if not spec:
        return ()
    required_set = set(required) if required is not None else None
    secret_set = set(secret)
    out: list[Port] = []
    items = spec.items() if isinstance(spec, Mapping) else [(str(n), "any") for n in spec]
    for name, value in items:
        name = str(name)
        if isinstance(value, Mapping):
            out.append(
                Port(
                    name=name,
                    type=str(value.get("type", "any")),
                    required=bool(value.get("required", True)),
                    secret=bool(value.get("secret", False)) or name in secret_set,
                    description=str(value.get("description", "")),
                )
            )
            continue
        text = str(value or "any")
        out.append(
            Port(
                name=name,
                type=text,
                required=(name in required_set) if required_set is not None else not name.endswith("?"),
                secret=name in secret_set,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# 副作用：决定「能不能安全地重试 / 并发 / 回滚」
# --------------------------------------------------------------------------- #
class Effect(str, Enum):
    """副作用的强度（由弱到强）。"""

    PURE = "pure"            # 纯计算/断言：无外部影响
    READ = "read"            # 只读外部（GET、读文件、SELECT）
    WRITE = "write"          # 幂等写（PUT/SET、覆盖写文件）
    MUTATE = "mutate"        # 非幂等写（POST 创建、扣款、发消息）
    DESTRUCTIVE = "destructive"  # 删除/不可逆

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.value


_SAFE_TO_RETRY = {Effect.PURE, Effect.READ, Effect.WRITE}


def retry_safe(effects: Iterable[Effect]) -> bool:
    """副作用全部幂等/只读 ⇒ 重试天然安全。"""
    items = set(effects)
    return bool(items) and items <= _SAFE_TO_RETRY


# --------------------------------------------------------------------------- #
# 意图模板：领域包把「自然语言 → 参数化计划」的知识也交给包
# --------------------------------------------------------------------------- #
@dataclass
class RuleTemplate:
    """一条意图模板：关键词命中后产出参数化的用例规格。

    模板由**领域包**声明（``AtomPack.intent_templates``）；内核的规则规划器只负责
    收集、打分、排序——所以给新行业加模板不需要改内核。
    """

    name: str
    description: str
    keywords: tuple[str, ...]
    builder: Any  # Callable[[], Mapping[str, Any]]
    tags: tuple[str, ...] = ()
    weight: int = 1
    domain: str = "generic"
    pack: str = ""
    #: 意图为空 / 没命中关键词时是否作为兜底模板
    default: bool = False

    def score(self, text: str) -> int:
        lowered = (text or "").lower()
        return sum(self.weight for keyword in self.keywords if keyword.lower() in lowered)

    def build(self) -> dict[str, Any]:
        spec = dict(self.builder())
        spec.setdefault("tags", [])
        spec["tags"] = sorted({*spec["tags"], f"tpl:{self.name}"})
        meta = spec.setdefault("meta", {})
        meta["template"] = self.name
        meta["template_description"] = self.description
        meta["template_pack"] = self.pack or self.domain
        return spec

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "tags": list(self.tags),
            "weight": self.weight,
            "domain": self.domain,
            "pack": self.pack,
            "default": self.default,
        }


# --------------------------------------------------------------------------- #
# 中立失败类别：内核据此决策
# --------------------------------------------------------------------------- #
class FailureClass(str, Enum):
    """跨领域通用的失败类别（领域包负责把自己的信号映射到这里）。"""

    TRANSIENT = "transient"              # 再试可能就好了（5xx、限流、连接抖动、锁等待）
    CREDENTIALS = "credentials"          # 凭证/会话问题（401、token 过期、登录态失效）
    PERMISSION = "permission"            # 有身份但无权限（403、文件只读、越权）
    MISSING_RESOURCE = "missing_resource"  # 目标不存在（404、文件不存在、表不存在）
    CONFLICT = "conflict"                # 状态冲突（409、已支付、重复创建）
    INVALID_INPUT = "invalid_input"      # 参数/计划本身有问题（400、模板变量缺失、类型不符）
    EXPECTATION = "expectation"          # 断言不满足：结果与预期不符
    INTERNAL = "internal"                # 原子/执行体自身异常
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


#: 默认「值得重试」的中立类别（领域标签由包决定，见 :class:`AtomSemantics`）
DEFAULT_RETRY_ON = (FailureClass.TRANSIENT.value, "timeout", "network")


# --------------------------------------------------------------------------- #
# 恢复策略：领域包声明「这类失败可以怎么救」
# --------------------------------------------------------------------------- #
@dataclass
class Recovery:
    """一次可执行的恢复动作。"""

    action: str                     # reauth | retry | none
    reason: str = ""
    auth_step_id: str | None = None  # reauth 时：重跑哪个步骤
    delay_ms: int = 0
    retry_hint: dict[str, Any] | None = None
    advice: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 领域语义：内核 ↔ 领域包之间的唯一接口
# --------------------------------------------------------------------------- #
class AtomSemantics:
    """领域语义基类：**默认实现对任何领域都安全**（不猜测、不魔法修补）。

    领域包继承并覆写这些方法，把「自己领域的信号」翻译成内核能懂的东西：

    - :meth:`failure_class` / :meth:`failure_kind`：失败 → （中立类别, 领域标签）
    - :meth:`is_retryable`：这次失败值不值得重试
    - :meth:`target`：这个原子的「作用对象标识」（HTTP 是接口路径、文件是路径、SQL 是表名…），
      经验库用它把教训精确绑定到对象上，避免把 A 的毛病套到 B
    - :meth:`recoveries`：可执行的恢复策略（重新认证、退避重试…）
    - :meth:`advice`：给人看的排查建议
    - :meth:`sugar` / :meth:`defaults`：步骤语法糖与默认值（例如 HTTP 的 ``assert:`` 与默认 headers）
    """

    name: str = "generic"

    # -- 失败语义 -------------------------------------------------------- #
    def failure_class(self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None) -> FailureClass:
        return neutral_failure_class(exc)

    def failure_kind(self, atom: Any, outputs: Mapping[str, Any] | None, exc: BaseException | None) -> str:
        return neutral_failure_kind(exc)

    def is_retryable(
        self,
        atom: Any,
        outputs: Mapping[str, Any] | None,
        *,
        kind: str,
        failure_class: FailureClass,
    ) -> bool:
        """默认：只有中立的瞬态类别值得重试。"""
        return failure_class is FailureClass.TRANSIENT

    def result_status(
        self, atom: Any, outputs: Mapping[str, Any] | None
    ) -> tuple[str, FailureClass] | None:
        """**成功返回**的结果里是否已经藏着失败（例如 HTTP 5xx/429）？

        返回 ``None`` 表示「这个领域没有这种概念」——绝大多数领域都是这样：
        出错就抛异常，正常返回就是正常。
        返回 ``(领域标签, 中立类别)`` 时，引擎会在配置了重试策略的情况下重试它，
        但**不会**把它直接判定为失败（4xx 依然可以是被断言的合法结果）。
        """
        return None

    # -- 标识 / 恢复 / 建议 ---------------------------------------------- #
    def target(self, atom: Any, inputs: Mapping[str, Any] | None) -> str:
        return ""

    def recoveries(
        self,
        atom: Any,
        *,
        kind: str,
        failure_class: FailureClass,
        case: Any = None,
        step: Any = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> list[Recovery]:
        return []

    def advice(self, *, kind: str, failure_class: FailureClass) -> str:
        return NEUTRAL_ADVICE.get(failure_class, NEUTRAL_ADVICE[FailureClass.UNKNOWN])

    def planning_hints(self) -> tuple[str, ...]:
        """给 LLM 规划器的**领域提示词片段**（怎么用本领域的原子才地道）。

        这样规划提示词不需要在内核里硬编码任何领域惯例：新领域包加几条 hint 即可。
        """
        return ()

    # -- 计划层的语法糖（领域专属写法） ---------------------------------- #
    def sugar(self, step_id: str, step: Mapping[str, Any], *, atom: Any = None) -> list[dict[str, Any]]:
        """把领域专属的简写展开成标准步骤（默认：不认任何糖）。"""
        return []

    def step_defaults(self, step: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """声明「套件级 defaults 里哪些键适用于本领域」（默认：什么都不补）。"""
        return {}

    def sugar_keys(self) -> set[str]:
        return set()

    def known_input_roots(self, atom: Any) -> set[str]:
        """``in:`` 这类相对根的合法取值（用于静态校验）。"""
        return set()

    # -- 元信息 ---------------------------------------------------------- #
    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "failure_classes": [c.value for c in FailureClass]}


NEUTRAL_SEMANTICS = AtomSemantics()


# --------------------------------------------------------------------------- #
# 中立分类：只依赖异常类型与中立概念，不依赖任何领域
# --------------------------------------------------------------------------- #
def neutral_failure_class(exc: BaseException | None) -> FailureClass:
    from .errors import AtomFailure, PanguError, PlanError, RequestTimeout, StepError, StepFailure, TemplateError

    if exc is None:
        return FailureClass.UNKNOWN
    if isinstance(exc, AtomFailure):
        try:
            return FailureClass(exc.failure_class)
        except ValueError:
            return FailureClass.INTERNAL
    if isinstance(exc, (RequestTimeout, TimeoutError)):
        return FailureClass.TRANSIENT
    if isinstance(exc, (TemplateError, PlanError)):
        return FailureClass.INVALID_INPUT
    if isinstance(exc, StepError):
        return FailureClass.INTERNAL
    if isinstance(exc, StepFailure):
        return FailureClass.EXPECTATION
    if isinstance(exc, (ConnectionError, OSError)):
        return FailureClass.TRANSIENT
    if isinstance(exc, PanguError):
        # 中立兜底：按异常自报的 kind 归类（不涉及任何具体领域）
        return _NEUTRAL_KIND_CLASS.get(exc.kind, FailureClass.INTERNAL)
    return FailureClass.UNKNOWN


def neutral_failure_kind(exc: BaseException | None) -> str:
    from .errors import AtomFailure, PanguError, PlanError, RequestTimeout, StepError, StepFailure, TemplateError

    if exc is None:
        return "unknown"
    if isinstance(exc, AtomFailure):
        return exc.kind
    if isinstance(exc, RequestTimeout):
        return "timeout"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, TemplateError):
        return "template"
    if isinstance(exc, PlanError):
        return "plan"
    if isinstance(exc, StepError):
        return "step_error"
    if isinstance(exc, StepFailure):
        return "assertion"
    if isinstance(exc, ConnectionError):
        return "network"
    if isinstance(exc, OSError):
        return "io_error"
    if isinstance(exc, PanguError):
        return exc.kind
    return "atom_error"


_NEUTRAL_KIND_CLASS: dict[str, FailureClass] = {
    "network": FailureClass.TRANSIENT,
    "timeout": FailureClass.TRANSIENT,
    "plan": FailureClass.INVALID_INPUT,
    "template": FailureClass.INVALID_INPUT,
    "spec": FailureClass.INVALID_INPUT,
    "atom_not_found": FailureClass.INVALID_INPUT,
    "auth": FailureClass.CREDENTIALS,
    "assertion": FailureClass.EXPECTATION,
}


NEUTRAL_ADVICE: dict[FailureClass, str] = {
    FailureClass.TRANSIENT: "瞬态故障：退避重试或稍后再跑；若持续复现，检查下游依赖的稳定性",
    FailureClass.CREDENTIALS: "凭证失效：重新获取凭证/会话后再执行",
    FailureClass.PERMISSION: "权限不足：确认执行身份与授权范围",
    FailureClass.MISSING_RESOURCE: "目标不存在：确认标识来源，或在前置步骤先创建它",
    FailureClass.CONFLICT: "状态冲突：流程可能被重复执行，请改为幂等写法或先重置状态",
    FailureClass.INVALID_INPUT: "输入/计划有误：检查必填项、类型与模板引用",
    FailureClass.EXPECTATION: "结果与预期不符：核对期望值的来源；若为变更，请同步更新断言",
    FailureClass.INTERNAL: "原子内部异常：查看输入、返回值与执行轨迹",
    FailureClass.UNKNOWN: "查看报告中的现场信息后定位",
}


# --------------------------------------------------------------------------- #
# 校验辅助：把「能力/密钥/副作用」这类声明检查统一在这里
# --------------------------------------------------------------------------- #
def ensure_declared_types(atom: Any, values: Mapping[str, Any]) -> list[str]:
    """按端口声明检查值类型，返回问题列表（不抛异常，供静态校验使用）。"""
    problems: list[str] = []
    for port in atom.input_ports():
        if port.name not in values:
            continue
        value = values[port.name]
        if value is None:
            continue
        if not port.accepts(value):
            problems.append(f"{atom.name}.{port.name}: 期望 {port.type}，实际 {type_name(value)}")
    return problems


def require_capability(capability: str, granted: Iterable[str]) -> None:
    """执行期硬校验：原子声明需要的能力必须被授权。"""
    if capability and capability not in set(granted):
        raise StepError(f"缺少运行时能力授权: {capability}")
