"""``local_fs`` 领域包的意图模板：自然语言 → 文件/数据类自动化（无 LLM 也能用）。

这组模板证明：**换一个行业只加一个包**——规则规划器会自动把它们纳入候选，
不需要改内核一行代码。用法::

    panguverse loop -i "备份配置文件并校验备份存在" --env FS_ROOT=./demo
"""

from __future__ import annotations

from typing import Any

from ..core.contract import RuleTemplate


def _template(name: str, description: str, keywords: tuple[str, ...], builder, tags=(), weight=1) -> RuleTemplate:
    return RuleTemplate(
        name=name,
        description=description,
        keywords=tuple(keywords),
        builder=builder,
        tags=tuple(tags),
        weight=weight,
        domain="filesystem",
        pack="local_fs",
    )


def build_config_backup() -> dict[str, Any]:
    """生成配置 → 复制成快照 → 校验快照存在且内容一致。"""
    return {
        "id": "config_backup",
        "name": "配置备份：生成配置 → 打快照 → 校验内容",
        "tags": ["ops", "backup"],
        "description": "写出一份配置，复制到 backups/，再断言内容一致（纯文件系统闭环）",
        "steps": [
            {
                "id": "write_config",
                "use": "fs_write_text",
                "name": "写出配置",
                "with": {"path": "config/app.conf", "text": "mode=production\nretries=3\n"},
            },
            {
                "id": "backup",
                "use": "fs_copy",
                "name": "备份配置",
                "with": {"source": "config/app.conf", "target": "backups/app.conf.bak"},
                "expect": {"exists": True},
            },
            {
                "id": "verify_content",
                "use": "assert_file_contains",
                "name": "校验备份内容",
                "with": {"path": "backups/app.conf.bak", "text": "mode=production"},
            },
            {
                "id": "list_backups",
                "use": "fs_list",
                "name": "列出备份目录",
                "with": {"path": "backups", "pattern": "*.bak"},
            },
            {
                "id": "assert_backup_count",
                "use": "assert_json",
                "with": {"source": "${list_backups}", "path": "count", "gte": 1},
            },
        ],
    }


def build_data_pipeline() -> dict[str, Any]:
    """生成 JSON 数据 → 回读 → 字段断言 → 汇总统计。"""
    return {
        "id": "data_roundtrip",
        "name": "数据落盘回读：写 JSON → 读回 → 字段断言",
        "tags": ["data", "roundtrip"],
        "description": "验证「写入的数据读回来还是那份数据」，并检查关键字段",
        "steps": [
            {
                "id": "write_json",
                "use": "fs_write_json",
                "name": "写入数据",
                "with": {"path": "data/records.json", "data": {"count": 2, "items": ["a", "b"], "state": "READY"}},
                "expect": {"exists": True},
            },
            {
                "id": "read_json",
                "use": "fs_read_json",
                "name": "回读数据",
                "with": {"path": "data/records.json"},
            },
            {
                "id": "assert_state",
                "use": "assert_json",
                "with": {"source": "${read_json.data}", "path": "state", "equals": "READY"},
            },
            {
                "id": "assert_items",
                "use": "assert_json",
                "with": {"source": "${read_json.data}", "path": "items", "length": 2, "type": "array"},
            },
            {
                "id": "typed_summary",
                "use": "set_var",
                "name": "汇总（演示变量运算在非 API 领域同样可用）",
                "with": {"name": "summary", "value": "${read_json.data.count|int}"},
            },
            {
                "id": "log_summary",
                "use": "log",
                "with": {"message": "数据回读校验完成，count=${summary}"},
            },
        ],
    }


def build_cleanup() -> dict[str, Any]:
    """确认临时文件不存在 → 清理 → 再确认。"""
    return {
        "id": "cleanup_tasks",
        "name": "清理任务：确认 → 删除 → 复核",
        "tags": ["ops", "cleanup"],
        "description": "安全地删除临时产物，并复核删除结果（destructive 副作用演示）",
        "steps": [
            {
                "id": "prepare",
                "use": "fs_write_text",
                "name": "准备临时文件",
                "with": {"path": "tmp/scratch.txt", "text": "temporary"},
            },
            {
                "id": "exists_before",
                "use": "fs_exists",
                "name": "确认临时文件存在",
                "with": {"path": "tmp/scratch.txt"},
            },
            {
                "id": "assert_before",
                "use": "assert_json",
                "with": {"source": "${exists_before}", "path": "exists", "equals": True},
            },
            {
                "id": "delete",
                "use": "fs_delete",
                "name": "删除临时文件",
                "with": {"path": "tmp/scratch.txt"},
            },
            {
                "id": "exists_after",
                "use": "fs_exists",
                "name": "复核已删除",
                "with": {"path": "tmp/scratch.txt"},
            },
            {
                "id": "assert_after",
                "use": "assert_json",
                "with": {"source": "${exists_after}", "path": "exists", "equals": False},
            },
        ],
    }


def local_fs_intent_templates() -> list[RuleTemplate]:
    """``local_fs`` 包声明的意图模板。"""
    return [
        _template(
            "fs_config_backup",
            "配置文件备份与校验",
            ("备份", "backup", "配置", "conf", "快照", "snapshot", "归档"),
            build_config_backup,
            tags=("ops", "backup"),
            weight=2,
        ),
        _template(
            "fs_data_roundtrip",
            "数据落盘与回读校验",
            ("文件", "file", "落盘", "写文件", "数据校验", "json 文件", "roundtrip"),
            build_data_pipeline,
            tags=("data",),
        ),
        _template(
            "fs_cleanup",
            "临时文件清理与复核",
            ("清理", "cleanup", "删除文件", "临时文件", "释放空间"),
            build_cleanup,
            tags=("ops", "cleanup"),
        ),
    ]
