#!/usr/bin/env python3
"""把一份打标表发布成云端版本（App「设置 → 标签 → 标签导入 → 云端」读的那份）。

用法：
    python publish_tags.py 新导出的打标表.json
    python publish_tags.py 新表.json --keep 30      # 归档保留最近 30 份

做的事：
1. 校验结构（必须带 items）；
2. 紧凑化写入 `data/tag_table.json`（App 默认下载地址，JSON 缩进对解析无用，紧凑能省近一半流量）；
3. 同样内容 gzip 归档到 `data/archive/tag_table-<时间戳>.json.gz`；
4. 重写 `data/manifest.json`（条数 / 生成时间 / sha256 / 各类型分布 / 归档列表）；
5. 清理超出 --keep 的旧归档；
6. 打印接下来要执行的 git 命令。

更新流程就是「跑这个脚本 + push」，App 侧不需要发新版本 ——
App 只认 manifest.json 这个固定地址，读完发现 generatedAt 比本地新才会去下载大文件。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
ARCHIVE = os.path.join(DATA, "archive")


def fail(msg: str) -> "None":
    print(f"[x] {msg}")
    sys.exit(1)


def summarize(items: list[dict]) -> dict:
    types: dict[str, int] = {}
    for item in items:
        key = item.get("itemType") or "?"
        types[key] = types.get(key, 0) + 1
    return {
        "count": len(items),
        "taggedCount": sum(1 for i in items if i.get("tags")),
        "types": types,
        "sources": len({i.get("sourceId") for i in items}),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="发布打标表到 data/tag_table.json")
    ap.add_argument("input", help="打标表 JSON 路径（App 导出 / PC 工具产出）")
    ap.add_argument("--keep", type=int, default=20, help="归档保留份数（默认 20）")
    ap.add_argument("--no-push", action="store_true", help="只落盘，不打印 git 提示")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        fail(f"找不到输入文件：{args.input}")

    raw = open(args.input, "rb").read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        fail(f"不是合法 JSON：{exc}")

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        fail("缺少 items 或 items 为空 —— 这不是一份打标表")

    os.makedirs(ARCHIVE, exist_ok=True)

    # 1) 紧凑化（App 侧解析不受影响）
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    latest_path = os.path.join(DATA, "tag_table.json")
    with open(latest_path, "wb") as f:
        f.write(compact)

    # 2) 归档
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    arc_name = f"tag_table-{stamp}.json.gz"
    arc_path = os.path.join(ARCHIVE, arc_name)
    with gzip.open(arc_path, "wb", compresslevel=9) as f:
        f.write(compact)

    # 3) manifest（归档名倒序，只列保留下来的部分）
    stats = summarize(items)
    archives = sorted(
        (n for n in os.listdir(ARCHIVE) if n.endswith(".json.gz")),
        reverse=True,
    )
    kept = archives[: args.keep]
    for old in archives[args.keep:]:
        os.remove(os.path.join(ARCHIVE, old))
    manifest = {
        "latest": "tag_table.json",
        "generatedAt": payload.get("generatedAt"),
        "updatedAt": int(_dt.datetime.now().timestamp() * 1000),
        "bytes": len(compact),
        "sha256": hashlib.sha256(compact).hexdigest(),
        "archive": [f"archive/{n}" for n in kept],
        **stats,
        "note": "App 设置 → 标签 → 标签导入 → 云端，先读本文件判断版本，再决定是否下载 tag_table.json",
    }
    with open(os.path.join(DATA, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"[ok] 写入 {os.path.relpath(latest_path, ROOT)}  {len(compact):,} 字节")
    print(f"[ok] 归档 {os.path.relpath(arc_path, ROOT)}  {os.path.getsize(arc_path):,} 字节")
    print(f"[ok] 条目 {stats['count']:,}（有标签 {stats['taggedCount']:,}）  类型 {stats['types']}  源 {stats['sources']} 个")
    print(f"[ok] manifest 已更新，归档共 {len(kept)} 份")

    if not args.no_push:
        print("\n接下来：")
        print("  git add data && git commit -m \"data: 更新打标表 %s\"" % stamp)
        print("  git push origin main")
        print("  之后 App 里点「云端」即可导入（jsDelivr 有分钟级缓存，等 1~2 分钟）")


if __name__ == "__main__":
    main()
