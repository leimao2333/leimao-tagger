#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""磊毛阅读 · PC 端网络源遍历引擎（v1.8.1.6）。

在 PC 上直接导入「网络源」并遍历，抓取条目的 标题 + 封面 汇入打标清单 ——
不必先从手机导出收录清单。支持三类源：

1. **legado / shuyuan 书源**（JSON 数组，含 `bookSourceUrl` + `ruleExplore`）：
   解析 `exploreUrl`（逐行 `分类名::地址`）与 `ruleExplore` 规则。
2. **App JSON 源规范**（`{name, baseUrl, search:{url, list, fields}}`）：
   用 search 规则逐页抓取（URL 需要 {{keyword}} 的源跳过）。
3. **MoonTV / MacCMS 采集站**（`{api_site: {key: {name, api}}}`）：
   `ac=videolist` 接口翻页抓取。

规则引擎为精简实现：JSONPath 子集（`$.a.b[0]` / `$[*]`）+ CSS 选择器子集
（`selector@attr`、`class.x` / `id.x` / `tag.x`，尾部 `.N` 取第 N 个），
`{{page}}` 与纯算术 `{{(page-1)*20}}` 占位符可渲染；`@js:` 等复杂规则跳过。
JS 源 / Venera 源无法在 PC 端评估，遍历时记录日志后跳过。

**标签搜索（v1.8.7.2 待办 #25）**：每个源除 `explore()`（按分类遍历）外还有
`search(keyword, pages, ...)`（按标签同义写法搜索），口径对齐 App 侧
`TagEntryUrl` / `LegadoBookSource.searchUrl` / `MoonTvSourceAdapter.ac=detail&wd=`：
- legado：`searchUrl` 模板（`{{key}}` / `{{page}}`）+ `ruleSearch` 字段规则；
- App JSON：`search.url` 里的 `{{keyword}}` 占位；
- MacCMS：`?ac=detail&wd=<词>&pg=<页>`；
- Venera / JS：PC 端评估不了，跳过。

搜索只用来**发现条目**：抓到的作品照常走标题解析 + 视觉打标，不把搜索词本身
写成标签 —— 与 App 的「打标证据化」口径一致（关键词命中 ≠ 作品属于该标签）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

USER_AGENT = "leimao-pc-tagger/1.6"
TIMEOUT = (5, 12)  # (连接, 读取) 秒；慢源快速跳过，避免单源卡几分钟

_TRUTHY_PAGE_EXPR = re.compile(r"^[\d\s()+\-*%]*$")


def fetch_text(url: str, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update({k: v for k, v in headers.items() if isinstance(v, str)})
    resp = requests.get(url, timeout=timeout, headers=merged)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def fetch_json(url: str, timeout: int = TIMEOUT, headers: dict | None = None):
    return json.loads(fetch_text(url, timeout, headers))


# ---------------------------------------------------------------- URL 渲染


def render_url(template: str, page: int = 1, keyword: str = "",
               allow_keyword: bool = False) -> str | None:
    """渲染 URL 模板：{{page}} / {{searchPage}} / 纯算术 {{(page-1)*20}} / {{keyword}}。

    其余 {{表达式}}（需要 JS 求值）渲染失败时按空串丢弃。

    @param allow_keyword 是否渲染 {{keyword}} / {{key}} 占位。遍历（explore）场景
      没有关键词，传 False 时遇到这两种占位一律返回 None（老行为）；标签搜索
      （search）场景传 True，并把关键词做 URL 编码。
    """
    if ("{{keyword}}" in template or "{{key}}" in template) and not allow_keyword:
        return None
    out = (template
           .replace("{{keyword}}", quote(keyword, safe=""))
           .replace("{{key}}", quote(keyword, safe="")))
    out = out.replace("{{searchPage}}", str(page)).replace("{{page}}", str(page))

    def _eval(m: re.Match) -> str:
        expr = m.group(1).replace("page", str(page)).replace("searchPage", str(page))
        if not _TRUTHY_PAGE_EXPR.match(expr):
            return ""
        try:
            return str(int(eval(expr, {"__builtins__": {}}, {})))  # noqa: S307 表达式已白名单
        except Exception:  # noqa: BLE001
            return ""

    return re.sub(r"\{\{([^{}]+)\}\}", _eval, out)


def normalize_term(term: str) -> str:
    """同义写法归一到源站查询词（App 侧 [TagAlias.norm] 的同一口径：去空白 + 小写）。"""
    return "".join(term.split()).lower()


# ---------------------------------------------------------------- 规则求值


def jsonpath_get(obj, expr: str):
    """JSONPath 子集：`$.a.b[0]`、`$[*].x`、`$.a[*]`。返回 list（可能为空）。"""
    if not expr.startswith("$"):
        return None
    path = expr[1:]
    tokens: list = []
    for m in re.finditer(r"\.([A-Za-z0-9_\-]+)|\[(\*|\d+)\]", path):
        if m.group(1) is not None:
            tokens.append(("k", m.group(1)))
        else:
            tokens.append(("idx", m.group(2)))
    cur: list = [obj]
    for kind, val in tokens:
        nxt: list = []
        for node in cur:
            if kind == "k":
                if isinstance(node, dict) and val in node:
                    nxt.append(node[val])
            else:
                if val == "*" and isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, list) and val != "*":
                    i = int(val)
                    if -len(node) <= i < len(node):
                        nxt.append(node[i])
        cur = nxt
    return cur


