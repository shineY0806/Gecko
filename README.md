<p align="center">
  <img src="assets/gecko_256.png" width="120" alt="Gecko">
</p>

<h1 align="center">Gecko</h1>

<p align="center">
  单文件内核的实战型爬虫工具 · 图形界面 + 命令行双用法 · 无需安装数据库或框架
</p>

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

## 使用声明

本工具不限制目标范围，也不替使用者判断边界 —— 它只提供抓取能力。

请你自行确保：**只对你拥有、或已获得明确授权的目标使用**，遵守所在地法律法规与
目标站点的服务条款、`robots.txt` 与速率要求。使用本工具所产生的一切后果由使用者承担。

## 许可

[MIT](LICENSE) © 2026 shineY0806
