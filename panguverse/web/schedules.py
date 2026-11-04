"""调度与触发：cron / interval / webhook（契约 v8.0 §1）。

设计约束（全部来自 ``tasks/schedule-artifacts-spec.md``）：

- **不引入第三方 cron 库**：这里自己解析 5 字段（``分 时 日 月 周``），
  支持 ``*`` / ``*/n`` / ``a-b`` / ``a,b``（额外兼容 ``a-b/n``）；
- 时区默认 ``Asia/Shanghai``，``next_run_at`` 是 epoch 秒；
- 单进程 MVP：调度线程随 ``serve`` 启停，**不排队、不堆叠**；
- **禁止重叠**：同一 sid 的上一次 batch 仍在 ``queued/running`` 时，
  本次触发只记录 ``skipped_overlap`` 事件，不产生新 batch；
- **重启不补跑**：启动时从磁盘恢复并按当前时间重算 ``next_run_at``，
  已错过的时点只写 ``missed`` 事件。

落盘：``<workspace>/.panguverse/schedules/<sid>.json``（目录 0700、文件 0600、原子写），
事件追加到 ``<workspace>/.panguverse/schedules/events.jsonl``。

本模块不 import ``service``（避免循环依赖）：真正触发批量运行时在函数内部
局部导入 :mod:`panguverse.web.service`。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import projects

LOGGER = logging.getLogger(__name__)

STATE_DIRNAME = ".panguverse"
SCHEDULES_DIRNAME = "schedules"
EVENTS_FILENAME = "events.jsonl"
DIR_MODE = 0o700
FILE_MODE = 0o600
DEFAULT_TZ = "Asia/Shanghai"
SID_RE = re.compile(r"^sc-[0-9a-f]{6,12}$")
MIN_INTERVAL_MINUTES = 1
#: 超过这个延迟视为「错过」：记录 missed 且不补跑（重启后的恢复也走同一判定）
MISSED_GRACE_SECONDS = 120.0
#: cron 向前搜索的最大范围（5 年），覆盖 2 月 29 日这种稀疏表达式
CRON_SEARCH_MINUTES = 366 * 24 * 60 * 5
BATCH_ACTIVE_STATUSES = {"queued", "running"}

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "internal_error": 500,
}

_LOCK = threading.RLock()


class ScheduleError(Exception):
    """带错误码的调度异常（由 ``api.py`` 统一渲染）。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _STATUS_BY_CODE.get(code, 500)


# --------------------------------------------------------------------------- #
# cron：5 字段解析与下一次计算
# --------------------------------------------------------------------------- #
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")


def _parse_number(text: str, low: int, high: int, field: str) -> int:
    if not text.isdigit():
        raise ScheduleError("bad_request", f"cron {field} 字段非法: {text!r}")
    value = int(text)
    if value < low or value > high:
        raise ScheduleError(
            "bad_request", f"cron {field} 超出范围 {low}-{high}: {value}"
        )
    return value


def _parse_field(text: str, low: int, high: int, field: str) -> set[int]:
    """解析单个字段：``*`` / ``*/n`` / ``a-b`` / ``a,b`` / ``a-b/n``。"""
    values: set[int] = set()
    for chunk in str(text).split(","):
        token = chunk.strip()
        if not token:
            raise ScheduleError("bad_request", f"cron {field} 字段含空项")
        if token == "*":
            values.update(range(low, high + 1))
            continue
        step = 1
        body = token
        if "/" in token:
            body, _, raw_step = token.partition("/")
            if not raw_step.isdigit() or int(raw_step) <= 0:
                raise ScheduleError("bad_request", f"cron {field} 步长非法: {token!r}")
            step = int(raw_step)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            raw_start, _, raw_end = body.partition("-")
            start = _parse_number(raw_start, low, high, field)
            end = _parse_number(raw_end, low, high, field)
            if start > end:
                raise ScheduleError(
                    "bad_request", f"cron {field} 区间起点大于终点: {token!r}"
                )
        else:
            start = _parse_number(body, low, high, field)
            end = high if "/" in token else start
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError("bad_request", f"cron {field} 字段没有有效值")
    return values