def _css_selector(token: str) -> str:
    """legado 简写 → CSS：`class.x` → `.x`、`id.x` → `#x`、`tag.div` → `div`。"""
    if token.startswith("class."):
        return "." + token[6:]
    if token.startswith("id."):
        return "#" + token[3:]
    if token.startswith("tag."):
        return token[4:]
    if token.startswith("@css:"):
        return token[5:]
    return token


def _split_index(token: str) -> tuple[str, int | None]:
    """拆尾部索引：`tag.h3.0` → (`tag.h3`, 0)；`class.x.y` → (`class.x.y`, None)。"""
    m = re.match(r"^(.+)\.(-?\d+)$", token)
    if m:
        return m.group(1), int(m.group(2))
    return token, None


def css_select_chain(node, expr: str) -> list:
    """链式选择：`id.x.0@tag.li` → 先 #x 取第 0 个，再在其中选全部 li。

    返回全部匹配节点（列表规则用）；字段规则取第一个。
    """
    parts = [p.strip() for p in expr.split("@") if p.strip()]
    if not parts:
        return []
    cur: list = [node]
    for token in parts:
        base, index = _split_index(token)
        sel = _css_selector(base)
        nxt: list = []
        for c in cur:
            if c is None:
                continue
            try:
                found = c.select(sel) if sel else [c]
            except Exception:  # noqa: BLE001 选择器语法超出 soupsieve 支持范围
                found = []
            if not found:
                continue
            if index is not None and -len(found) <= index < len(found):
                nxt.append(found[index])
            else:
                nxt.extend(found)
        cur = nxt
        if not cur:
            break
    return cur


_KNOWN_ATTRS = ("text", "html", "all", "href", "src", "alt", "title", "content",
                "value", "data-src", "data-original", "data-lazy-src", "url")


def css_extract(node, expr: str) -> str:
    """提取表达式：`selector@attr`（attr ∈ text/html/href/src/… 或属性名）。

    `@` 可链式：`class.item@tag.a@href`；尾部 `.N` 表示取第 N 个（0 起）。
    """
    parts = [p.strip() for p in expr.split("@") if p.strip()]
    if not parts:
        return ""
    last = parts[-1]
    if last.lower() in _KNOWN_ATTRS or (not last.startswith(("class.", "id.", "tag.", "@css", ".", "#"))
                                        and not re.match(r"^[A-Za-z][\w\-]*(\[|\.)", last)):
        attr = parts.pop()
    else:
        attr = "text"
    nodes = css_select_chain(node, "@".join(parts))
    if not nodes:
        return ""
    cur = nodes[0]
    a = attr.lower()
    if a == "text":
        return cur.get_text(strip=True)
    if a in ("html", "all"):
        return cur.decode_contents()
    if hasattr(cur, "get"):
        return cur.get(attr) or cur.get(a) or ""
    return ""


def looks_jsonpath(rule: str) -> bool:
    return rule.strip().startswith("$")


def looks_js_rule(rule: str) -> bool:
    return rule.strip().startswith(("@js:", "<js>", "@JavaScript"))


