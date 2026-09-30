"""规则模板：把自然语言意图映射成可执行的测试计划（离线可用的「盘古斧」）。

LLM 不可用（或用户不想用 AI）时，闭环不能断——这里是确定性的兜底：
关键词 → 领域模板 → 参数化用例。模板针对 **REST 业务 API 的通用形状**：
健康检查、认证链路、业务状态机、CRUD、负向鉴权、抖动自愈。

注：模板产出的接口路径默认对齐本项目内置的 mock API；接自己的系统时，
用 ``--env`` 覆盖变量或直接写场景 YAML（见 docs/api-testing.md）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Template:
    name: str
    description: str
    keywords: tuple[str, ...]
    builder: Callable[[], dict[str, Any]]
    tags: tuple[str, ...] = ()
    weight: int = 1

    def score(self, text: str) -> int:
        lowered = text.lower()
        return sum(self.weight for keyword in self.keywords if keyword.lower() in lowered)


# --------------------------------------------------------------------------- #
# 模板定义
# --------------------------------------------------------------------------- #
def _login_step(step_id: str = "login") -> dict[str, Any]:
    return {
        "id": step_id,
        "use": "http_request",
        "name": "登录获取 token",
        "tags": ["auth"],
        "with": {
            "method": "POST",
            "url": "${env.BASE_URL}/api/login",
            "json": {
                "username": "${env.API_USERNAME|default:demo}",
                "password": "${env.API_PASSWORD|default:demo123}",
            },
            "expect_status": 200,
        },
        "capture": {"token": "body.token", "login_user": "body.user.name"},
    }


def build_health_check() -> dict[str, Any]:
    return {
        "id": "health_check",
        "name": "健康检查：服务存活且响应够快",
        "tags": ["smoke", "health", "rules"],
        "description": "GET /api/health，断言 200 / status=ok / 耗时上限",
        "steps": [
            {
                "id": "health",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/health",
                    "expect_status": 200,
                },
            },
            {"id": "assert_status", "use": "assert_json", "with": {"path": "status", "equals": "ok"}},
            {"id": "assert_service", "use": "assert_json", "with": {"path": "service", "exists": True}},
            {"id": "assert_latency", "use": "assert_latency", "with": {"max_ms": "${env.MAX_LATENCY_MS|default:2000|int}"}},
        ],
    }


def build_login_flow() -> dict[str, Any]:
    return {
        "id": "login_flow",
        "name": "认证闭环：登录拿 token，再带 token 访问 /api/me",
        "tags": ["smoke", "auth", "rules"],
        "secrets": ["token"],
        "steps": [
            _login_step(),
            {"id": "assert_token", "use": "assert_json", "with": {"path": "token", "exists": True}},
            {
                "id": "me",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/me",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {
                "id": "assert_me",
                "use": "assert_json",
                "with": {"path": "data.name", "equals": "${login_user}"},
            },
        ],
    }


def build_order_flow() -> dict[str, Any]:
    return {
        "id": "order_flow",
        "name": "订单业务闭环：登录 → 下单 → 支付 → 回查状态机",
        "tags": ["order", "rules"],
        "secrets": ["token"],
        "steps": [
            _login_step(),
            {
                "id": "create_order",
                "use": "http_request",
                "with": {
                    "method": "POST",
                    "url": "${env.BASE_URL}/api/orders",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "json": {
                        "sku": "${env.ORDER_SKU|default:SKU-001}",
                        "quantity": "${env.ORDER_QTY|default:2|int}",
                        "unit_price": 19.9,
                    },
                    "expect_status": 201,
                },
                "capture": {"order_id": "body.data.id", "order_amount": "body.data.amount"},
            },
            {"id": "assert_created", "use": "assert_json", "with": {"path": "data.status", "equals": "CREATED"}},
            {"id": "assert_amount", "use": "assert_json", "with": {"path": "data.amount", "gt": 0}},
            {
                "id": "get_order",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/orders/${order_id}",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {"id": "assert_same_order", "use": "assert_json", "with": {"path": "data.id", "equals": "${order_id}"}},
            {
                "id": "pay_order",
                "use": "http_request",
                "retry": {"attempts": 3, "delay_ms": 100, "on": ["server_error", "timeout", "network"]},
                "with": {
                    "method": "POST",
                    "url": "${env.BASE_URL}/api/orders/${order_id}/pay",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {"id": "assert_paid", "use": "assert_json", "with": {"path": "data.status", "equals": "PAID"}},
            {"id": "assert_paid_at", "use": "assert_json", "with": {"path": "data.paid_at", "exists": True}},
            {
                "id": "list_paid",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/orders",
                    "params": {"status": "PAID"},
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {"id": "assert_list", "use": "assert_json", "with": {"path": "total", "gte": 1}},
            {"id": "assert_schema", "use": "assert_schema", "with": {"schema": {"type": "object", "required": ["total", "items"], "properties": {"items": {"type": "array"}}}}},
        ],
    }


def build_user_crud() -> dict[str, Any]:
    return {
        "id": "user_crud_flow",
        "name": "用户 CRUD 闭环：创建 → 查询 → 删除 → 再查应 404",
        "tags": ["crud", "rules"],
        "secrets": ["token"],
        "steps": [
            _login_step(),
            {
                "id": "create_user",
                "use": "http_request",
                "with": {
                    "method": "POST",
                    "url": "${env.BASE_URL}/api/users",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "json": {
                        "name": "${env.NEW_USER_NAME|default:pangu_tester}",
                        "email": "pangu_tester@panguverse.dev",
                    },
                    "expect_status": 201,
                },
                "capture": {"user_id": "body.data.id", "user_name": "body.data.name"},
            },
            {
                "id": "get_user",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/users/${user_id}",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {"id": "assert_user", "use": "assert_json", "with": {"path": "data.name", "equals": "${user_name}"}},
            {
                "id": "delete_user",
                "use": "http_request",
                "with": {
                    "method": "DELETE",
                    "url": "${env.BASE_URL}/api/users/${user_id}",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 204,
                },
            },
            {
                "id": "get_deleted",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/users/${user_id}",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 404,
                },
            },
            {"id": "assert_not_found", "use": "assert_json", "with": {"path": "error", "equals": "not_found"}},
        ],
    }


def build_auth_negative() -> dict[str, Any]:
    return {
        "id": "auth_negative",
        "name": "负向鉴权：无 token / 错密码都应被拒绝",
        "tags": ["negative", "auth", "rules"],
        "steps": [
            {
                "id": "no_token",
                "use": "http_request",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/users",
                    "expect_status": 401,
                },
            },
            {"id": "assert_unauthorized", "use": "assert_json", "with": {"path": "error", "equals": "unauthorized"}},
            {
                "id": "bad_login",
                "use": "http_request",
                "with": {
                    "method": "POST",
                    "url": "${env.BASE_URL}/api/login",
                    "json": {"username": "demo", "password": "definitely-wrong"},
                    "expect_status": 401,
                },
            },
            {
                "id": "assert_invalid_credentials",
                "use": "assert_json",
                "with": {"path": "error", "equals": "invalid_credentials"},
            },
        ],
    }


def build_flaky_retry() -> dict[str, Any]:
    return {
        "id": "flaky_self_heal",
        "name": "自愈（重试层）：服务端抖动后自动重试恢复",
        "tags": ["selfheal", "rules"],
        "description": "对注入 5xx 的接口做退避重试，验证步骤级自愈",
        "steps": [
            {
                "id": "flaky",
                "use": "http_request",
                "retry": {"attempts": 4, "delay_ms": 50, "backoff": 1.0, "on": ["server_error", "timeout", "network"]},
                "with": {"method": "GET", "url": "${env.BASE_URL}/api/flaky"},
                "capture": {"flaky_attempt": "body.attempt"},
            },
            {"id": "assert_ok", "use": "assert_status", "with": {"expected": 200}},
            {"id": "assert_body", "use": "assert_json", "with": {"path": "status", "equals": "ok"}},
        ],
    }


def build_auth_self_heal() -> dict[str, Any]:
    return {
        "id": "auth_self_heal",
        "name": "自愈（重认证层）：token 失效后自动重新登录并重试",
        "tags": ["selfheal", "auth", "rules"],
        "secrets": ["token"],
        "description": "先让 token 失效，再用带 token 的接口触发 401，验证自愈器重新认证",
        "steps": [
            _login_step(),
            {
                "id": "invalidate_token",
                "use": "http_request",
                "name": "让服务端 token 全部失效",
                "with": {
                    "method": "POST",
                    "url": "${env.BASE_URL}/api/debug/invalidate-tokens",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {
                "id": "me_with_stale_token",
                "use": "http_request",
                "name": "旧 token 访问应 401（触发自愈）",
                "with": {
                    "method": "GET",
                    "url": "${env.BASE_URL}/api/me",
                    "headers": {"Authorization": "Bearer ${token}"},
                    "expect_status": 200,
                },
            },
            {"id": "assert_me_ok", "use": "assert_json", "with": {"path": "data.name", "equals": "${login_user}"}},
        ],
    }


TEMPLATES: dict[str, Template] = {
    "health_check": Template(
        name="health_check",
        description="服务健康检查",
        keywords=("健康", "health", "探活", "存活", "ping", "alive", "可用性"),
        builder=build_health_check,
        tags=("smoke",),
    ),
    "login_flow": Template(
        name="login_flow",
        description="登录认证链路",
        keywords=("登录", "login", "认证", "signin", "sign in", "token", "鉴权成功"),
        builder=build_login_flow,
        tags=("auth",),
    ),
    "order_flow": Template(
        name="order_flow",
        description="订单业务闭环（状态机）",
        keywords=("订单", "下单", "order", "支付", "pay", "交易", "业务闭环", "状态机"),
        builder=build_order_flow,
        tags=("order",),
        weight=2,
    ),
    "user_crud": Template(
        name="user_crud",
        description="用户 CRUD 闭环",
        keywords=("用户", "user", "crud", "增删改查", "注册", "删除"),
        builder=build_user_crud,
        tags=("crud",),
    ),
    "auth_negative": Template(
        name="auth_negative",
        description="负向鉴权用例",
        keywords=("401", "未授权", "unauthorized", "负向", "异常", "拒绝", "权限"),
        builder=build_auth_negative,
        tags=("negative",),
    ),
    "auth_self_heal": Template(
        name="auth_self_heal",
        description="token 失效自愈",
        keywords=("token 过期", "失效", "重新登录", "会话", "session", "401 自愈"),
        builder=build_auth_self_heal,
        tags=("selfheal",),
        weight=2,
    ),
    "flaky_retry": Template(
        name="flaky_retry",
        description="服务抖动重试自愈",
        keywords=("重试", "retry", "自愈", "抖动", "flaky", "容错", "5xx", "故障注入"),
        builder=build_flaky_retry,
        tags=("selfheal",),
        weight=2,
    ),
}

DEFAULT_TEMPLATES = ("health_check", "login_flow", "order_flow")


def match_templates(intent: str, *, max_cases: int = 3, defaults: tuple[str, ...] = DEFAULT_TEMPLATES) -> tuple[list[str], list[str]]:
    """返回 ``(选中的模板名, 备注)``。"""
    text = (intent or "").strip()
    notes: list[str] = []
    if not text:
        notes.append("意图为空 → 使用默认冒烟模板组")
        return list(defaults[:max_cases]), notes

    scored = [(template.score(text), template) for template in TEMPLATES.values()]
    hits = [(score, template) for score, template in scored if score > 0]
    if not hits:
        notes.append("没有命中任何关键词 → 使用默认冒烟模板组；可改用场景 YAML 精确描述")
        return list(defaults[:max_cases]), notes

    hits.sort(key=lambda item: (-item[0], item[1].name))
    chosen = [template.name for _score, template in hits[:max_cases]]
    notes.append(f"命中的模板: {', '.join(chosen)}")
    if len(hits) > len(chosen):
        notes.append(f"其余候选未纳入（max_cases={max_cases}）: {', '.join(t.name for _s, t in hits[len(chosen):])}")
    return chosen, notes


def build_cases(template_names: list[str]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for name in template_names:
        template = TEMPLATES.get(name)
        if template is None:
            continue
        spec = template.builder()
        spec.setdefault("tags", [])
        spec["tags"] = sorted({*spec["tags"], f"tpl:{template.name}"})
        spec.setdefault("meta", {})["template"] = template.name
        spec["meta"]["template_description"] = template.description
        cases.append(spec)
    return cases
