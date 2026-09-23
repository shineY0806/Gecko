<div align="center">

<h1 align="center">🦎 Gecko</h1>

单文件内核的实战型爬虫工具 · 图形界面 + 命令行双用法 · 无需安装数据库或框架

<sub>新手练手项目 · 在 AI 助手协助下完成 · 代码有不成熟之处，多多见谅，欢迎指正</sub>

<kbd>版本 v1.7.1</kbd> <kbd>MIT 许可</kbd> <kbd>Python 3.9+</kbd> <kbd>Windows 可执行版</kbd>

[ **下载 Gecko_v1.7.1_Windows_x64.zip** ](https://github.com/shineY0806/Gecko/releases/latest/download/Gecko_v1.7.1_Windows_x64.zip) · [ 全部版本 ](https://github.com/shineY0806/Gecko/releases) · [ 使用说明 ](docs/使用说明.md)

</div>

---

## 下载

Windows 用户直接下这个，解压后双击 `Gecko.exe` 就能用，**不需要装 Python**：

> 👉 [Gecko_v1.7.1_Windows_x64.zip](https://github.com/shineY0806/Gecko/releases/latest/download/Gecko_v1.7.1_Windows_x64.zip)（60 MB）
> 全部版本见 [Releases](https://github.com/shineY0806/Gecko/releases)

---

## 它是什么

Gecko 是一个自带图形界面的网站抓取工具，核心引擎实现在
[`gecko/core/range_crawler.py`](gecko/core/range_crawler.py) 一个文件里，
不依赖 Scrapy 之类的爬虫框架，复制整个文件夹换台机器就能跑。

能力覆盖从「把整站结构摸一遍」到「按字段把数据抽成表格」：

| 类别 | 能力 |
|---|---|
| 抓取 | 多线程并发、每域名独立并发、深度控制、sitemap 种子、分页自动翻页、断点续爬 |
| 数据 | CSS 选择器自定义字段、`--select-each` 列表抽取、表格识别、结构化导出 CSV / JSONL / SQLite / XLSX / Markdown |
| 反爬对抗 | UA 轮换、Referer 自动补齐、TLS 指纹伪装（curl_cffi）、429/503 退避重试、代理轮换与失败淘汰、浏览器渲染、挑战页识别 |
| 自愈 | 网站改版导致选择器失效时，按元素指纹相似度自动重新定位（`gecko/core/adaptive.py`） |
| 接口发现 | 渲染模式下捕获后台 XHR / fetch 的 JSON 响应，落盘 `xhr_api.json` |
| 效率 | 响应缓存、增量抓取（ETag / 304）、正文去重、自适应限速 |

## 快速开始

### 图形界面（推荐）

```bash
# 源码方式
python gecko/ui/webui.py          # 默认 8765 端口，自动打开窗口

# Windows 双击
启动 Gecko.bat                     # 首次运行会自动建 .venv 装依赖
启动Gecko-秒开版.bat                # 直接跑打包好的 exe，最快
```

### 命令行

```bash
# 抽一本书的标题与价格
python gecko/core/range_crawler.py \
  -u "https://books.toscrape.com/" \
  --select "书名=h3 a@title" --select "价格=.price_color" \
  --select-each "article.product_pod" \
  --export csv,md --pager 3 -o ./out
```

完整参数说明见 [`docs/使用说明.md`](docs/使用说明.md)，
能力对照与设计说明见 [`docs/README.md`](docs/README.md)。

## 目录结构

```
gecko/
  core/     引擎：range_crawler（主引擎）、smart_extract（导出）、
            adaptive（自愈选择器）、http_backend（请求后端）、anticrawl（反爬对抗）
  ui/       界面：webui.py（本地服务）、webui.html、apple.css
docs/       使用说明 / 设计文档 / 实战手册
assets/     壁虎项目标志与多尺寸图标
tools/      打包脚本与演示靶场
```

## 打包成 exe

```bash
python tools/build_exe.py          # 产物在 dist/Gecko/
```

## 版本

| 版本 | 说明 |
|---|---|
| v1.7.1 | 修复媒体 / 图片重复下载：中断后重跑会把已下完的文件再加 `(1)` 存第二份（`final` 由 `.part` 原子改名而来，存在即代表已完整下载成功），现改为直接跳过；新增 `examples/video_grab_demo.py` 视频抓取教学脚本（发现 → 挑选 → 下载 三段式，默认只扫描不下载） |
| v1.7.0 | 新增图片发现与下载（`--image-scan` / `--image-download`）：覆盖 `img@src`、懒加载 `data-src` / `data-original`、`srcset` 最大候选、`<picture><source>`、内联 CSS 背景图、OG / Twitter 分享图，产出 `images.csv` / `images.json`；下载复用大文件链路（流式写盘 + 断点续传）。发现默认关、下载默认关 |
| v1.6.1 | 新增媒体下载能力（`--media-download`）：HLS 分片流自动解析 m3u8 → 并发拉分片 → 合并 mp4；直链大文件走流式写盘 + 断点续传 + SHA256 校验。1GB 实测内存恒定 60MB |
| v1.6.0 | 新增 `--media-scan` 媒体源发现：识别页面里的 m3u8 / mpd / mp4 / webm 等视频音频地址，产出 `media.csv` / `media.json`。**只做发现、不做下载**，拿地址后用什么工具下由使用者决定 |
| v1.5.1 | 修掉自愈选择器在「页面没有目标列表」时把 ▼、箭头、分隔条等装饰元素当成记录抽出的问题；逐条模式抽不到不再回退整页凑数 |
| v1.5.0 | 更名 Gecko；自愈选择器、XHR 接口捕获、代理轮换、Markdown 导出；放开工具层目标限制；修掉分页死锁与 `--pager` 失效 |

版本号同时记录在 `gecko/core/range_crawler.py` 的 `ENGINE_VERSION`，界面与抓取报告里都能看到。
发布流程：改版本号 → 提交 → `git tag -a vX.Y.Z` → `gh release create` 附上打包 zip。

## 关于本项目（写在前面）

> **这是一个新手的练手项目，代码是在 AI 助手的协助下写出来的，多多见谅 🙏**

作者并不是职业程序员，Gecko 从第一行代码起就是边学边做 —— 很多设计是"能用就行"的
土办法，命名、分层、异常处理里大概率还留着不少不成熟的地方，也可能藏着没被测试覆盖到的 bug。

所以：

- **遇到问题请先别急着骂**，能复现的话欢迎直接提 [Issue](https://github.com/shineY0806/Gecko/issues)，
  描述清楚目标站点、参数和操作步骤即可；有能力的朋友更是欢迎 PR
- **AI 生成的代码请带着审视的眼光看**：核心逻辑（尤其是并发、加锁、请求重试这几块）
  建议自己过一遍再上生产环境
- 功能上如果与成熟框架（Scrapy、Scrapling、crawl4ai 等）重叠，那些项目在工程质量上更值得信赖；
  Gecko 的价值在于**开箱即用的图形界面**和**单文件、无框架依赖**这两点

## 使用声明

本工具不限制目标范围，也不替使用者判断边界 —— 它只提供抓取能力。

请你自行确保：**只对你拥有、或已获得明确授权的目标使用**，遵守所在地法律法规与
目标站点的服务条款、`robots.txt` 与速率要求。使用本工具所产生的一切后果由使用者承担。

## 许可

[MIT](LICENSE) © 2026 shineY0806