def extract_list(body: str, list_rule: str, field_rules: dict, base_url: str) -> list[dict]:
    """从响应体提取列表：JSONPath 或 CSS 两条路，字段规则逐个求值。"""
    items: list[dict] = []
    if list_rule and looks_jsonpath(list_rule):
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return items
        nodes = jsonpath_get(data, list_rule) or []
        for node in nodes:
            fields: dict[str, str] = {}
            for name, rule in field_rules.items():
                if looks_jsonpath(rule):
                    vals = jsonpath_get(node, rule)
                    v = vals[0] if vals else ""
                elif looks_js_rule(rule):
                    v = ""
                else:
                    v = css_extract(node if not isinstance(node, str) else BeautifulSoup(node, "html.parser"), rule)
                fields[name] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            items.append(fields)
        return items

    if not list_rule:
        return items
    soup = BeautifulSoup(body, "html.parser")
    if looks_js_rule(list_rule):
        return items
    nodes = css_select_chain(soup, list_rule)
    for node in nodes:
        fields = {}
        for name, rule in field_rules.items():
            fields[name] = "" if looks_js_rule(rule) else css_extract(node, rule)
        items.append(fields)
    return items


def abs_url(base: str, value: str) -> str:
    v = (value or "").strip()
    if not v or v.startswith(("http://", "https://", "data:")):
        return v
    return urljoin(base, v)


# ---------------------------------------------------------------- 源模型


class TraversedItem(dict):
    """遍历所得条目：sourceId / itemId / itemType / title / cover / sourceName。"""


class BaseSource:
    name = "未知源"
    source_id = ""
    item_type = "COMIC"

    def explore(self, pages: int, log, progress=None) -> list[TraversedItem]:
        """遍历源；progress(done_pages, total_pages, items_found) 每页回调一次（UI 进度）。"""
        raise NotImplementedError

    def search(self, keyword: str, pages: int, log, progress=None) -> list[TraversedItem]:
        """按标签关键词搜索源（v1.8.7.2 待办 #25）。

        与 [explore] 的产物结构完全一致，因此调用方可以把搜索结果直接并入遍历结果，
        一起走标题解析 + 视觉打标。**不把 keyword 本身写成标签** —— 关键词命中
        不等于作品属于该标签（App 侧「打标证据化」的同一口径）。

        @param keyword 已经过同义组归一的一个写法（见 tag_search.py）
        @param pages   最多抓几页；<=0 = 抓到没有内容为止
        """
        return []

    # 源是否支持关键词搜索（标签搜索管道据此跳过并计数）
    def can_search(self) -> bool:
        return False


def _drain_pages(fetch_rows, pages: int, log, progress, make_item, tag: str):
    """翻页抓取的公共骨架：累加到 max_pages / 遇到空页 / 连续 3 页无新内容即停。

    @param fetch_rows(page) 抓一页并返回原始 rows（抓不到时返回 None 即停）
    @param make_item(row)   原始行 → [TraversedItem]（无标题/封面的行返回 None）
    """
    found: dict[str, TraversedItem] = {}
    unlimited = pages <= 0
    stale = 0
    for page in range(1, pages + 1) if not unlimited else _count_from_one():
        rows = fetch_rows(page)
        if rows is None:
            break
        fresh = 0
        for row in rows:
            item = make_item(row)
            if not item:
                continue
            key = item.get("itemId") or item.get("title")
            if key in found:
                continue
            found[key] = item
            fresh += 1
        if progress:
            try:
                progress(page, len(found))
            except Exception:  # noqa: BLE001
                pass
        log(f"  「{tag}」第 {page} 页：+{fresh}")
        if not rows:
            break
        if fresh == 0:
            stale += 1
            if stale >= 3:  # 连续 3 页无新内容 = 站点翻页重复，跳过该源
                log(f"  「{tag}」连续 3 页无新内容，停止翻页")
                break
        else:
            stale = 0
    return list(found.values())


def _count_from_one():
    """无限翻页的页号生成器（1, 2, 3, …）。"""
    n = 0
    while True:
        n += 1
        yield n


