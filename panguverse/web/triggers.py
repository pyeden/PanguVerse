"""Workflow 级触发器：已发布工作流的手动 / 定时 / webhook 触发。

与 ``schedules``（项目级调度）并存：调度线程统一扫描两套存储，但本模块只
认 ``workflow_ref``（``w-xxxx@N``），不经过 project/suite/task。

落盘：``<workspace>/.panguverse/triggers/<trigger_id>.json``
（目录 ``0700``、文件 ``0600``、原子写），事件追加到
``<workspace>/.panguverse/triggers/events.jsonl``。

规则：

- ``workflow_ref`` 必须通过 :func:`panguverse.web.studio.load_published` 校验；
- ``kind=schedule`` 必须且只能给 ``schedule``（cron）或 ``interval`` 之一；
- ``kind=webhook`` 创建时生成 token，**只在创建/重置响应返回一次**，
  磁盘只保存 SHA-256 与末 4 位提示；
- 同一 trigger 不重叠：上次 job 仍 ``queued/running`` 时只写
  ``skipped_overlap`` 事件，不产生新 job；
- 重启按当前时间重算 ``next_run_at``，错过的时点写 ``missed``、不补跑；
- ``enabled=false`` 不参与调度，但允许显式 run-now。

cron 解析、时区与 ``next_run_at`` 计算复用 :mod:`panguverse.web.schedules`，
不重复实现，也不引入第三方 cron 库。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import schedules, studio
from .schedules import DEFAULT_TZ, DIR_MODE, MISSED_GRACE_SECONDS, ScheduleError

LOGGER = logging.getLogger(__name__)

TRIGGERS_DIRNAME = "triggers"
EVENTS_FILENAME = "events.jsonl"
TID_RE = re.compile(r"^tr-[0-9a-f]{8}$")
KINDS = ("schedule", "webhook", "manual")
ACTIVE_JOB_STATUSES = {"queued", "running"}

_LOCK = threading.RLock()

_STATUS_BY_CODE = {
    "bad_request": 400,
    "not_found": 404,
    "conflict": 409,
    "internal_error": 500,
}


class TriggerError(ScheduleError):
    """带错误码的触发器异常（由 ``api.py`` 统一渲染）。"""


def _error(code: str, message: str, *, status: int | None = None) -> TriggerError:
    return TriggerError(code, message, status=status or _STATUS_BY_CODE.get(code, 500))


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def triggers_dir(root: Path) -> Path:
    return Path(root) / schedules.STATE_DIRNAME / TRIGGERS_DIRNAME


def events_path(root: Path) -> Path:
    return triggers_dir(root) / EVENTS_FILENAME


def _trigger_path(root: Path, tid: str) -> Path:
    validate_tid(tid)
    return triggers_dir(root) / f"{tid}.json"


def validate_tid(value: Any) -> str:
    if not isinstance(value, str) or not TID_RE.match(value):
        raise _error("bad_request", "trigger_id 非法（应形如 tr-1a2b3c4d）")
    return value


def new_tid() -> str:
    return "tr-" + uuid.uuid4().hex[:8]


def _ensure_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # pragma: no cover - 权限异常不阻断
        directory.chmod(DIR_MODE)


def _read_trigger_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _error("not_found", "触发器不存在") from exc
    except OSError as exc:
        raise _error("internal_error", f"读取触发器失败: {exc}") from exc
    except ValueError as exc:
        raise _error("internal_error", f"触发器文件无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise _error("internal_error", "触发器文件格式非法")
    return data


def read_trigger(root: Path, tid: str) -> dict[str, Any]:
    path = _trigger_path(root, tid)
    with _LOCK:
        data = _read_trigger_file(path)
    data["trigger_id"] = tid
    return normalize_trigger(data)


def _all_triggers(root: Path, *, include_disabled: bool = True) -> list[dict[str, Any]]:
    directory = triggers_dir(root)
    if not directory.is_dir():
        return []
    try:
        children = sorted(directory.glob("*.json"))
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for path in children:
        if not TID_RE.match(path.stem):
            continue
        try:
            item = _read_trigger_file(path)
            item["trigger_id"] = path.stem
            item = normalize_trigger(item)
        except ScheduleError:
            continue  # 单个损坏文件不拖垮列表
        if not include_disabled and not item.get("enabled"):
            continue
        items.append(item)
    return items


def list_triggers(root: Path, wid: str) -> list[dict[str, Any]]:
    validate_wid(wid)
    items = [item for item in _all_triggers(root) if item.get("wid") == wid]
    ordered = sorted(
        items, key=lambda item: float(item.get("created_at") or 0), reverse=True
    )
    return [public_trigger(item) for item in ordered]


def public_trigger(trigger: dict[str, Any], *, webhook_token: str | None = None) -> dict[str, Any]:
    """对外安全视图：去掉 token 哈希，只留提示；创建/重置时附带一次原文。"""
    data = json.loads(json.dumps(trigger, ensure_ascii=False, default=str))
    if data.get("kind") == "webhook":
        webhook = data.get("webhook") if isinstance(data.get("webhook"), dict) else {}
        hint = str(webhook.get("token_hint") or "")
        public = {
            "token_hint": hint,
            "configured": bool(webhook.get("token_hash") or hint),
        }
        if webhook_token:
            public["token"] = webhook_token
        data["webhook"] = public
    else:
        data.pop("webhook", None)
    data.pop("token_hash", None)
    return data


# --------------------------------------------------------------------------- #
# 校验 / 规整
# --------------------------------------------------------------------------- #
def validate_wid(value: Any) -> str:
    try:
        return studio.validate_wid(value)
    except studio.StudioError as exc:
        raise _error(exc.code, exc.message, status=exc.status) from exc


def normalize_trigger(data: dict[str, Any]) -> dict[str, Any]:
    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        raise _error("internal_error", f"触发器 kind 非法: {kind!r}")
    rev = data.get("rev", 0)
    try:
        rev_int = int(rev)
    except (TypeError, ValueError) as exc:
        raise _error("internal_error", "触发器 rev 非法") from exc
    item: dict[str, Any] = {
        "trigger_id": str(data.get("trigger_id") or ""),
        "workflow_ref": str(data.get("workflow_ref") or ""),
        "wid": str(data.get("wid") or ""),
        "rev": rev_int,
        "kind": kind,
        "enabled": bool(data.get("enabled", True)),
        "env_override": (
            {str(key): value for key, value in (data.get("env_override") or {}).items()}
            if isinstance(data.get("env_override"), dict)
            else {}
        ),
        "last_run_at": data.get("last_run_at"),
        "last_status": data.get("last_status"),
        "last_job_id": data.get("last_job_id"),
        "next_run_at": data.get("next_run_at"),
        "created_at": float(data.get("created_at") or 0),
        "updated_at": float(data.get("updated_at") or 0),
    }
    if kind == "schedule":
        schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else None
        interval = data.get("interval") if isinstance(data.get("interval"), dict) else None
        if bool(schedule) == bool(interval):
            raise _error("internal_error", "schedule 触发器必须且只能有 cron 或 interval")
        if schedule:
            item["schedule"] = {
                "cron": str(schedule.get("cron") or ""),
                "tz": str(schedule.get("tz") or DEFAULT_TZ),
            }
        else:
            try:
                minutes = int(interval.get("minutes") or 0)
            except (TypeError, ValueError) as exc:
                raise _error("internal_error", "interval.minutes 非法") from exc
            item["interval"] = {"minutes": minutes}
    if kind == "webhook":
        webhook = data.get("webhook") if isinstance(data.get("webhook"), dict) else {}
        item["webhook"] = {
            "token_hash": str(webhook.get("token_hash") or ""),
            "token_hint": str(webhook.get("token_hint") or ""),
        }
    return item


def _clean_workflow_ref(root: Path, value: Any, *, wid: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise _error("bad_request", "workflow_ref 必须是非空字符串")
    try:
        published = studio.load_published(root, value.strip())
    except studio.StudioError as exc:
        raise _error(exc.code, exc.message, status=exc.status) from exc
    if published.get("wid") != wid:
        raise _error("bad_request", "workflow_ref 不属于该工作流")
    return published


def _clean_env_override(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _error("bad_request", "env_override 必须是对象")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise _error("bad_request", "env_override 的键必须是非空字符串")
        if item is None:
            continue
        if isinstance(item, (bool, str, int, float)):
            cleaned[key] = item
        else:
            raise _error("bad_request", f"env_override.{key} 必须是 string/number/bool")
    return cleaned


def _clean_schedule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("bad_request", "schedule 必须是对象")
    cron = value.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        raise _error("bad_request", "schedule.cron 必须是非空字符串")
    tz = value.get("tz") or DEFAULT_TZ
    if not isinstance(tz, str) or not tz.strip():
        raise _error("bad_request", "schedule.tz 必须是非空字符串")
    # 语法/时区用调度模块的解析器校验（与项目级调度同一套实现）
    try:
        schedules.parse_cron(cron.strip())
        schedules.compute_next_run(
            {"kind": "cron", "expr": cron.strip(), "tz": tz.strip()}, time.time()
        )
    except ScheduleError as exc:
        raise _error(exc.code, exc.message, status=exc.status) from exc
    return {"cron": cron.strip(), "tz": tz.strip()}


def _clean_interval(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("bad_request", "interval 必须是对象")
    minutes = value.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        raise _error("bad_request", "interval.minutes 必须是整数")
    minutes = int(minutes)
    if minutes < schedules.MIN_INTERVAL_MINUTES:
        raise _error("bad_request", "interval.minutes 最小为 1 分钟")
    if minutes > 525600:
        raise _error("bad_request", "interval.minutes 过大")
    return {"minutes": minutes}


def _new_webhook_secret() -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest(), token[-4:]


def _next_run_at(
    trigger: dict[str, Any], after: float, *, last_run: float | None = None
) -> float | None:
    """按 trigger 计算 ``next_run_at``；webhook/manual 返回 ``None``。"""
    if trigger.get("kind") != "schedule":
        return None
    schedule = trigger.get("schedule")
    if isinstance(schedule, dict):
        spec = {
            "kind": "cron",
            "expr": schedule.get("cron") or "",
            "tz": schedule.get("tz") or DEFAULT_TZ,
        }
    else:
        interval = trigger.get("interval") if isinstance(trigger.get("interval"), dict) else {}
        spec = {"kind": "interval", "minutes": int(interval.get("minutes") or 0)}
    try:
        return schedules.compute_next_run(spec, after, last_run=last_run)
    except ScheduleError as exc:
        raise _error(exc.code, exc.message, status=exc.status) from exc


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
def _mask_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """事件入口统一打码：复用 HTTP 层 ``mask_env_payload`` 的敏感键规则。"""
    try:
        from .api import mask_env_payload  # 运行时局部导入，避免模块级循环

        return mask_env_payload(detail)
    except Exception:  # noqa: BLE001 - 打码失败也不能丢事件
        return detail


def append_event(
    root: Path, tid: str, kind: str, detail: dict[str, Any] | None = None
) -> None:
    payload = {
        "ts": time.time(),
        "trigger_id": tid,
        "kind": kind,
        "detail": _mask_detail(detail or {}),
    }
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    try:
        with _LOCK:
            _ensure_dir(triggers_dir(root))
            with events_path(root).open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError as exc:  # pragma: no cover - 磁盘异常
        LOGGER.warning("写入触发器事件失败: %s", exc)


def read_events(root: Path, tid: str, *, limit: int = 100) -> list[dict[str, Any]]:
    try:
        lines = events_path(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("trigger_id") == tid:
            items.append(item)
    return items[-max(1, limit):]


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def create_trigger(root: Path, wid: str, payload: Any) -> dict[str, Any]:
    validate_wid(wid)
    if not isinstance(payload, dict):
        raise _error("bad_request", "请求体必须是 JSON 对象")
    kind = payload.get("kind")
    if kind not in KINDS:
        raise _error("bad_request", "kind 必须是 schedule|webhook|manual")
    published = _clean_workflow_ref(root, payload.get("workflow_ref"), wid=wid)

    now = time.time()
    trigger: dict[str, Any] = {
        "trigger_id": new_tid(),  # 客户端传入的 id 一律忽略
        "workflow_ref": str(published["workflow_ref"]),
        "wid": str(published["wid"]),
        "rev": int(published["rev"]),
        "kind": str(kind),
        "enabled": bool(payload.get("enabled", True)),
        "env_override": _clean_env_override(payload.get("env_override")),
        "last_run_at": None,
        "last_status": None,
        "last_job_id": None,
        "next_run_at": None,
        "created_at": now,
        "updated_at": now,
    }
    webhook_token: str | None = None
    if kind == "schedule":
        has_schedule = payload.get("schedule") is not None
        has_interval = payload.get("interval") is not None
        if has_schedule == has_interval:
            raise _error("bad_request", "schedule 与 interval 必须且只能提供一个")
        if has_schedule:
            trigger["schedule"] = _clean_schedule(payload.get("schedule"))
        else:
            trigger["interval"] = _clean_interval(payload.get("interval"))
    else:
        if payload.get("schedule") is not None or payload.get("interval") is not None:
            raise _error("bad_request", "只有 kind=schedule 才能配置 schedule/interval")
        if kind == "webhook":
            webhook_token, token_hash, hint = _new_webhook_secret()
            trigger["webhook"] = {"token_hash": token_hash, "token_hint": hint}

    if trigger["enabled"]:
        trigger["next_run_at"] = _next_run_at(trigger, now)

    with _LOCK:
        _ensure_dir(triggers_dir(root))
        schedules._atomic_write(_trigger_path(root, trigger["trigger_id"]), trigger)
    append_event(
        root,
        trigger["trigger_id"],
        "created",
        {"kind": kind, "workflow_ref": trigger["workflow_ref"]},
    )
    return public_trigger(trigger, webhook_token=webhook_token)


def update_trigger(root: Path, tid: str, payload: Any) -> dict[str, Any]:
    validate_tid(tid)
    if not isinstance(payload, dict):
        raise _error("bad_request", "请求体必须是 JSON 对象")
    webhook_token: str | None = None
    with _LOCK:
        trigger = read_trigger(root, tid)
        kind = trigger["kind"]
        schedule_changed = False

        if "enabled" in payload:
            trigger["enabled"] = bool(payload.get("enabled"))
            schedule_changed = True
        if "env_override" in payload:
            trigger["env_override"] = _clean_env_override(payload.get("env_override"))
        if "schedule" in payload or "interval" in payload:
            if kind != "schedule":
                raise _error("bad_request", "只有 kind=schedule 才能配置 schedule/interval")
            has_schedule = payload.get("schedule") is not None
            has_interval = payload.get("interval") is not None
            if has_schedule == has_interval:
                raise _error("bad_request", "schedule 与 interval 必须且只能提供一个")
            trigger.pop("schedule", None)
            trigger.pop("interval", None)
            if has_schedule:
                trigger["schedule"] = _clean_schedule(payload.get("schedule"))
            else:
                trigger["interval"] = _clean_interval(payload.get("interval"))
            schedule_changed = True
        if "regenerate_token" in payload:
            if kind != "webhook":
                raise _error("bad_request", "只有 kind=webhook 才能重置 token")
            if bool(payload.get("regenerate_token")):
                webhook_token, token_hash, hint = _new_webhook_secret()
                trigger["webhook"] = {"token_hash": token_hash, "token_hint": hint}

        now = time.time()
        trigger["updated_at"] = now
        if schedule_changed or "regenerate_token" in payload:
            trigger["next_run_at"] = (
                _next_run_at(trigger, now) if trigger["enabled"] else None
            )
        schedules._atomic_write(_trigger_path(root, tid), trigger)

    append_event(
        root,
        tid,
        "updated",
        {"enabled": trigger.get("enabled"), "kind": kind},
    )
    return public_trigger(trigger, webhook_token=webhook_token)


def delete_trigger(root: Path, tid: str) -> None:
    path = _trigger_path(root, tid)
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise _error("not_found", "触发器不存在") from exc
        except OSError as exc:
            raise _error("internal_error", f"删除触发器失败: {exc}") from exc
    append_event(root, tid, "deleted")


# --------------------------------------------------------------------------- #
# 触发
# --------------------------------------------------------------------------- #
def _job_status(jobs: Any, job_id: Any) -> str | None:
    if not job_id or jobs is None:
        return None
    try:
        job = jobs.get(str(job_id))
    except Exception:  # noqa: BLE001 - job 表异常不能阻断触发
        return None
    return str(getattr(job, "status", "") or "") if job is not None else None


def _job_active(jobs: Any, job_id: Any) -> bool:
    return _job_status(jobs, job_id) in ACTIVE_JOB_STATUSES


def find_by_token(root: Path, token: Any) -> dict[str, Any]:
    """按明文 webhook token 找到触发器；无效一律 404（不泄漏是否存在）。"""
    if not isinstance(token, str) or not token:
        raise _error("not_found", "hook 不存在")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for trigger in _all_triggers(root):
        if trigger.get("kind") != "webhook":
            continue
        webhook = trigger.get("webhook") or {}
        if secrets.compare_digest(str(webhook.get("token_hash") or ""), digest):
            return trigger
    raise _error("not_found", "hook 不存在")


def run_trigger(
    root: Path,
    tid: str,
    jobs: Any,
    *,
    trigger: str = "manual",
    env: dict[str, Any] | None = None,
    payload: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """真正启动一次 workflow job；``trigger`` ∈ ``manual|schedule|webhook``。"""
    if trigger not in ("manual", "schedule", "webhook"):
        raise _error("bad_request", f"trigger 非法: {trigger!r}")
    moment = time.time() if now is None else float(now)
    with _LOCK:
        item = read_trigger(root, tid)
        if not item.get("enabled") and trigger != "manual":
            raise _error("conflict", "触发器已禁用", status=409)
        if _job_active(jobs, item.get("last_job_id")):
            append_event(
                root,
                tid,
                "skipped_overlap",
                {"job_id": item.get("last_job_id"), "trigger": trigger},
            )
            item["last_status"] = "skipped_overlap"
            item["updated_at"] = moment
            schedules._atomic_write(_trigger_path(root, tid), item)
            return {
                "trigger_id": tid,
                "status": "skipped_overlap",
                "job_id": None,
                "trigger": trigger,
            }
        merged_env = {**(item.get("env_override") or {}), **(env or {})}
        # 先落 last_run_at：即使提交后进程崩溃，同一时点也不会重复触发
        item["last_run_at"] = moment
        item["last_status"] = "queued"
        item["updated_at"] = moment
        schedules._atomic_write(_trigger_path(root, tid), item)
        workflow_ref = item["workflow_ref"]

    from . import service  # 局部导入：避免模块级循环

    try:
        job = service.submit_run(
            jobs,
            root,
            {"mode": "workflow", "workflow_ref": workflow_ref, "env": merged_env},
            trigger=trigger,
            extra_meta={"workflow_ref": workflow_ref, "trigger_id": tid},
            on_finish=lambda finished: _mark_finished(root, tid, finished),
        )
    except service.ApiError as exc:
        _mark_status(root, tid, "trigger_failed")
        append_event(
            root, tid, "trigger_failed", {"trigger": trigger, "message": exc.message}
        )
        raise _error(exc.code, exc.message, status=exc.status) from exc
    except Exception as exc:
        _mark_status(root, tid, "trigger_failed")
        append_event(
            root,
            tid,
            "trigger_failed",
            {"trigger": trigger, "message": type(exc).__name__},
        )
        raise

    with _LOCK:
        item = read_trigger(root, tid)
        item["last_run_at"] = moment
        item["last_job_id"] = job.job_id
        item["last_status"] = str(job.status or "queued")
        item["updated_at"] = moment
        schedules._atomic_write(_trigger_path(root, tid), item)

    detail: dict[str, Any] = {
        "trigger": trigger,
        "job_id": job.job_id,
        "workflow_ref": workflow_ref,
    }
    if env:
        detail["env"] = env
    if payload is not None:
        detail["payload"] = payload
    append_event(root, tid, "triggered", detail)
    return {
        "trigger_id": tid,
        "job_id": job.job_id,
        "status": "queued",
        "trigger": trigger,
    }


def _mark_finished(root: Path, tid: str, job: Any) -> None:
    """job 终态回调：只在本 trigger 仍指向该 job 时回写 last_status。"""
    try:
        with _LOCK:
            item = read_trigger(root, tid)
            if item.get("last_job_id") != job.job_id:
                return
            status = str(getattr(job, "status", "") or "")
            if not status or item.get("last_status") == status:
                return
            item["last_status"] = status
            item["updated_at"] = time.time()
            schedules._atomic_write(_trigger_path(root, tid), item)
    except ScheduleError:
        return


def _mark_status(root: Path, tid: str, status: str) -> None:
    try:
        with _LOCK:
            item = read_trigger(root, tid)
            item["last_status"] = status
            item["updated_at"] = time.time()
            schedules._atomic_write(_trigger_path(root, tid), item)
    except ScheduleError:
        return


# --------------------------------------------------------------------------- #
# 调度线程集成
# --------------------------------------------------------------------------- #
def recover(root: Path, *, now: float | None = None) -> None:
    """重启恢复：重算 ``next_run_at``，错过的时点只写 ``missed``，不补跑。"""
    moment = time.time() if now is None else float(now)
    for trigger in _all_triggers(root):
        tid = trigger["trigger_id"]
        if trigger.get("kind") != "schedule":
            if trigger.get("next_run_at") is not None:
                trigger["next_run_at"] = None
                _persist(root, trigger)
            continue
        if not trigger.get("enabled"):
            trigger["next_run_at"] = None
            _persist(root, trigger)
            continue
        previous = trigger.get("next_run_at")
        try:
            upcoming = _next_run_at(
                trigger, moment, last_run=trigger.get("last_run_at")
            )
        except ScheduleError as exc:
            LOGGER.warning("触发器 %s 的 schedule 无法恢复: %s", tid, exc.message)
            trigger["next_run_at"] = None
            _persist(root, trigger)
            continue
        if isinstance(previous, (int, float)) and not isinstance(previous, bool):
            if float(previous) <= moment:
                append_event(
                    root,
                    tid,
                    "missed",
                    {"due_at": previous, "recovered_at": moment},
                )
        trigger["next_run_at"] = upcoming
        _persist(root, trigger)


def _persist(root: Path, trigger: dict[str, Any]) -> None:
    with _LOCK:
        try:
            schedules._atomic_write(
                _trigger_path(root, trigger["trigger_id"]),
                normalize_trigger(trigger),
            )
        except (OSError, ScheduleError) as exc:  # pragma: no cover - 磁盘异常
            LOGGER.warning("写回触发器失败: %s", exc)


def tick(root: Path, jobs: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    """检查全部到期 workflow 触发器；供 ``schedules.Scheduler.tick`` 调用。"""
    moment = time.time() if now is None else float(now)
    fired: list[dict[str, Any]] = []
    for trigger in _all_triggers(root):
        tid = str(trigger.get("trigger_id") or "")
        if trigger.get("kind") != "schedule":
            _sync_job_status(root, trigger, jobs)
            continue
        due = trigger.get("next_run_at")
        try:
            if not trigger.get("enabled"):
                _sync_job_status(root, trigger, jobs)
                continue
            if not isinstance(due, (int, float)) or isinstance(due, bool):
                trigger["next_run_at"] = _next_run_at(trigger, moment)
                _persist(root, trigger)
                _sync_job_status(root, trigger, jobs)
                continue
            if float(due) > moment:
                _sync_job_status(root, trigger, jobs)
                continue
            if moment - float(due) > MISSED_GRACE_SECONDS:
                append_event(
                    root, tid, "missed", {"due_at": due, "checked_at": moment}
                )
                continue
            result = run_trigger(root, tid, jobs, trigger="schedule", now=moment)
            fired.append(result)
        except ScheduleError:
            # run_trigger 已写 trigger_failed；这里只保证下一时点被推进
            pass
        except Exception as exc:  # noqa: BLE001 - 单触发器失败必须隔离
            LOGGER.warning("触发器 %s tick 失败: %s", tid, exc)
            append_event(
                root,
                tid,
                "tick_failed",
                {"trigger": "schedule", "type": type(exc).__name__, "message": str(exc)},
            )
        finally:
            if tid:
                _advance_if_due(root, tid, moment)
    return fired


def _advance_if_due(root: Path, tid: str, moment: float) -> None:
    """``finally`` 兜底：到期触发器的 ``next_run_at`` 至少越过 ``now``。"""
    try:
        fresh = read_trigger(root, tid)
    except ScheduleError:
        return
    if fresh.get("kind") != "schedule" or not fresh.get("enabled"):
        return
    due = fresh.get("next_run_at")
    if isinstance(due, bool) or not isinstance(due, (int, float)):
        fresh["next_run_at"] = moment + 60.0
        _persist(root, fresh)
        return
    due_at = float(due)
    if due_at > moment:
        return
    base = max(float(moment), due_at)
    try:
        interval = fresh.get("interval")
        if isinstance(interval, dict):
            upcoming: float | None = base + int(interval.get("minutes") or 0) * 60
            if upcoming is None or upcoming <= base:
                raise _error("bad_request", "interval.minutes 必须 >= 1")
        else:
            upcoming = _next_run_at(fresh, base)
        if upcoming is None:
            raise _error("bad_request", "触发器无法计算下一次运行")
    except Exception as exc:  # noqa: BLE001 - 推进失败也不能形成重试风暴
        LOGGER.warning("触发器 %s 的 next_run_at 无法推进: %s", tid, exc)
        upcoming = base + 60.0
    fresh["next_run_at"] = float(upcoming)
    _persist(root, fresh)


def _sync_job_status(root: Path, trigger: dict[str, Any], jobs: Any) -> None:
    job_id = trigger.get("last_job_id")
    status = _job_status(jobs, job_id)
    if not status or status == trigger.get("last_status"):
        return
    trigger["last_status"] = status
    _persist(root, trigger)


__all__ = [
    "EVENTS_FILENAME",
    "TRIGGERS_DIRNAME",
    "TriggerError",
    "append_event",
    "create_trigger",
    "delete_trigger",
    "events_path",
    "find_by_token",
    "list_triggers",
    "new_tid",
    "normalize_trigger",
    "public_trigger",
    "read_events",
    "read_trigger",
    "recover",
    "run_trigger",
    "tick",
    "triggers_dir",
    "update_trigger",
    "validate_tid",
    "validate_wid",
]