@dataclass(frozen=True)
class CronSpec:
    """已解析的 5 字段 cron；``weekday`` 按标准 cron：0=周日，7 也视为周日。"""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    raw: str

    @property
    def day_restricted(self) -> bool:
        return len(self.days) < 31

    @property
    def weekday_restricted(self) -> bool:
        return len(self.weekdays) < 7

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        weekday_ok = (moment.isoweekday() % 7) in self.weekdays
        # 标准 cron：日/周字段都被限制时取 OR，否则取 AND
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def parse_cron(expr: Any) -> CronSpec:
    """解析 5 字段 cron 表达式；非法一律 ``bad_request``。"""
    if not isinstance(expr, str):
        raise ScheduleError("bad_request", "cron 表达式必须是字符串")
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError(
            "bad_request", f"cron 表达式必须是 5 个字段（分 时 日 月 周）: {expr!r}"
        )
    fields = [
        _parse_field(part, low, high, name)
        for part, (low, high), name in zip(parts, _FIELD_BOUNDS, _FIELD_NAMES, strict=True)
    ]
    weekdays = {0 if value == 7 else value for value in fields[4]}
    return CronSpec(
        minutes=frozenset(fields[0]),
        hours=frozenset(fields[1]),
        days=frozenset(fields[2]),
        months=frozenset(fields[3]),
        weekdays=frozenset(weekdays),
        raw=expr.strip(),
    )


def _zone(tz: str) -> Any:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("bad_request", f"未知时区: {tz}") from exc


def cron_next(expr: str, tz: str, after: float) -> float:
    """严格晚于 ``after`` 的下一个 cron 时点（epoch 秒）。"""
    spec = parse_cron(expr)
    zone = _zone(tz)
    candidate = datetime.fromtimestamp(float(after), zone).replace(second=0, microsecond=0)
    candidate += timedelta(minutes=1)
    # 逐分钟向前找：最坏情况（2/29）约 210 万次，仍在单次触发可接受范围内
    for _ in range(CRON_SEARCH_MINUTES):
        if spec.matches(candidate):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)
    raise ScheduleError("bad_request", f"cron 表达式在 5 年内没有可执行时点: {expr!r}")