class LegadoSource(BaseSource):
    """legado / shuyuan 书源：exploreUrl + ruleExplore。"""

    def __init__(self, obj: dict, item_type: str = "COMIC"):
        self.obj = obj
        self.source_id = (obj.get("bookSourceUrl") or obj.get("sourceUrl") or "").strip()
        self.name = (obj.get("bookSourceName") or obj.get("sourceName")
                     or urlparse(self.source_id).netloc or "legado源").strip()
        self.item_type = item_type
        self.base = self.source_id
        explore_raw = (obj.get("exploreUrl") or "").strip()
        self.categories = _parse_legado_explore_url(explore_raw)
        self.rule_explore = obj.get("ruleExplore") or {}
        # legado 的 header 可能是 JSON 字符串，也可能是对象
        h = obj.get("header")
        if isinstance(h, str):
            try:
                h = json.loads(h)
            except Exception:  # noqa: BLE001
                h = {}
        self.search_headers = h if isinstance(h, dict) else {}
        # 标签搜索（v1.8.7.2 #25）：searchUrl 模板 + ruleSearch 字段规则
        self.search_url_tpl = (obj.get("searchUrl") or "").strip()
        rule_search = obj.get("ruleSearch") or {}
        self.rule_search = rule_search if isinstance(rule_search, dict) else {}

    def can_search(self) -> bool:
        return bool(self.search_url_tpl and self.rule_search.get("bookList"))

    def search(self, keyword: str, pages: int, log, progress=None) -> list[TraversedItem]:
        if not self.can_search():
            log(f"  跳过：searchUrl / ruleSearch.bookList 缺失（{self.name}）")
            return []
        if self.search_url_tpl.startswith(("@js", "<js")):
            log(f"  跳过：searchUrl 含 @js，PC 端不支持（{self.name}）")
            return []
        fields = {
            "title": self.rule_search.get("name") or "",
            "cover": self.rule_search.get("coverUrl") or "",
            "author": self.rule_search.get("author") or "",
            "detailUrl": self.rule_search.get("bookUrl") or "",
        }
        state = {"url": ""}

        def fetch_rows(page):
            url = render_url(self.search_url_tpl, page, keyword, allow_keyword=True)
            if not url:
                return None
            url = abs_url(self.base, url)
            if url == state["url"]:  # URL 不再变化 = 无分页参数，防止死循环
                return None
            state["url"] = url
            try:
                body = fetch_text(url, headers=self.search_headers)
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {self.name} 搜索「{keyword}」第 {page} 页：{exc}")
                return None
            # 搜索结果的元信息没有意义，只取标题 / 封面 / 详情地址
            return extract_list(
                body, self.rule_search.get("bookList", ""),
                {k: v for k, v in fields.items() if k != "author"}, url,
            )

        def make_item(row) -> TraversedItem | None:
            title = (row.get("title") or "").strip()
            cover = abs_url(state["url"], row.get("cover") or "")
            detail = abs_url(state["url"], row.get("detailUrl") or "")
            if not title or not cover:
                return None
            return TraversedItem(
                sourceId=self.source_id, itemId=detail or title, itemType=self.item_type,
                title=title, cover=cover, sourceName=self.name,
            )

        return _drain_pages(
            fetch_rows, pages, log, progress, make_item,
            tag=f"{self.name}·搜索「{keyword}」",
        )

    def explore(self, pages: int, log, progress=None) -> list[TraversedItem]:
        if not self.categories:
            log(f"  跳过：exploreUrl 为空或含 @js（{self.name}）")
            return []
        if not self.rule_explore.get("bookList"):
            log(f"  跳过：ruleExplore.bookList 缺失（{self.name}）")
            return []
        field_map = {
            "title": self.rule_explore.get("name") or "",
            "cover": self.rule_explore.get("coverUrl") or "",
            "author": self.rule_explore.get("author") or "",
            "detailUrl": self.rule_explore.get("bookUrl") or "",
        }
        unlimited = pages <= 0  # 0/负数 = 抓全部页，直到某页没有内容
        total_pages = 0 if unlimited else sum(
            (pages if "{{" in tpl else 1) for _, tpl in self.categories
        )
        done_pages = 0
        stale = 0
        found: dict[str, TraversedItem] = {}
        for title, cat_tpl in self.categories:
            has_page = "{{" in cat_tpl
            page = 0
            prev_url = None
            while True:
                page += 1
                if has_page and not unlimited and page > pages:
                    break
                url = render_url(cat_tpl, page)
                if not url or url == prev_url:  # URL 不再变化 = 无分页参数，防止死循环
                    break
                prev_url = url
                url = abs_url(self.base, url)
                try:
                    body = fetch_text(url, headers=self.search_headers)
                except Exception as exc:  # noqa: BLE001
                    log(f"  ✗ {self.name}「{title}」第 {page} 页：{exc}")
                    break
                rows = extract_list(body, self.rule_explore.get("bookList", ""), field_map, url)
                fresh = 0
                for row in rows:
                    t = (row.get("title") or "").strip()
                    cover = abs_url(url, row.get("cover") or "")
                    detail = abs_url(url, row.get("detailUrl") or "")
                    if not t or not cover:
                        continue
                    key = detail or t
                    if key in found:
                        continue
                    found[key] = TraversedItem(
                        sourceId=self.source_id, itemId=detail or t, itemType=self.item_type,
                        title=t, cover=cover, sourceName=self.name,
                    )
                    fresh += 1
                done_pages += 1
                if progress:
                    try:
                        progress(done_pages, total_pages, len(found))
                    except Exception:  # noqa: BLE001
                        pass
                log(f"  {self.name}「{title}」第 {page} 页：+{fresh}")
                if not rows:
                    break
                if fresh == 0:
                    stale += 1
                    if stale >= 3:  # 连续 3 页无新内容 = 站点翻页重复，跳过该源
                        log(f"  {self.name}「{title}」连续 3 页无新内容，停止翻页")
                        break
                else:
                    stale = 0
        return list(found.values())


