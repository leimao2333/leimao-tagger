#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""磊毛阅读 · PC 端「按标签搜索遍历」打标管道（v1.8.7.2 待办 #25）。

在此之前 PC 工具只能按**站方分类**遍历（`explore()`），于是打标范围完全取决于
源恰好配了哪些分类页 —— 站方没建某个属性分类，那一类内容就一页也不进清单。
App 侧的解法是把受控词表里的题材 / 属性词绑到源**搜索入口**上
（`TagRepository.attachSearchTags`），本模块把这套口径搬到 PC 端：

1. **标签清单**：读 `vocab.json` 的 groups（`gen_vocab.py` 从 App 的
   `TagAlias.GROUPS` 生成），组首词即标签名，组内其余写法即同义词；
2. **写法挑选**：与 App 的 `TagAlias.searchTermsForQuery` 同一口径 —— 标签名排第一，
   然后是组内第一个不同写法（通常是简繁互补的那个），最后补一个外文写法
   （优先拉丁，其次日文）。不做全组展开：1341 个写法全发一次就是几千个请求；
3. **源分配**：照抄 App 的「全源轮转 + 单标签源上限」—— 游标在可搜索源之间依次
   分配，每个标签最多 `--tag-sources` 个源，既不把长尾晾着，也不会一个标签
   打满几十个源；
4. **打标口径**：搜索结果**只用来发现条目** —— 抓到的作品汇入主清单，照常走
   标题解析 + 视觉打标，**不把搜索词本身写成标签**。这与 App 的「打标证据化」
   是同一件事：关键词命中 ≠ 作品属于该标签（否则标题写了「无码」的片会被
   一律当作该分类内容）。要按标签圈内容，请用 App 端建本地索引的分类页证据。

产物结构与 `explore()` 完全一致，调用方直接并入既有清单即可。
"""

from __future__ import annotations

import json
from pathlib import Path

from urllib.parse import quote  # noqa: F401  （保留给调用方做日志截断）

# 与 App 侧 TagAlias.MAX_SEARCH_TERMS_PER_SOURCE 保持同一数值（v1.8.7.1 起为 3）
DEFAULT_TERMS_PER_TAG = 3
# 与 App 侧 ComicGenres.MAX_SOURCES_PER_GENRE 同口径
DEFAULT_SOURCES_PER_TAG = 8
DEFAULT_TAG_LIMIT = 40
DEFAULT_TAG_PAGES = 2

# 玩法与 App 不同：PC 侧是本地批处理，源没有「实时等待」压力，
# 所以每次最多抓几页给得比 App 的 _tag_pages 略宽，由调用方覆盖。
DEFAULT_PAGES_PER_TERM = DEFAULT_TAG_PAGES


def script_of(term: str) -> int:
    """写法所属字符集：0 = 中文，1 = 日文（含假名），2 = 拉丁。"""
    if all(ord(c) < 128 for c in term):
        return 2
    if any("\u3040" <= c <= "\u30ff" for c in term):
        return 1
    return 0


def load_groups(path: str | Path) -> list[list[str]]:
    """读受控词表（vocab.json）：返回 [[写法...], ...]，组内第一个是规范名。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    groups = data.get("groups") or []
    return [g for g in groups if isinstance(g, list) and g]


