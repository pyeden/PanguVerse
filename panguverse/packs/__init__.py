"""内置领域包。

平台的可扩展性来自这里：**新增一个行业 = 新增一个包**，内核不需要改动。

- :mod:`~panguverse.packs.core` —— 领域中立的通用原子（等待/记录/变量/JSON）
- :mod:`~panguverse.packs.api_testing` —— HTTP / API 测试领域
- :mod:`~panguverse.packs.local_fs` —— 本地文件系统领域
- :mod:`~panguverse.packs.web_research` —— 网页抓取 / 正文 / 结构 / LLM 总结领域
- :mod:`~panguverse.packs.web_browser` —— Chrome 无头浏览器 / 渲染后 DOM / 截图领域
- :mod:`~panguverse.packs.android_adb` —— 安卓 adb 设备控制领域
- :mod:`~panguverse.packs.ios_sim` —— iOS 模拟器（simctl）领域

第三方包：把 ``*.pack.yaml``（声明式、无需 Python）放进项目或 ``PANGUVERSE_PACKS``
指向的目录即可被加载，见 :func:`panguverse.core.pack.load_pack_dir`。
"""

from __future__ import annotations

from ..core.pack import AtomPack
from . import android_adb, api_testing, core, ios_sim, local_fs, web_browser, web_research

_BUILDERS = (
    core.build_pack,
    api_testing.build_pack,
    local_fs.build_pack,
    web_research.build_pack,
    web_browser.build_pack,
    android_adb.build_pack,
    ios_sim.build_pack,
)


def builtin_packs() -> list[AtomPack]:
    """构建全部内置领域包（每次调用都产出新实例，便于测试隔离）。"""
    return [builder() for builder in _BUILDERS]


def pack_names() -> list[str]:
    return [builder().name for builder in _BUILDERS]


__all__ = ["AtomPack", "builtin_packs", "pack_names"]