def _parse_legado_explore_url(raw: str) -> list[tuple[str, str]]:
    """解析 exploreUrl：逐行 `分类名::地址`；含 @js / <js> 的返回空（PC 端不支持）。"""
    if not raw or raw.startswith(("@js:", "<js>", "@json:", "[")):
        return []
    cats: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "::" in line:
            t, u = line.split("::", 1)
        elif "\t" in line:
            t, u = line.split("\t", 1)
        else:
            t, u = line, line
        t, u = t.strip(), u.strip()
        if u and not u.startswith(("@js", "<js")):
            cats.append((t or u, u))
    return cats


class AppJsonSource(BaseSource):
    """App JSON 源规范：search.url 逐页抓取（不含 {{keyword}} 时才可用）。"""

    def __init__(self, obj: dict, item_type: str = "COMIC"):
        self.obj = obj
        self.source_id = (obj.get("baseUrl") or obj.get("name") or "").strip()
        self.name = (obj.get("name") or "JSON源").strip()
        self.item_type = item_type
        self.base = obj.get("baseUrl") or ""
        search = obj.get("search") or {}
        self.url_tpl = search.get("url") or ""
        self.list_rule = search.get("list") or ""
        self.fields = search.get("fields") or {}
        self.headers = search.get("headers") or {}

    def can_search(self) -> bool:
        if not self.url_tpl or self.list_rule.startswith("@js"):
            return False
        return "{{keyword}}" in self.url_tpl or "{{key}}" in self.url_tpl

    def search(self, keyword: str, pages: int, log, progress=None) -> list[TraversedItem]:
        if not self.can_search():
            log(f"  跳过：search.url 不含 {{keyword}} 或 list 为 @js（{self.name}）")
            return []
        state = {"url": ""}

        def fetch_rows(page):
            url = render_url(self.url_tpl, page, keyword, allow_keyword=True)
            if not url:
                return None
            url = abs_url(self.base, url)
            if url == state["url"]:
                return None
            state["url"] = url
            try:
                body = fetch_text(url, headers=self.headers)
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {self.name} 搜索「{keyword}」第 {page} 页：{exc}")
                return None
            return extract_list(body, self.list_rule, self.fields, url)

        def make_item(row) -> TraversedItem | None:
            title = (row.get("title") or row.get("name") or "").strip()
            cover = abs_url(state["url"], row.get("cover") or row.get("image") or "")
            detail = abs_url(self.base, row.get("id") or row.get("url") or "")
            if not title or not cover:
                return None
            return TraversedItem(
                sourceId=self.source_id, itemId=detail or title, itemType=self.item_type,
                title=title, cover=cover, sourceName=self.name,
            )

        return _drain_pages(
            fetch_rows, pages, log, progress, make_item,
            tag=f"{self.name}·搜索「{keyword}」",
        )

    def explore(self, pages: int, log, progress=None) -> list[TraversedItem]:
        if not self.url_tpl:
            log(f"  跳过：search.url 缺失（{self.name}）")
            return []
        if "{{keyword}}" in self.url_tpl or "{{key}}" in self.url_tpl:
            log(f"  跳过：search.url 需要 {{keyword}}，PC 端遍历不支持（{self.name}）")
            return []
        if self.list_rule.startswith("@js"):
            log(f"  跳过：search.list 为 @js 规则（{self.name}）")
            return []
        found: dict[str, TraversedItem] = {}
        unlimited = pages <= 0
        has_page = "{{" in self.url_tpl
        total_pages = 0 if unlimited else (pages if has_page else 1)
        done_pages = 0
        stale = 0
        page = 0
        prev_url = None
        while True:
            page += 1
            if has_page and not unlimited and page > pages:
                break
            url = render_url(self.url_tpl, page)
            if not url or url == prev_url:
                break
            prev_url = url
            url = abs_url(self.base, url)
            try:
                body = fetch_text(url, headers=self.headers)
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {self.name} 第 {page} 页：{exc}")
                break
            rows = extract_list(body, self.list_rule, self.fields, url)
            fresh = 0
            for row in rows:
                t = (row.get("title") or row.get("name") or "").strip()
                cover = abs_url(url, row.get("cover") or row.get("image") or "")
                detail = abs_url(self.base, row.get("id") or row.get("url") or "")
                if not t or not cover:
                    continue
                key = detail or t
                if key in found:
                    continue
                found[key] = TraversedItem(
                    sourceId=self.source_id, itemId=detail or t, itemType=self.item_type,
                    title=t, cover=cover, sourceName=self.name,
                )
                fresh += 1
            done_pages += 1
            if progress:
                try:
                    progress(done_pages, total_pages, len(found))
                except Exception:  # noqa: BLE001
                    pass
            log(f"  {self.name} 第 {page} 页：+{fresh}")
            if not rows:
                break
            if fresh == 0:
                stale += 1
                if stale >= 3:  # 连续 3 页无新内容 = 翻页重复
                    log(f"  {self.name} 连续 3 页无新内容，停止翻页")
                    break
            else:
                stale = 0
        return list(found.values())


