"""站点别名 → URL 的**数据表**。

意图识别只做「站点名 → 候选 URL」这一件小事，不把任何站点写进某条意图分支。
新增站点时只需要在 :data:`SITE_ALIASES` 里追加一条 :class:`SiteSpec`：

```python
SiteSpec(
    key="example",
    aliases=("示例站", "example"),
    default_url="https://www.example.com",
    topic_variants=(
        TopicVariant(keywords=("热点", "热搜"), url="https://www.example.com/hot"),
    ),
)
```

匹配规则（刻意保守）：
- 显式 URL 优先，调用方只在没有显式 URL 时才调用 :func:`resolve_site_url`；
- 别名按最长命中；同一句话命中多个站点时取最长别名（更具体的站点名优先）；
- ``topic_variants`` 命中任意关键词时使用对应 URL，否则退回 ``default_url``；
- 未知站点不编造 URL，返回 ``None``，由上层继续报 ``missing_inputs``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicVariant:
    """站点内的「主题页」：命中关键词时换用更具体的 URL。"""

    keywords: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class SiteSpec:
    """一个站点的别名、默认 URL 与可选主题页。"""

    key: str
    aliases: tuple[str, ...]
    default_url: str
    topic_variants: tuple[TopicVariant, ...] = ()


#: 站点别名表。扩展方式见模块文档：追加一条 SiteSpec 即可，不需要改编排代码。
SITE_ALIASES: tuple[SiteSpec, ...] = (
    SiteSpec(
        key="baidu",
        aliases=("百度", "baidu"),
        default_url="https://www.baidu.com",
        topic_variants=(
            TopicVariant(
                keywords=("实时热点", "热点", "热搜", "热门新闻", "今日热点"),
                url="https://top.baidu.com/board?tab=realtime",
            ),
        ),
    ),
    # 以下两个站点是「数据表可扩展」的样板，不参与任何特判。
    SiteSpec(
        key="weibo",
        aliases=("微博", "weibo"),
        default_url="https://weibo.com",
        topic_variants=(
            TopicVariant(keywords=("热搜", "热点"), url="https://s.weibo.com/top/summary"),
        ),
    ),
    SiteSpec(
        key="zhihu",
        aliases=("知乎", "zhihu"),
        default_url="https://www.zhihu.com",
        topic_variants=(
            TopicVariant(keywords=("热榜", "热点"), url="https://www.zhihu.com/hot"),
        ),
    ),
)


@dataclass(frozen=True)
class SiteResolution:
    """一次站点解析结果，供意图分析记录来源。"""

    site: str
    alias: str
    url: str
    source: str = "site_alias"
    variant: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {"site": self.site, "alias": self.alias, "url": self.url, "source": self.source}
        if self.variant:
            data["variant"] = self.variant
        return data


def resolve_site_url(text: str) -> SiteResolution | None:
    """从一句话里识别已知站点名，返回解析结果；未知站点返回 ``None``。

    只在调用方确认「没有显式 URL」后使用。命中多个站点时取**最长别名**，
    避免「某某百度合作站」这类文本把合作站误当成百度。
    """
    lowered = str(text or "").lower()
    if not lowered:
        return None
    best: tuple[int, SiteSpec, str] | None = None
    for site in SITE_ALIASES:
        for alias in site.aliases:
            needle = alias.lower()
            if needle and needle in lowered:
                if best is None or len(needle) > best[0]:
                    best = (len(needle), site, alias)
    if best is None:
        return None
    _, site, alias = best
    for variant in site.topic_variants:
        if any(keyword in lowered for keyword in variant.keywords):
            return SiteResolution(
                site=site.key,
                alias=alias,
                url=variant.url,
                variant=",".join(k for k in variant.keywords if k in lowered),
            )
    return SiteResolution(site=site.key, alias=alias, url=site.default_url)


__all__ = [
    "SITE_ALIASES",
    "SiteResolution",
    "SiteSpec",
    "TopicVariant",
    "resolve_site_url",
]