def compute_next_run(
    trigger: dict[str, Any], after: float, *, last_run: float | None = None
) -> float | None:
    """按 trigger 计算 ``next_run_at``；webhook 返回 ``None``。

    ``interval`` 在重启恢复时按 ``last_run`` 对齐到 ``> after`` 的下一格，
    避免进程停了一晚上后「每个错过的格子都补跑」。
    """
    kind = str(trigger.get("kind") or "")
    if kind == "cron":
        return cron_next(
            str(trigger.get("expr") or ""), str(trigger.get("tz") or DEFAULT_TZ), after
        )
    if kind == "interval":
        minutes = int(trigger.get("minutes") or 0)
        step = minutes * 60
        if step <= 0:
            raise ScheduleError("bad_request", "interval.minutes 必须 >= 1")
        if last_run is None:
            return float(after) + step
        elapsed = float(after) - float(last_run)
        if elapsed < 0:
            return float(last_run) + step
        jumps = int(elapsed // step) + 1
        return float(last_run) + jumps * step
    if kind == "webhook":
        return None
    raise ScheduleError("bad_request", f"trigger.kind 非法: {kind!r}")


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def schedules_dir(root: Path) -> Path:
    return Path(root) / STATE_DIRNAME / SCHEDULES_DIRNAME


def events_path(root: Path) -> Path:
    return schedules_dir(root) / EVENTS_FILENAME


def _ensure_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(DIR_MODE)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _schedule_path(root: Path, sid: str) -> Path:
    validate_sid(sid)
    return schedules_dir(root) / f"{sid}.json"


def validate_sid(value: Any) -> str:
    if not isinstance(value, str) or not SID_RE.match(value):
        raise ScheduleError("bad_request", "sid 非法（应形如 sc-1a2b3c）")
    return value


def new_sid() -> str:
    return "sc-" + uuid.uuid4().hex[:8]


def _read_schedule_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScheduleError("not_found", "调度不存在") from exc
    except OSError as exc:
        raise ScheduleError("internal_error", f"读取调度失败: {exc}") from exc
    except ValueError as exc:
        raise ScheduleError("internal_error", f"调度文件无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise ScheduleError("internal_error", "调度文件格式非法")
    return data


def read_schedule(root: Path, sid: str) -> dict[str, Any]:
    path = _schedule_path(root, sid)
    with _LOCK:
        data = _read_schedule_file(path)
    data["sid"] = sid
    return normalize_schedule(data)


def _all_schedules(root: Path, *, include_disabled: bool = True) -> list[dict[str, Any]]:
    directory = schedules_dir(root)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    try:
        children = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for path in children:
        if not SID_RE.match(path.stem):
            continue
        try:
            item = _read_schedule_file(path)
        except ScheduleError:
            continue  # 单个损坏文件不拖垮列表
        item["sid"] = path.stem
        try:
            item = normalize_schedule(item)
        except ScheduleError:
            continue
        if not include_disabled and (not item.get("enabled") or item.get("archived")):
            continue
        items.append(item)
    return items


def list_schedules(root: Path, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    items = _all_schedules(root, include_disabled=include_disabled)
    ordered = sorted(items, key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return [_public(item) for item in ordered]


def public_schedule(schedule: dict[str, Any], *, webhook_token: str | None = None) -> dict[str, Any]:
    """对外的安全视图（去掉 token 哈希/原文）。"""
    return _public(schedule, webhook_token=webhook_token)


def _public(schedule: dict[str, Any], *, webhook_token: str | None = None) -> dict[str, Any]:
    """出口视图：不返回 token 原文/哈希，只留提示；新建/重置时单独返回一次原文。"""
    data = json.loads(json.dumps(schedule, ensure_ascii=False, default=str))
    trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
    if trigger.get("kind") == "webhook":
        trigger.pop("token_hash", None)
        trigger["configured"] = bool(trigger.pop("token_hint", ""))
    data["trigger"] = trigger
    if webhook_token:
        # 展示一次：同时给出 UI 约定的 trigger.token 和显式 webhook_token 别名；
        # 详情/列表永远不会带上它。
        trigger["token"] = webhook_token
        data["webhook_token"] = webhook_token
    return data


# --------------------------------------------------------------------------- #
# 校验 / 规整
# --------------------------------------------------------------------------- #
def normalize_schedule(data: dict[str, Any]) -> dict[str, Any]:
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {"kind": "cron"}
    return {
        "sid": data.get("sid", ""),
        "name": str(data.get("name") or ""),
        "target": {
            "kind": "project",
            "project_id": str(target.get("project_id") or ""),
            "suite_ids": [
                str(item) for item in (target.get("suite_ids") or []) if isinstance(item, str)
            ],
        },
        "trigger": dict(trigger),
        "env_override": (
            {str(key): value for key, value in (data.get("env_override") or {}).items()}
            if isinstance(data.get("env_override"), dict)
            else {}
        ),
        "enabled": bool(data.get("enabled", True)),
        "archived": bool(data.get("archived", False)),
        "created_at": float(data.get("created_at") or 0),
        "updated_at": float(data.get("updated_at") or 0),
        "last_run_at": data.get("last_run_at"),
        "last_batch_id": data.get("last_batch_id"),
        "last_status": data.get("last_status"),
        "next_run_at": data.get("next_run_at"),
    }


def _clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError("bad_request", "name 必须是非空字符串")
    name = value.strip()
    if len(name) > 80:
        raise ScheduleError("bad_request", "name 不能超过 80 个字符")
    return name


def _coerce_env(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScheduleError("bad_request", "env_override 必须是对象")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ScheduleError("bad_request", "env_override 的键必须是非空字符串")
        if item is None:
            continue
        if isinstance(item, (bool, str, int, float)):
            cleaned[key] = item
        else:
            raise ScheduleError(
                "bad_request", f"env_override.{key} 必须是 string/number/bool"
            )
    return cleaned


def _clean_target(root: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScheduleError("bad_request", "target 必须是对象")
    kind = value.get("kind", "project")
    if kind != "project":
        raise ScheduleError("bad_request", "target.kind 目前只支持 project")
    project_id = projects.validate_project_id(value.get("project_id"))
    try:
        project = projects.read_project(root, project_id)
    except projects.ProjectStoreError as exc:
        raise ScheduleError(exc.code, exc.message, status=exc.status) from exc
    known = {
        suite.get("suite_id")
        for suite in project.get("suites") or []
        if isinstance(suite, dict)
    }
    raw_suites = value.get("suite_ids")
    if raw_suites is None:
        raw_suites = []
    if not isinstance(raw_suites, list) or any(
        not isinstance(item, str) for item in raw_suites
    ):
        raise ScheduleError("bad_request", "target.suite_ids 必须是字符串数组")
    suite_ids: list[str] = []
    for suite_id in raw_suites:
        projects.validate_suite_id(suite_id)
        if suite_id not in known:
            raise ScheduleError("bad_request", f"套件不存在: {suite_id}")
        suite_ids.append(suite_id)
    return {"kind": "project", "project_id": project_id, "suite_ids": suite_ids}


def _new_webhook_secret() -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest(), token[-4:]


def _clean_trigger(
    value: Any, *, existing: dict[str, Any] | None = None
) -> tuple[dict[str, Any], str | None]:
    if not isinstance(value, dict):
        raise ScheduleError("bad_request", "trigger 必须是对象")
    kind = value.get("kind")
    if kind not in ("cron", "interval", "webhook"):
        raise ScheduleError("bad_request", "trigger.kind 必须是 cron|interval|webhook")
    existing = existing if isinstance(existing, dict) else {}
    if kind == "cron":
        expr = value.get("expr")
        expr_text = expr.strip() if isinstance(expr, str) else ""
        parse_cron(expr_text)  # 语法即校验
        tz = value.get("tz") or DEFAULT_TZ
        if not isinstance(tz, str) or not tz.strip():
            raise ScheduleError("bad_request", "trigger.tz 必须是非空字符串")
        _zone(tz.strip())
        return {"kind": "cron", "expr": expr_text, "tz": tz.strip()}, None
    if kind == "interval":
        minutes = value.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            raise ScheduleError("bad_request", "trigger.minutes 必须是整数")
        minutes = int(minutes)
        if minutes < MIN_INTERVAL_MINUTES:
            raise ScheduleError("bad_request", "trigger.minutes 最小为 1 分钟")
        if minutes > 525600:
            raise ScheduleError("bad_request", "trigger.minutes 过大")
        return {"kind": "interval", "minutes": minutes}, None
    # webhook
    rotate = bool(value.get("reset_token") or value.get("rotate_token"))
    if not rotate and existing.get("kind") == "webhook" and existing.get("token_hash"):
        return {
            "kind": "webhook",
            "token_hash": str(existing["token_hash"]),
            "token_hint": str(existing.get("token_hint") or ""),
        }, None
    token, token_hash, hint = _new_webhook_secret()
    return {"kind": "webhook", "token_hash": token_hash, "token_hint": hint}, token


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
def append_event(root: Path, sid: str, kind: str, detail: dict[str, Any] | None = None) -> None:
    """追加一条调度事件；写事件失败只记日志，绝不影响主流程。"""
    payload = {"ts": time.time(), "sid": sid, "kind": kind, "detail": detail or {}}
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    try:
        with _LOCK:
            _ensure_dir(schedules_dir(root))
            with events_path(root).open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError as exc:  # pragma: no cover - 磁盘异常
        LOGGER.warning("写入调度事件失败: %s", exc)


def read_events(root: Path, sid: str, *, limit: int = 100) -> list[dict[str, Any]]:
    path = events_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("sid") == sid:
            items.append(item)
    return items[-max(1, limit):]


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def create_schedule(root: Path, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScheduleError("bad_request", "请求体必须是 JSON 对象")
    now = time.time()
    trigger, webhook_token = _clean_trigger(payload.get("trigger"))
    schedule = {
        "sid": new_sid(),  # 客户端传的 sid 一律忽略
        "name": _clean_name(payload.get("name")),
        "target": _clean_target(root, payload.get("target")),
        "trigger": trigger,
        "env_override": _coerce_env(payload.get("env_override")),
        "enabled": bool(payload.get("enabled", True)),
        "archived": False,
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_batch_id": None,
        "last_status": None,
        "next_run_at": None,
    }
    if schedule["enabled"]:
        schedule["next_run_at"] = compute_next_run(trigger, now)
    with _LOCK:
        _ensure_dir(schedules_dir(root))
        _atomic_write(_schedule_path(root, schedule["sid"]), schedule)
    append_event(root, schedule["sid"], "created", {"trigger": trigger.get("kind")})
    return _public(schedule, webhook_token=webhook_token)


def update_schedule(root: Path, sid: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScheduleError("bad_request", "请求体必须是 JSON 对象")
    validate_sid(sid)
    webhook_token: str | None = None
    with _LOCK:
        schedule = read_schedule(root, sid)
        if "name" in payload:
            schedule["name"] = _clean_name(payload.get("name"))
        if "target" in payload:
            schedule["target"] = _clean_target(root, payload.get("target"))
        if "trigger" in payload:
            schedule["trigger"], webhook_token = _clean_trigger(
                payload.get("trigger"), existing=schedule.get("trigger")
            )
        if "env_override" in payload:
            schedule["env_override"] = _coerce_env(payload.get("env_override"))
        if "enabled" in payload:
            schedule["enabled"] = bool(payload.get("enabled"))
        schedule["updated_at"] = time.time()
        if schedule["enabled"]:
            schedule["next_run_at"] = compute_next_run(
                schedule["trigger"], schedule["updated_at"]
            )
        else:
            schedule["next_run_at"] = None
        _atomic_write(_schedule_path(root, sid), schedule)
    append_event(
        root,
        sid,
        "updated",
        {"enabled": schedule.get("enabled"), "trigger": schedule["trigger"].get("kind")},
    )
    return _public(schedule, webhook_token=webhook_token)


def delete_schedule(root: Path, sid: str) -> None:
    validate_sid(sid)
    path = _schedule_path(root, sid)
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ScheduleError("not_found", "调度不存在") from exc
        except OSError as exc:
            raise ScheduleError("internal_error", f"删除调度失败: {exc}") from exc
    append_event(root, sid, "deleted")


# --------------------------------------------------------------------------- #
# 触发
# --------------------------------------------------------------------------- #
def _batch_snapshot(batches: Any, batch_id: str | None) -> dict[str, Any] | None:
    if not batch_id or batches is None or not hasattr(batches, "get"):
        return None
    try:
        batch = batches.get(batch_id)
    except Exception:  # noqa: BLE001 - 批次表异常不能阻断调度
        return None
    if batch is None:
        return None
    try:
        snapshot = batch.snapshot()
    except Exception:  # noqa: BLE001
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _batch_is_running(batches: Any, batch_id: str | None) -> bool:
    snapshot = _batch_snapshot(batches, batch_id)
    return bool(snapshot and snapshot.get("status") in BATCH_ACTIVE_STATUSES)


def trigger_schedule(
    root: Path,
    sid: str,
    jobs: Any,
    batches: Any,
    *,
    trigger: str,
    env: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """真正触发一次批量运行；``trigger`` ∈ ``schedule|webhook|manual``。"""
    if trigger not in ("schedule", "webhook", "manual"):
        raise ScheduleError("bad_request", f"trigger 非法: {trigger!r}")
    now = time.time() if now is None else float(now)
    with _LOCK:
        schedule = read_schedule(root, sid)
        if not schedule.get("enabled") and trigger != "manual":
            raise ScheduleError("conflict", "调度已禁用", status=409)
        if _batch_is_running(batches, schedule.get("last_batch_id")):
            append_event(
                root,
                sid,
                "skipped_overlap",
                {"batch_id": schedule.get("last_batch_id"), "trigger": trigger},
            )
            schedule["last_status"] = "skipped_overlap"
            schedule["updated_at"] = now
            _atomic_write(_schedule_path(root, sid), schedule)
            return {
                "schedule_id": sid,
                "status": "skipped_overlap",
                "batch_id": schedule.get("last_batch_id"),
                "job_id": None,
                "trigger": trigger,
            }
        payload: dict[str, Any] = {
            "suite_ids": list(schedule["target"].get("suite_ids") or []),
        }
        merged_env = {**(schedule.get("env_override") or {}), **(env or {})}
        if merged_env:
            payload["env"] = merged_env
        # 幂等键先落盘：先写回 last_run_at，再创建批次。
        # 这样即使进程在「批次已创建、结果尚未写回」之间崩溃或抛异常，
        # 同一个到期时点也不会被下一次 tick 重复触发；tick 的 finally 还会
        # 把 next_run_at 推过 now，形成第二道保险。
        schedule["last_run_at"] = now
        schedule["updated_at"] = now
        _atomic_write(_schedule_path(root, sid), schedule)

    from . import service  # 局部导入：避免模块级循环

    try:
        result = service.start_project_batch(
            jobs,
            batches,
            root,
            schedule["target"]["project_id"],
            payload,
            trigger=trigger,
            extra_meta={"trigger": trigger, "schedule_id": sid},
        )
    except service.ApiError as exc:
        _mark_trigger_failed(root, sid, now)
        append_event(
            root, sid, "trigger_failed", {"trigger": trigger, "message": exc.message}
        )
        raise ScheduleError(exc.code, exc.message, status=exc.status) from exc
    except Exception:
        # 非 ApiError 交给 tick 记录 tick_failed；这里只回写失败状态
        _mark_trigger_failed(root, sid, now)
        raise

    batch_id = str(result.get("batch_id") or "")
    job_id = None
    for item in result.get("jobs") or []:
        if isinstance(item, dict) and item.get("job_id"):
            job_id = item["job_id"]
            break
    with _LOCK:
        schedule = read_schedule(root, sid)
        schedule["last_run_at"] = now
        schedule["last_batch_id"] = batch_id
        schedule["last_status"] = "running"
        schedule["updated_at"] = now
        _atomic_write(_schedule_path(root, sid), schedule)
    append_event(
        root,
        sid,
        "triggered",
        {"trigger": trigger, "batch_id": batch_id, "job_id": job_id},
    )
    return {
        "schedule_id": sid,
        "status": "triggered",
        "batch_id": batch_id,
        "job_id": job_id,
        "trigger": trigger,
    }


def find_by_webhook_token(root: Path, token: str) -> dict[str, Any]:
    """按明文 token 找到 webhook 调度；无效一律 404（不泄漏是否存在）。"""
    if not isinstance(token, str) or not token:
        raise ScheduleError("not_found", "hook 不存在")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for schedule in _all_schedules(root, include_disabled=True):
        trigger = schedule.get("trigger") or {}
        if trigger.get("kind") != "webhook":
            continue
        if secrets.compare_digest(str(trigger.get("token_hash") or ""), digest):
            return schedule
    raise ScheduleError("not_found", "hook 不存在")


def _mark_trigger_failed(root: Path, sid: str, now: float) -> None:
    """触发失败时尽力回写状态（不掩盖原始异常）。"""
    try:
        with _LOCK:
            fresh = read_schedule(root, sid)
            fresh["last_status"] = "trigger_failed"
            fresh["updated_at"] = now
            with contextlib.suppress(OSError):
                _atomic_write(_schedule_path(root, sid), fresh)
    except ScheduleError:
        return


# --------------------------------------------------------------------------- #
# 调度线程
# --------------------------------------------------------------------------- #
class Scheduler:
    """单进程调度线程：``start`` 恢复磁盘状态，``tick`` 可注入时钟便于测试。"""

    def __init__(
        self,
        root: Path,
        jobs: Any,
        batches: Any,
        *,
        clock: Any = None,
        tick_seconds: float = 1.0,
    ) -> None:
        self.root = Path(root)
        self.jobs = jobs
        self.batches = batches
        self.clock = clock or time.time
        self.tick_seconds = max(0.05, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 状态自检：最近一次 tick 的时钟值与最近一次单调度异常
        self._state_lock = threading.Lock()
        self.last_tick_at: float | None = None
        self.last_tick_error: dict[str, Any] | None = None

    # -- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        with _LOCK:
            if self._thread is not None and self._thread.is_alive():
                return
        self.recover()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="pv-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 调度线程必须活下去
                self._record_tick_error("", exc, self.clock())
                LOGGER.warning("调度 tick 失败: %s", exc)

    # -- 恢复 ---------------------------------------------------------- #
    def recover(self, *, now: float | None = None) -> None:
        """按磁盘状态重算 ``next_run_at``；错过的时点只记 ``missed``，不补跑。"""
        moment = self.clock() if now is None else float(now)
        for schedule in _all_schedules(self.root, include_disabled=True):
            sid = schedule["sid"]
            trigger = schedule.get("trigger") or {}
            previous = schedule.get("next_run_at")
            if not schedule.get("enabled") or trigger.get("kind") == "webhook":
                schedule["next_run_at"] = None
                self._persist(schedule)
                continue
            try:
                upcoming = compute_next_run(
                    trigger, moment, last_run=schedule.get("last_run_at")
                )
            except ScheduleError as exc:
                LOGGER.warning("调度 %s 的 trigger 无法恢复: %s", sid, exc.message)
                schedule["next_run_at"] = None
                self._persist(schedule)
                continue
            if isinstance(previous, (int, float)) and not isinstance(previous, bool):
                if float(previous) <= moment:
                    append_event(
                        self.root,
                        sid,
                        "missed",
                        {"due_at": previous, "recovered_at": moment},
                    )
            schedule["next_run_at"] = upcoming
            self._persist(schedule)

    def _persist(self, schedule: dict[str, Any]) -> None:
        with _LOCK:
            try:
                _atomic_write(
                    _schedule_path(self.root, schedule["sid"]),
                    normalize_schedule(schedule),
                )
            except OSError as exc:  # pragma: no cover - 磁盘异常
                LOGGER.warning("写回调度失败: %s", exc)

    # -- tick ---------------------------------------------------------- #
    def tick(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """检查全部到期调度；返回本次真实触发的摘要（测试可直接调用）。

        单个 schedule 的异常不会中断本轮其它调度：失败只记 ``tick_failed``，
        并且 ``finally`` 会无条件把到期调度的 ``next_run_at`` 推过 ``now``。
        """
        moment = self.clock() if now is None else float(now)
        with self._state_lock:
            self.last_tick_at = moment
        fired: list[dict[str, Any]] = []
        for schedule in _all_schedules(self.root, include_disabled=True):
            sid = str(schedule.get("sid") or "")
            try:
                self._tick_one(schedule, moment, fired)
            except Exception as exc:  # noqa: BLE001 - 单调度失败必须隔离
                self._record_tick_error(sid, exc, moment)
            finally:
                if sid:
                    self._advance_if_due(sid, moment)
        return fired

    def _tick_one(
        self, schedule: dict[str, Any], moment: float, fired: list[dict[str, Any]]
    ) -> None:
        sid = schedule["sid"]
        trigger = schedule.get("trigger") or {}
        if not schedule.get("enabled") or trigger.get("kind") == "webhook":
            self._sync_status(schedule)
            return
        due = schedule.get("next_run_at")
        if not isinstance(due, (int, float)) or isinstance(due, bool):
            schedule["next_run_at"] = compute_next_run(
                trigger, moment, last_run=schedule.get("last_run_at")
            )
            self._persist(schedule)
            self._sync_status(schedule)
            return
        if float(due) > moment:
            self._sync_status(schedule)
            return
        if moment - float(due) > MISSED_GRACE_SECONDS:
            # 进程可能刚睡醒/刚恢复：不补跑，只记 missed 并推到下一个时点
            append_event(self.root, sid, "missed", {"due_at": due, "checked_at": moment})
            return
        try:
            result = trigger_schedule(
                self.root,
                sid,
                self.jobs,
                self.batches,
                trigger="schedule",
                now=moment,
            )
            fired.append(result)
        except ScheduleError as exc:
            append_event(
                self.root,
                sid,
                "trigger_failed",
                {"trigger": "schedule", "message": exc.message},
            )

    def _advance_if_due(self, sid: str, moment: float) -> None:
        """``finally`` 里调用：到期调度的 ``next_run_at`` 至少要越过 ``now``。

        等价于 ``max(now, due) + interval``（cron 从 ``max(now, due)`` 找下一格），
        既避免异常时同一时点每 tick 重试，也避免 ``next_run_at`` 落后于当前时间。
        """
        try:
            fresh = read_schedule(self.root, sid)
        except ScheduleError:
            return
        trigger = fresh.get("trigger") or {}
        if not fresh.get("enabled") or trigger.get("kind") == "webhook":
            return
        due = fresh.get("next_run_at")
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            # trigger 损坏、连 due 都算不出来：兜底 60 秒后再试，避免 1 秒风暴
            fresh["next_run_at"] = moment + 60.0
            self._persist(fresh)
            return
        due_at = float(due)
        if due_at > moment:
            return  # 已经推进过，本轮不再重复推
        base = max(float(moment), due_at)
        try:
            if trigger.get("kind") == "interval":
                step = int(trigger.get("minutes") or 0) * 60
                if step <= 0:
                    raise ScheduleError("bad_request", "interval.minutes 必须 >= 1")
                upcoming: float | None = base + step
            else:
                upcoming = compute_next_run(trigger, base, last_run=None)
            if upcoming is None:
                raise ScheduleError("bad_request", "trigger 无法计算下一次运行")
        except Exception as exc:  # noqa: BLE001 - 推进失败也必须防止重试风暴
            LOGGER.warning("调度 %s 的 next_run_at 无法推进: %s", sid, exc)
            upcoming = base + 60.0
        fresh["next_run_at"] = float(upcoming)
        self._persist(fresh)

    def _record_tick_error(self, sid: str, exc: Exception, moment: float) -> None:
        trace = traceback.format_exc()
        entry = {
            "sid": sid or None,
            "type": type(exc).__name__,
            "message": str(exc),
            "at": float(moment),
            "traceback": trace[-4000:],
        }
        with self._state_lock:
            self.last_tick_error = entry
        LOGGER.error("调度 %s 执行失败: %s", sid or "<unknown>", exc, exc_info=True)
        if not sid:
            return
        with contextlib.suppress(Exception):
            append_event(
                self.root,
                sid,
                "tick_failed",
                {
                    "trigger": "schedule",
                    "type": entry["type"],
                    "message": entry["message"],
                    "traceback": entry["traceback"],
                },
            )

    def status_snapshot(self) -> dict[str, Any]:
        """``/api/status`` 用的调度线程观测字段（加锁读取）。"""
        with self._state_lock:
            return {
                "scheduler_running": self.running,
                "last_tick_at": self.last_tick_at,
                "last_tick_error": (
                    dict(self.last_tick_error) if self.last_tick_error else None
                ),
            }

    def _sync_status(self, schedule: dict[str, Any]) -> None:
        """把内存批次状态回写到 ``last_status``（尽力而为）。"""
        snapshot = _batch_snapshot(self.batches, schedule.get("last_batch_id"))
        if not snapshot:
            return
        status = snapshot.get("status")
        if not isinstance(status, str) or status == schedule.get("last_status"):
            return
        schedule["last_status"] = status
        self._persist(schedule)

    # ------------------------------------------------------------------ #
    def seconds_until_next(self) -> float | None:
        """最近一次待触发的间隔（仅供状态展示/调试）。"""
        nexts = [
            float(item["next_run_at"])
            for item in _all_schedules(self.root, include_disabled=False)
            if isinstance(item.get("next_run_at"), (int, float))
        ]
        if not nexts:
            return None
        return max(0.0, min(nexts) - float(self.clock()))


__all__ = [
    "DEFAULT_TZ",
    "EVENTS_FILENAME",
    "ScheduleError",
    "Scheduler",
    "compute_next_run",
    "create_schedule",
    "cron_next",
    "delete_schedule",
    "events_path",
    "find_by_webhook_token",
    "list_schedules",
    "new_sid",
    "normalize_schedule",
    "parse_cron",
    "public_schedule",
    "read_events",
    "read_schedule",
    "schedules_dir",
    "trigger_schedule",
    "update_schedule",
    "validate_sid",
]