class MacCmsSource(BaseSource):
    """MoonTV / MacCMS 采集站：api.php?ac=videolist 翻页。"""

    def __init__(self, name: str, api: str, detail: str = ""):
        self.name = name
        self.source_id = api
        self.api = api.rstrip("?")
        self.detail = detail.rstrip("/")
        self.item_type = "VIDEO"

    def can_search(self) -> bool:
        return True

    def search(self, keyword: str, pages: int, log, progress=None) -> list[TraversedItem]:
        """按 `ac=detail&wd=<词>&pg=<页>` 搜索（与 App 的 [MoonTvSourceAdapter] 同一口径）。"""

        def fetch_rows(page):
            url = f"{self.api}?ac=detail&wd={quote(keyword, safe='')}&pg={page}"
            try:
                data = fetch_json(url)
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {self.name} 搜索「{keyword}」第 {page} 页：{exc}")
                return None
            return data.get("list") or []

        def make_item(row) -> TraversedItem | None:
            title = (row.get("vod_name") or "").strip()
            cover = (row.get("vod_pic") or "").strip()
            if not title or not cover:
                return None
            video_id = str(row.get("vod_id") or title)
            detail = f"{self.detail}?p={video_id}" if self.detail else video_id
            return TraversedItem(
                sourceId=self.source_id, itemId=detail or title, itemType="VIDEO",
                title=title, cover=cover, sourceName=self.name,
            )

        return _drain_pages(
            fetch_rows, pages, log, progress, make_item,
            tag=f"{self.name}·搜索「{keyword}」",
        )

    def explore(self, pages: int, log, progress=None) -> list[TraversedItem]:
        found: dict[str, TraversedItem] = {}
        unlimited = pages <= 0
        stale = 0
        page = 0
        while True:
            page += 1
            if not unlimited and page > pages:
                break
            url = f"{self.api}?ac=videolist&pg={page}"
            try:
                data = fetch_json(url)
            except Exception as exc:  # noqa: BLE001
                log(f"  ✗ {self.name} 第 {page} 页：{exc}")
                break
            rows = data.get("list") or []
            fresh = 0
            for row in rows:
                t = (row.get("vod_name") or "").strip()
                pic = (row.get("vod_pic") or "").strip()
                if not t or not pic:
                    continue
                vid = str(row.get("vod_id") or t)
                detail = f"{self.detail}?p={vid}" if self.detail else vid
                key = detail or t
                if key in found:
                    continue
                found[key] = TraversedItem(
                    sourceId=self.source_id, itemId=vid, itemType="VIDEO",
                    title=t, cover=pic, sourceName=self.name,
                )
                fresh += 1
            # MacCMS 能从响应里拿到总页数，进度条可用
            pagecount = 0
            if isinstance(data.get("page"), dict):
                try:
                    pagecount = int(data["page"].get("pagecount") or 0)
                except Exception:  # noqa: BLE001
                    pagecount = 0
            total_pages = pagecount if unlimited else pages
            if progress:
                try:
                    progress(page, total_pages, len(found))
                except Exception:  # noqa: BLE001
                    pass
            log(f"  {self.name} 第 {page} 页：+{fresh}")
            if unlimited and pagecount and page >= pagecount:
                break
            if not rows:
                break
            if fresh == 0:
                stale += 1
                if stale >= 3:  # 连续 3 页无新内容 = 翻页重复
                    log(f"  {self.name} 连续 3 页无新内容，停止翻页")
                    break
            else:
                stale = 0
        return list(found.values())