def pick_terms(tag: str, group: list[str], limit: int = DEFAULT_TERMS_PER_TAG) -> list[str]:
    """给一个标签挑出发搜索用的写法（App `searchTermsForQuery` 的同一口径）。

    挑选顺序：标签名 → 组内第一个不同写法（简繁互补）→ 一个外文写法
    （优先拉丁，其次日文）→ 按声明顺序补满。
    """
    if limit <= 0 or not group:
        return []
    picked: list[str] = [tag] if tag else [group[0]]
    if len(group) > 1:
        picked.append(next((w for w in group if w != picked[0]), group[0]))
    for script in (2, 1):
        if len(picked) >= limit:
            break
        if any(script_of(w) == script for w in picked):
            continue
        hit = next((w for w in group if script_of(w) == script), None)
        if hit:
            picked.append(hit)
    for word in group:
        if len(picked) >= limit:
            break
        if word not in picked:
            picked.append(word)
    # 去重并保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for w in picked:
        key = "".join(w.split()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out[:limit] if limit > 0 else out


class TagPlan:
    """本轮要跑的标签 + 写法 + 分配到的源。"""

    def __init__(self, tag: str, terms: list[str], source_ids: list[str]):
        self.tag = tag
        self.terms = terms
        self.source_ids = source_ids

    def describe(self) -> str:
        return f"{self.tag}（{'、'.join(self.terms)} × {len(self.source_ids)} 源）"


def build_plans(
    groups: list[list[str]],
    searchable_sources: list,
    only_tags: list[str] | None = None,
    tag_limit: int = DEFAULT_TAG_LIMIT,
    sources_per_tag: int = DEFAULT_SOURCES_PER_TAG,
    terms_per_tag: int = DEFAULT_TERMS_PER_TAG,
) -> list[TagPlan]:
    """构造标签搜索计划。

    @param only_tags     只跑这些标签（逗号分隔的手工清单）；None = 按词表顺序跑前 tag_limit 个
    @param tag_limit     最多跑多少个标签（0 = 不限）
    @param sources_per_tag 每个标签最多分配几个源（0 = 全部；轮转分配）
    """
    pool_ids = [getattr(s, "source_id", "") or getattr(s, "name", "") for s in searchable_sources]
    plans: list[TagPlan] = []
    if only_tags:
        by_name = {g[0]: g for g in groups}
        names: list[str] = []
        for raw in only_tags:
            for name in [n.strip() for n in str(raw).split(",") if n.strip()]:
                group = by_name.get(name)
                if group:
                    names.append(name)
                else:
                    # 词表里没有的词也允许跑（自定义标签），单写法不展开
                    plans.append(TagPlan(name, [name], pool_ids[: max(sources_per_tag, 0) or len(pool_ids)]))
    else:
        names = [g[0] for g in groups]
        if tag_limit > 0:
            names = names[:tag_limit]

    cursor = 0
    for name in names:
        group = next((g for g in groups if g[0] == name), [name])
        terms = pick_terms(name, group, terms_per_tag)
        if not terms:
            continue
        if sources_per_tag <= 0 or sources_per_tag >= len(pool_ids):
            chosen = list(pool_ids)
        else:
            # 全源轮转：每个标签从上次结束的位置接着拿，保证长尾源也能分到标签
            chosen = []
            scanned = 0
            while len(chosen) < sources_per_tag and scanned < len(pool_ids):
                src_id = pool_ids[(cursor + scanned) % len(pool_ids)]
                scanned += 1
                if src_id and src_id not in chosen:
                    chosen.append(src_id)
            cursor = (cursor + scanned) % max(len(pool_ids), 1)
        if not chosen:
            continue
        plans.append(TagPlan(name, terms, chosen))
    return plans


def run_tag_search(
    plans: list[TagPlan],
    sources_by_id: dict,
    pages: int,
    log=print,
    should_stop=None,
    on_progress=None,
) -> tuple[list[dict], dict]:
    """逐标签 × 逐源搜索，返回 (items, stats)。

    @param pages       每个 (标签, 写法, 源) 最多抓几页；<=0 = 抓到空页为止
    @param on_progress (tag, done_terms, total_terms, items_found) 每个写法回调
    @return items 结构与 [source_engine.TraversedItem] 一致，可直接并入主清单
    """
    items: list[dict] = []
    stats = {
        "tags": len(plans),
        "searches": 0,
        "skipped": 0,
        "hits": 0,
        "failed": 0,
    }
    seen_keys: set[str] = set()

    total_terms = sum(len(p.terms) for p in plans)
    done_terms = 0

    for plan in plans:
        if should_stop and should_stop():
            log("  已停止")
            break
        log(f"标签「{plan.tag}」：{len(plan.terms)} 个写法 × {len(plan.source_ids)} 源")
        for term in plan.terms:
            done_terms += 1
            if should_stop and should_stop():
                break
            fresh_in_term = 0
            for source in (sources_by_id.get(sid) for sid in plan.source_ids):
                if source is None:
                    continue
                if not getattr(source, "can_search", lambda: False)():
                    stats["skipped"] += 1
                    continue
                if should_stop and should_stop():
                    break
                stats["searches"] += 1
                try:
                    found = source.search(term, pages, log)
                except Exception as exc:  # noqa: BLE001
                    stats["failed"] += 1
                    log(f"  ✗ {getattr(source, 'name', source)}「{term}」搜索失败：{exc}")
                    continue
                fresh = 0
                for item in found:
                    key = item.get("itemId") or item.get("title")
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    items.append(dict(item))
                    fresh += 1
                fresh_in_term += fresh
                stats["hits"] += len(found)
                if found:
                    log(f"  ✓ {getattr(source, 'name', source)}「{term}」+{len(found)}"
                        f"（新 {fresh}）")
            if on_progress:
                try:
                    on_progress(plan.tag, done_terms, total_terms, len(items))
                except Exception:  # noqa: BLE001
                    pass
            if fresh_in_term:
                log(f"标签「{plan.tag}」写法「{term}」累计 +{fresh_in_term}，"
                    f"本轮合计 {len(items)} 部")

    return items, stats
