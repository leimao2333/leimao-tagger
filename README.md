# leimao-tagger · 磊毛阅读 PC 端打标工具

[磊毛阅读](https://github.com/leimao2333/leimao-reader)（Android 聚合阅读 App）的
PC 端封面打标工具：WD14 ONNX 端侧推理 + 书源直连遍历 + 标题元数据解析，
产出的打标表推到本仓库 `data/tag_table.json`，
App 端「设置 → 标签 → 标签导入」（默认 jsDelivr 指向本仓库）即可把打标成果
幂等合并进本地标签层（同名标签取更高置信度，站方证据不会被覆盖）。

## 功能

- **独立打标，零依赖 App**：只填**书源**即可完整跑通（遍历源站 → 标题解析 →
  WD14 封面打标 → 输出 / 推送 GitHub）。支持三类源：
  - **legado / shuyuan 书源**（`bookSourceUrl` + `exploreUrl` + `ruleExplore`，即 xiu2/yuedu 的 shuyuan）；
  - **App JSON 源规范**（`{name, baseUrl, search:{url, list, fields}}`）；
  - **MoonTV / MacCMS 采集站**（单站点配置或 `{api_site: {...}}`，按视频收录）。

  JS 源 / Venera 源无法在 PC 端评估，遍历时跳过并记录日志。规则引擎为精简实现
  （JSONPath 子集 + `selector@attr` / `class.x` / `tag.x` 链式选择 + `{{page}}` 渲染）。
- **App 备份直读**：收录清单处可直接选 App「备份与恢复」导出的 JSON —— 自动识别，
  从阅读进度抢救条目（含封面），并把备份里的 legado / MoonTV 源取出来直接遍历。
- **网络清单**：拉取 GitHub 云端打标表作为基底，与本地清单 / 书源遍历结果合并。
- **标题解析打标**：参考 [kawayiYokami/MangaReader](https://github.com/kawayiYokami/MangaReader)
  的 `metadata_parser`，从书名解析 `作者 / 汉化组 / 作品 / 会场 / 平台 / 其他`
  元数据标签（识别 `[汉化组 (作者)] (C97) [作品名] 标题 [个人汉化]` 惯用命名），
  以「文本」来源、置信度 60 入库。
- **原生窗口**：运行后打开原生窗口（WebView2），带文件 / 目录选择对话框；
  无 WebView2 的环境回退浏览器。模型（约 356MB）首次自动从 hf-mirror 下载，实时进度。

## 文件

| 文件 | 说明 |
|---|---|
| `pc_tagger.py` | 核心逻辑 + CLI（Python 3.10+） |
| `pc_tagger_gui.py` | **窗口版**：原生窗口 + 文件/目录选择，实时进度 / 各源进度表 / 日志，可中途停止 |
| `source_engine.py` | 网络源遍历引擎：legado 书源 / App JSON 源 / MacCMS 采集站的精简规则引擎 |
| `gen_vocab.py` | 从 App 源码 `TagAlias.kt` 生成受控词表 `vocab.json`（词表改动后重跑） |
| `vocab.json` | 受控词表（danbooru/english → 中文规范名），PC 端映射口径 |
| `data/tag_table.json` | 云端打标表（App「标签导入」的默认地址） |
| `data/manifest.json` | 云端版本清单：条数 / 生成时间 / sha256 / 各类型分布 / 归档列表 |
| `data/archive/` | 历次发布归档（gzip，默认保留最近 20 份） |
| `publish_tags.py` | **发布脚本**：把一份打标表变成云端最新版（紧凑化 + 归档 + 写 manifest） |
| `requirements.txt` | Python 依赖 |

## 更新云端标签表（App 不用发版）

App 的「设置 → 标签 → 标签导入 → 云端」读的是 `data/manifest.json` 这个**固定地址**：
先拿清单（几 KB）比对版本，只有云端比本地新才去下载 11MB 的表。所以更新数据完全不用改 App、
不用发 Release，只要把新数据推上去：

```bash
# 1. 用新导出的打标表更新云端（自动紧凑化 + 归档 + 重写 manifest）
python publish_tags.py 新导出的表.json

# 2. 推上去
git add data && git commit -m "data: 更新打标表" && git push origin main

# 3. 清 jsDelivr 缓存（必须，否则 App 可能连续几天读到旧内容）
python publish_tags.py --purge
```

之后在 App 里点「云端 → 导入」即可，**不需要等**。

- **为什么 latest 是 `tag_table-<时间戳>.json` 而不是固定名**：jsDelivr 按 URL 缓存最长 7 天
  （`Cache-Control: max-age=604800`），而且**忽略 `?t=` 这类 query 参数**。如果一直覆盖同一个
  `tag_table.json`，实测刚推完 App 仍读到占位空表，界面显示「云端 0 条」。换成新文件名后，新 URL
  没有旧缓存可命中，推完立刻生效。`data/tag_table.json` 仍同步覆盖，只为兼容只认固定名的旧版 App。
- 固定名那两个文件（`manifest.json` / `tag_table.json`）更新后必须 `--purge`，否则 CDN 还给旧内容。
- App 下载后会拿清单里的 `count` 校验条数，不符就报「可能是 CDN 缓存还没刷新」，不会静默导入空表。
- `--keep N`：归档保留份数（默认 20），超出自动删最旧的；带时间戳的表只留最近 2 份，避免仓库每次多 11MB。
- 也可以直接让 PC 工具带 `--push` 推送，效果等价（脚本只是把同样的几步做成一条命令）。

## 使用

### exe（推荐）

从 [Releases](https://github.com/leimao2333/leimao-tagger/releases) 下载
`leimao-pc-tagger.exe`，双击运行 —— 词表已内置，单文件即用（外置同名 `vocab.json`
优先，可自定义映射口径）。

1. 「书源导入」填书源文件 / 网址（多个用 `|` 分隔），或「收录清单」选 App 备份 / 打标表；
2. 点「开始遍历打标」；「输出文件」「模型目录」留空即用 exe 同目录默认值；
3. 勾选「打标后推送 GitHub」可直接更新云端表（Token 可选，留空用环境变量 `GH_TOKEN`）。

### CLI

```bash
pip install -r requirements.txt

# 直接用 App 备份文件（自动识别内嵌源 + 进度条目）
python pc_tagger.py --list leimao-backup.json --out tag_table.json

# 或只用书源文件遍历
python pc_tagger.py --sources shuyuan.json --src-type COMIC --src-pages 2

# 推送到本仓库（App 端「标签导入」即可拉取）
python pc_tagger.py --cloud --out tag_table.json --push --token ghp_xxx
```

参数：`--cloud [URL]` 网络清单；`--limit N` 本轮最多打标条数；`--workers N` 封面下载并发；
`--model-dir` 模型缓存目录；`--vocab` 词表路径；`--token` 也可用环境变量 `GH_TOKEN`。

## 打包 exe

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name leimao-pc-tagger ^
  --add-data "vocab.json;." --collect-all webview pc_tagger_gui.py
# 产物 dist/leimao-pc-tagger.exe
```