# ---------------------------------------------------------------- 装载


class VeneraSource(BaseSource):
    """Venera JS 漫画源（extends ComicSource）：node 子进程执行 venera_runner.js。

    runner 通过 NODE_PATH 加载 cheerio，shim 官方 _venera_.js API 面
    （Network / HtmlDocument / Comic / Convert / loadSetting / loadData 等）。
    pages <= 0 时抓全部页（以 maxPage 与空页为准，200 页保险上限）。
    """

    _MAX_PAGES_UNLIMITED = 200  # 全量模式单分类保险上限，防止站点无限翻页

    def __init__(self, raw: str, item_type: str = "COMIC"):
        self.raw = raw
        self.item_type = item_type
        self.name = "Venera源"
        self.source_id = ""
        self._probed = False

    # ---------- node / runner ----------
    @staticmethod
    def _find_node() -> str | None:
        exe = "node.exe" if os.name == "nt" else "node"
        # 1) PyInstaller 解包目录（--add-binary 打包的 node）
        mei = getattr(sys, "_MEIPASS", None)
        if mei and os.path.isfile(os.path.join(mei, exe)):
            return os.path.join(mei, exe)
        # 2) exe 同目录 / 脚本目录
        for base in (os.path.dirname(os.path.abspath(sys.argv[0])),
                     os.path.dirname(os.path.abspath(__file__))):
            p = os.path.join(base, exe)
            if os.path.isfile(p):
                return p
        # 3) PATH
        w = shutil.which("node")
        if w:
            return w
        # 4) 常见固定路径
        for p in (
            r"C:\Program Files\nodejs\node.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
        ):
            if os.path.isfile(p):
                return p
        return None

    def _runner_path(self) -> str:
        base = os.path.dirname(os.path.abspath(__file__))
        mei = getattr(sys, "_MEIPASS", None)
        for d in (mei, base):
            if d and os.path.isfile(os.path.join(d, "venera_runner.js")):
                return os.path.join(d, "venera_runner.js")
        return os.path.join(base, "venera_runner.js")

    def _run(self, job: dict, log) -> dict | None:
        node = self._find_node()
        if not node:
            log(f"  ✗ {self.name}：未找到 node.exe，Venera 源需要 Node 运行时")
            return None
        env = {**os.environ}
        # cheerio 所在目录（打包/开发两种布局都试）
        base = os.path.dirname(os.path.abspath(__file__))
        mei = getattr(sys, "_MEIPASS", None)
        for d in (mei, base):
            nm = os.path.join(d, "node_modules") if d else ""
            if nm and os.path.isdir(nm):
                env["NODE_PATH"] = os.pathsep.join(
                    x for x in (env.get("NODE_PATH", ""), nm) if x)
                break
        # 代理变量可能指向坏代理，直接绕过
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = env.pop("NO_PROXY", "*") or "*"
        try:
            p = subprocess.run(
                [node, self._runner_path(), self._cfg_path(), json.dumps(job)],
                capture_output=True, timeout=90, env=env,
                text=True, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            log(f"  ✗ {self.name}：runner 超时（90s）")
            return None
        except Exception as exc:  # noqa: BLE001
            log(f"  ✗ {self.name}：runner 启动失败：{exc}")
            return None
        line = ""
        for l in (p.stdout or "").strip().splitlines():
            l = l.strip()
            if l.startswith("{"):
                line = l  # 取最后一行 JSON
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception:  # noqa: BLE001
            return None

    def _cfg_path(self) -> str:
        if getattr(self, "_cfg_file", None):
            return self._cfg_file
        import tempfile
        fd, p = tempfile.mkstemp(prefix="venera_", suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.raw)
        self._cfg_file = p
        return p

    def _probe(self, log) -> bool:
        """info 模式：拿 name/key/explore 条目数。"""
        if self._probed:
            return True
        r = self._run({"mode": "info"}, log)
        if not r or not r.get("ok"):
            log(f"  ✗ {self.name}：配置加载失败（{r.get('error') if r else '无输出'}）")
            return False
        self.name = r.get("name") or self.name
        self.source_id = f"venera:{r.get('key') or self.name}"
        self._entries = int(r.get("entries") or 0)
        self._probed = True
        return self._entries > 0

    def __del__(self):
        try:
            if getattr(self, "_cfg_file", None) and os.path.isfile(self._cfg_file):
                os.remove(self._cfg_file)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 遍历 ----------
    def explore(self, pages: int, log, progress=None) -> list[TraversedItem]:
        if not self._probe(log):
            if not self._probed:
                return []
            log(f"  跳过：{self.name} 无 explore 条目")
            return []
        unlimited = pages <= 0
        found: dict[str, TraversedItem] = {}
        done_pages = 0
        for entry in range(self._entries):
            page = 0
            stale = 0
            while True:
                page += 1
                if not unlimited and page > pages:
                    break
                if unlimited and page > self._MAX_PAGES_UNLIMITED:
                    log(f"  {self.name} entry{entry} 达 {self._MAX_PAGES_UNLIMITED} 页上限，停止")
                    break
                r = self._run({"mode": "explore", "entry": entry, "page": page}, log)
                if not r or not r.get("ok"):
                    log(f"  ✗ {self.name} entry{entry} 第 {page} 页：{r.get('error') if r else '无输出'}")
                    break
                comics = r.get("comics") or []
                fresh = 0
                fp = []
                for c in comics:
                    t = (c.get("title") or "").strip()
                    cover = (c.get("cover") or "").strip()
                    cid = (c.get("id") or "").strip()
                    if not t or not cover:
                        continue
                    key = cid or t
                    fp.append(key)
                    if key in found:
                        continue
                    found[key] = TraversedItem(
                        sourceId=self.source_id, itemId=cid or t, itemType=self.item_type,
                        title=t, cover=cover, sourceName=self.name,
                    )
                    fresh += 1
                done_pages += 1
                if progress:
                    try:
                        progress(done_pages, 0, len(found))
                    except Exception:  # noqa: BLE001
                        pass
                log(f"  {self.name} entry{entry} 第 {page} 页：+{fresh}")
                max_page = int(r.get("maxPage") or 0)
                if not comics or not fp:
                    break  # 空页 = 到头了
                if fresh == 0:
                    stale += 1
                    if stale >= 2:  # 连续 2 页全是旧内容 = 翻页失效
                        log(f"  {self.name} entry{entry} 连续 2 页无新内容，停止")
                        break
                else:
                    stale = 0
                if max_page > 0 and page >= max_page:
                    break
        return list(found.values())


def load_sources(text: str, item_type: str = "COMIC") -> list[BaseSource]:
    """解析源文本（文件内容 / 网址响应）为源对象列表。"""
    raw = text.strip()
    # Venera JS 源（extends ComicSource）：非 JSON，先识别再尝试 json.loads
    if "extends ComicSource" in raw[:2000]:
        return [VeneraSource(raw, item_type)]
    root = json.loads(raw)
    sources: list[BaseSource] = []
    if isinstance(root, dict) and isinstance(root.get("api_site"), dict):
        for key, val in root["api_site"].items():
            if isinstance(val, dict) and val.get("api"):
                sources.append(MacCmsSource(
                    name=(val.get("name") or key).strip(), api=val["api"].strip(),
                    detail=(val.get("detail") or "").strip(),
                ))
            elif isinstance(val, str) and val.startswith("http"):
                sources.append(MacCmsSource(name=key.strip(), api=val.strip()))
        return sources
    if isinstance(root, dict):
        # v1.8.1.8：App 备份里 MoonTV 源是单站点配置 {key,name,api,detail}
        if root.get("api") and str(root.get("api", "")).startswith("http"):
            return [MacCmsSource(
                name=(root.get("name") or root.get("key") or "采集站").strip(),
                api=str(root["api"]).strip(),
                detail=str(root.get("detail") or "").strip(),
            )]
        root = [root]
    if not isinstance(root, list):
        return sources
    for obj in root:
        if not isinstance(obj, dict):
            continue
        if "bookSourceUrl" in obj or "ruleExplore" in obj or "ruleSearch" in obj:
            sources.append(LegadoSource(obj, item_type))
        elif "baseUrl" in obj and "search" in obj:
            sources.append(AppJsonSource(obj, item_type))
    return sources


def load_sources_from_text_or_url(spec: str, item_type: str, log) -> list[BaseSource]:
    """装载一个书源入口：URL（http 开头）或本地文件路径。"""
    spec = spec.strip()
    if spec.startswith("http"):
        log(f"下载书源：{spec}")
        text = fetch_text(spec)
    else:
        text = open(spec, encoding="utf-8").read()
    return load_sources(text, item_type)
