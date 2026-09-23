# Gecko

Gecko 是一款具备**实战能力**的通用爬虫工具：不只是靶场演练，也能在真实网站上抓到有效数据。
它帮你摸清站点结构（页面 / 表单 / JS / CSS / 隐藏注释 / 敏感路径），并把数据抽取成
CSV / JSON / SQLite / XLSX / Markdown 可直接使用的形态。

**单引擎设计**：所有能力（并发调度、每域名并发、自适应限速、断点续爬、响应缓存、
浏览器渲染与反检测、自定义字段提取、自愈选择器、接口捕获）都实现在自研引擎内，
不依赖任何第三方爬虫框架，复制整个文件夹即可换机器使用。完整新手向说明见 `使用说明.md`。

> ⚠️ **使用声明**：工具本身不对目标站点设置限制与边界。使用即视为你**自愿遵守**
> 目标站点的服务条款与 robots 约定、所在地法律法规，以及数据使用与传播的合规要求。
> 因使用本工具产生的后果由使用者承担。

## 项目结构

```
Gecko/
├─ gecko/
│  ├─ core/        引擎：range_crawler.py（主引擎）、smart_extract.py（结构化提取与导出）、
│  │               adaptive.py（自愈选择器）、http_backend.py（HTTP 后端与指纹）、
│  │               anticrawl.py（反爬对抗工具箱）、_compat_css.py（老内核兜底样式）
│  └─ ui/          界面：webui.py（本地服务）、webui.html、apple.css
├─ assets/         壁虎项目标志：gecko_logo.png / gecko.ico / gecko_favicon.png
├─ docs/           文档：README.md、使用说明.md、使用说明书.docx、靶场实战手册
├─ tools/          打包与演示：build_exe.py、demo_server.py、演示靶场启动脚本
├─ 启动 Gecko.bat   （推荐入口，自动建 .venv 装依赖）
├─ 启动Gecko-秒开版.bat（已有打包产物时直接开）
└─ 安装可选增强.bat  （浏览器渲染 / TLS 指纹等可选依赖）
```

## 一、图形界面（一键式，推荐）

双击 **`Gecko.exe`**（约 58MB，已内置浏览器渲染内核），直接弹出独立桌面窗口——**不需要装 Python，也不需要装任何依赖**。

改完代码后重新打包：`python tools/build_exe.py`（产物在 `dist/Gecko/Gecko.exe`）。
没有 Python 环境时用脚本启动：双击 `启动Gecko.bat`（自动建 `.venv` 并装依赖，首次 1～3 分钟）。

> **老电脑 / 老浏览器兼容**：界面的全部样式都带了免 CSS 变量的兜底写法（`_compat_css.py` 生成），
> IE11 内核、老 WebView 也能看到完整外观；`apple.css` 同时内嵌进页面，不怕单文件缺失。
> 改了 webui.html / apple.css 的样式后，记得重跑一次 `python _compat_css.py` 重新生成兜底值。

**没有靶场可练手？** 双击 `启动演示靶场.bat`，本地会开一个 `http://127.0.0.1:8000` 的练习站
（含 `spa.html` 用于体验浏览器渲染差异、`robots.txt` 与 `sitemap.xml` 用于体验这两个开关），
把这个地址填进「目标网址」即可。

📘 完整图文版手册：**`Gecko使用说明书.docx`**（零基础可读，含全部参数详解与排错手册）。

在可视化面板里填写目标地址、勾选选项，点「开始爬取」即可实时查看进度、日志和结果。

面板功能：
- 参数配置：深度 / 线程 / 限速 / 页面上限 / Cookie / 排除规则等
- 实时看板：页面、URL、表单、敏感路径、状态码分布、进度条
- 分类结果：页面列表、表单、敏感路径、HTML 注释、站外引用
- 报告文件：在线预览 / 下载 report.json、forms.csv、urls.txt 等
- 运行中可随时「停止」
- 「反爬对抗」页：字体反爬 / 文本解码 / 水印 / 鉴权测试 / JS接口加密 五个工具

也可以手动启动：
```powershell
python webui.py                       # 默认：优先桌面窗口，其次无地址栏应用窗口，最后系统浏览器
python webui.py --window pywebview    # 内嵌 WebView 的独立桌面窗口（无地址栏，最像原生应用）
python webui.py --window app          # Edge/Chrome 的 --app 模式窗口（无地址栏，零额外依赖）
python webui.py --window browser      # 系统默认浏览器打开
python webui.py --window none --port 8790   # 只起服务不弹窗口（远程访问时用）
```

> `--window pywebview` 需要 `pip install pywebview`（会带 pythonnet，约几十 MB）；
> Windows 上依赖系统自带的 WebView2 运行时与 .NET Desktop Runtime（Win10/11 一般已有）。
> 没装也不会出错——会自动降级到 `--window app`。

> **界面风格**：默认使用「素白工业」视觉层 `apple.css`（白底 + 发丝边框 + 中性灰阶，
> 唯一强调色为钢蓝，语义色只用于状态；层次靠留白 / 对齐 / 字号字重 / 细线建立，
> 全部系统字体，断网可用）。样式刻意克制：无渐变、无阴影、无模糊、无动画。
> 它是纯 CSS 覆盖层，不含任何 JS 改动——想回到不带视觉层的裸控制台样式，删掉 `webui.html` 里
> `<link rel="stylesheet" href="/apple.css">` 这一行即可。布局只用 block / inline-block / float，
> 不依赖 flex 的 gap 与 grid，老内核（IE11 / 老 WebView）下两栏依旧并排、不错乱。

## 二、命令行方式

```powershell
cd D:\爬虫工具
python -m pip install -r requirements.txt
```

依赖：`requests`、`beautifulsoup4`（爬虫引擎）；`fonttools`、`pillow`、`numpy`、`brotli`（反爬对抗模块）。一键脚本会自动安装。
**用 exe 的话不需要装任何依赖**；只有想重新打包 exe 时才需要 `pyinstaller`。

## 快速开始

```powershell
# 基本爬取：深度 3 层，5 线程，请求间隔 0.3 秒
python range_crawler.py -u http://127.0.0.1:8000

# 提取 HTML 注释（靶场里常藏着提示）
python range_crawler.py -u http://127.0.0.1:8000 --comments

# 带登录态爬取 + 跳过自签名证书校验
python range_crawler.py -u https://range.example.com --cookie "session=abc123" --no-verify

# 排除某些路径、加大并发、重新爬取
python range_crawler.py -u http://127.0.0.1:8000 --exclude "logout" --threads 10 --fresh

# 抓 JS 动态渲染的页面，并提取指定字段
python range_crawler.py -u http://127.0.0.1:8000 --render dynamic --select "标题=h1" --select "价格=.price"

# 大站分次爬：自适应限速 + 断点续爬（中断后再跑同一条命令即可继续）
python range_crawler.py -u http://127.0.0.1:8000 -d 4 --autothrottle --checkpoint
```

## 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-u, --url` | 必填* | 种子 URL（给了 `--url-file` 时可省略） |
| `--url-file PATH` | 无 | **v3.9** URL 清单文件，一行一个地址；配合 `-d 0` 就是「只抓清单里的地址」，成百上千个 URL 只需启动一次进程 |
| `-d, --depth` | 3 | 爬取深度（BFS 层数） |
| `--threads` | 5 | 并发线程数 |
| `--delay` | 0 | 请求间隔秒数（默认不限速；需要时再设，如 `--delay 0.5`） |
| `--qps N` | 0 | **v3.9** 全局每秒请求数上限（如 `--qps 2`）。不看线程数，直接压住总 QPS——躲风控最有效的一个旋钮 |
| `--max-pages` | 500 | 最多抓取页面数 |
| `--timeout` | 10 | 请求超时（秒） |
| `--retries` | 2 | 失败重试次数（上限 20） |
| `--max-body-mb` | 3 | 单页最多读取多少 MB 后截断（上限 200） |
| `--backend` | auto | HTTP 后端：`auto` 装了 curl_cffi 就用它伪装 Chrome 指纹；`requests` 传统后端；`curl_cffi` 强制浏览器指纹 |
| `--impersonate` | chrome | 浏览器 TLS/HTTP2 指纹目标，如 `safari` / `firefox` / `chrome110`（仅 curl_cffi 后端生效） |
| `--render` | 关 | 浏览器渲染：`dynamic` 真实 Chromium 渲染（可抓 SPA/JS 动态内容）；`stealthy` 反检测浏览器（需 `pip install playwright` + `playwright install chromium`） |
| `--wait-selector` | 空 | 配合 `--render`：等到该 CSS 选择器出现再取页面 |
| `--network-idle` | 关 | 配合 `--render`：等网络空闲再取页面（更完整但更慢） |
| `--no-headless` | 关 | 配合 `--render`：显示浏览器窗口（默认无头） |
| `--per-domain N` | 0 | 每域名最大并发，0=不限（建议 2~5，避免压垮目标） |
| `--autothrottle` | 关 | 自适应限速：按响应时间自动伸缩请求间隔 |
| `--checkpoint` | 关 | 断点续爬：中断后再次运行自动从断点恢复 |
| `--cache` | 关 | 响应缓存：相同请求直接复用上次结果 |
| `--select` | 无 | 自定义字段提取：`--select "标题=h1"`，可多次指定；写 `--select "书名=h3 a@title"` 可**取属性值**（`title` / `href` / `src` / `class` 等），结果写入 items.csv/json |
| `--select-each` | 无 | **[v4.0]** 指定"一条记录"的容器（如 `--select-each "article.product_pod"`），把列表页逐项拆成多行记录。抓商品 / 房源 / 招聘列表必用，否则一页 20 条会挤成一行 |
| `--cookie` | 空 | 携带 Cookie，如登录态 |
| `--method METHOD` | GET | **v3.9** 请求方法，可选 GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS；非 GET 建议配合 `-d 0 --url-file` |
| `--data BODY` | 空 | **v3.9** 请求体（表单串），如 `--data "user=a&pass=b"` |
| `--json JSON` | 空 | **v3.9** 请求体（JSON），自动带 `Content-Type: application/json` |
| `--content-type TYPE` | 空 | **v3.9** 显式指定 Content-Type，覆盖上面两项的默认值 |
| `--same-host` | 关 | **v3.9** 只抓站内：把待爬队列限制在种子主机内（默认会跟随站外链接） |
| `--extra-domain HOST` | 无 | **v3.9** 额外放行的主机（含端口），可多次指定；同机多端口靶场互相引用时用 |
| `--no-verify` | 关 | 跳过 HTTPS 证书校验（自签名靶场） |
| `--subdomains` | 关 | 连子域名一起抓（默认只抓种子所在主机） |
| `--respect-robots` | 关 | 遵守 robots.txt 的 Disallow（默认只收集信息） |
| `--exclude REGEX` | 无 | 排除匹配正则的 URL，可多次指定 |
| `--comments` | 关 | 提取 HTML 注释 |
| `--no-js-urls` | 开 | 关闭「从内联 JS 提取接口/路径」（SPA 站点靠它发现端点） |
| `--no-sitemap` | 开 | 关闭「sitemap.xml 种子发现」 |
| `--ignore-param NAME` | 无 | 额外忽略的 query 参数，可多次指定 |
| `--keep-tracking-params` | 关 | 保留 `utm_*` 等跟踪参数（默认剥离） |
| `-o, --output` | 自动 | 输出目录，默认 `output_<主机名>` |
| `--fresh` | 关 | 忽略上次访问记录，重新爬取 |
| `-v / -q` | — | 详细日志 / 静默模式 |

### v4.0 企业级参数（把"抓到页面"推进到"拿到可用数据"）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--extract` | 关 | 结构化提取：JSON-LD / Microdata / OpenGraph / Meta / 联系方式（邮箱、电话、QQ、微信），每页产出一条宽表记录 |
| `--extract-tables` | 关 | 额外导出页面上的 `<table>` 明细 —— 报价表、参数表、榜单这类企业官网最常见的数据载体 |
| `--export FORMATS` | 无 | 导出格式，逗号分隔：`csv` / `jsonl` / `sqlite` / `xlsx`。含 `sqlite` 时**边跑边落盘**，爬十万页也不撑内存 |
| `--pager N` | 0 | 分页自动扩展：识别 `<link rel=next>` 或"下一页"后顺延 N 页。列表站（商品/房源/资讯）必用 |
| `--incremental` | 关 | 增量抓取：带 `ETag` / `Last-Modified`，站点判定未变更返回 304 就跳过重抓。做日常增量采集时用 |
| `--dedup` | 关 | 按正文指纹去重，内容相同的页面只入库一次（分页重复、日历归档很有效） |
| `--rotate-ua` | 关 | 每次请求随机轮换浏览器 UA（应对 UA 黑名单） |
| `--referer-auto` | 关 | 自动带上站内首页 Referer（应对 Referer 白名单校验） |

### 一条命令抓到可用数据（真实站点示例）

```bat
range_crawler.py -u https://example-shop.com/ -d 2 --same-host ^
  --respect-robots --qps 3 --delay 0.2 ^
  --extract --extract-tables --export csv,sqlite,xlsx ^
  --select-each "article.product_pod" ^
  --select "书名=h3 a@title" --select "价格=.price_color" --select "详情页=h3 a@href"
```

- `--select-each` 指定"一条记录的容器" → 列表页里 20 个商品变成 **20 行**，而不是 1 行里挤 20 个值
- `字段=选择器@属性` → 取的是属性值（`title` / `href` / `src` / `class`），默认取标签文本

## 输出物（都在输出目录里）

| 文件 | 内容 |
|---|---|
| `report.json` | 完整结构化报告：页面、表单、资源、注释、敏感路径、站外引用、robots 信息 |
| `urls.txt` | 所有发现的站内 URL |
| `forms.csv` | 表单清单：页面 / action / method / 输入项名称 |
| `interesting.txt` | 命中敏感关键词的路径（login / admin / upload / api / flag / .git 等） |
| `challenges.txt` | **v3.9** 被风控挑战/拦截的地址（428/429/418，或 401/403/406/503 且正文含挑战词），一眼看出对抗点在哪 |
| `comments.txt` | 提取到的 HTML 注释 |
| `visited.json` | 访问记录，下次运行自动跳过已爬页面（`--fresh` 可强制重爬） |
| `report.md` | 人类可读的汇总报告（参数 + 统计 + 敏感目标 + 页面清单），便于归档对比 |
| `structured.csv` / `structured.jsonl` / `structured.xlsx` | **v4.0** 结构化提取结果（`--extract` + `--export`），每页一行，可直接进 Excel / BI |
| `data.db` | **v4.0** SQLite 数据库（`--export sqlite`），含 `pages` / `items` / `tables` 三张表，边跑边写、适合大规模采集 |
| `tables.csv` | **v4.0** 页面上所有表格的明细（`--extract-tables`） |
| `http_state.json` | **v4.0** 增量抓取的 ETag / Last-Modified 凭据（`--incremental`），下次运行据此跳过未变更页面 |
| `checkpoint.json` | 断点位置（开了 `--checkpoint` 才有，全部爬完自动删除） |

> `.cache/`（响应缓存）也在输出目录里，开了 `--cache` 时生成。

`report.json` 在 v2.0 起额外包含 `meta_links`（canonical / icon / manifest）、`js_extracted`（从内联 JS 提取到的 URL）、`robots_sitemaps`；v3.9 起额外包含 `challenges`（被风控挑战的地址）。

## v3.9 新增用法（靶场实战补的能力）

```powershell
# 批量抓已知地址：清单文件一行一个 URL，只启动一次进程
python range_crawler.py --url-file urls.txt -d 0 --threads 4 --qps 2 --cache -o out

# 压住全局 QPS（风控看的是速率，不是单请求间隔）
python range_crawler.py -u http://127.0.0.1:3000 -d 2 --qps 1 -o out

# 提交表单 / 调用 POST 接口（登录、GraphQL、过 JS 挑战的 verify）
python range_crawler.py -u http://127.0.0.1:3000/api/graphql -d 0 --method POST `
  --json '{"query":"{books{id title}}"}' --cache -o out

# 只抓本站，别跟到外网去
python range_crawler.py -u http://127.0.0.1:3000 -d 3 --same-host -o out
```

> 完整实战案例（怎么侦察、怎么换通道、怎么复盘风控）见 **`星河中文网靶场抓取实战手册.md`**。

## 一分钟自测（内置演示站点）

```powershell
# 终端 1：启动本地演示站点
python -m http.server 8000 --directory D:\爬虫工具\demo_site

# 终端 2：爬它
python range_crawler.py -u http://127.0.0.1:8000 -d 3 --comments
```

演示站点里有普通链接、站外链接、登录/搜索表单、JS/CSS 引用、注释和敏感路径，
跑完可以对照 `report.json`、`forms.csv`、`interesting.txt` 检查效果。

## 三、反爬对抗模块（针对靶场防护）

用于靶场内置的典型防护对抗验证。图形界面入口：面板右上「反爬对抗」；命令行统一入口：

```powershell
python anticrawl.py -m <模块> ...
```

| 模块 | 对付的防护 | 用法示例 |
|---|---|---|
| `font` | **字体反爬**（字形映射混淆） | `python anticrawl.py -m font --url http://靶场/防爬页 --text "&#xE001;&#xE005;&#xE002;"` |
| `decode` | **文本混淆**（实体/转义/URL/Base64/全角） | `python anticrawl.py -m decode --text "\u4F60\u597D %E4%B8%96%E7%95%8C"` |
| `water` | **隐形水印**（LSB/频域检测与清理） | `python anticrawl.py -m water --file a.png --action detect` |
| `auth` | **付费/登录鉴权**（访问控制差分测试） | `python anticrawl.py -m auth --url http://靶场/付费页 --cookie "session=xx"` |
| `js` | **JS逆向接口加密**（扫描/执行/重放） | `python anticrawl.py -m js --url http://靶场/ --js-action scan` |

各模块说明：

- **font**：自动提取页面 `@font-face`（支持 URL 与 base64 内联），下载字体后用字形比对重建「混淆码点→真实字符」映射（PUA 区），再还原正文。`--font-file` 可分析本地字体，`--ref-font` 可指定参考字体。
- **decode**：按「HTML实体 → Unicode转义 → URL编码 → Base64 → NFKC全角/同形字归一化」逐级尝试，输出每一步变化。
- **water**：检测（LSB通道相关性/熵、频域周期峰、分块重复）+ 清理（LSB置零、频域低通、高斯/中值滤波、颜色量化）。启发式，用于靶场验证。
- **auth**：用「匿名 / 携带Cookie / 伪造VIP字段 / 伪造管理员字段 / 伪造来源 / 内网来源头」六种上下文发差分请求，对比状态码与内容指纹，标出疑似越权点。
- **js**：① `scan` 扫描页面 JS 的加密特征（AES/RSA/MD5/Base64/签名/请求封装）与硬编码密钥候选；② `run` 用本机 Node 执行你写的解密/签名脚本（`--script`）；③ `replay` 重放捕获的加密请求列表（`--captures`，JSON 数组 `[{"method":"POST","url":"...","data":{...}}]`）。

**演示素材**（demo_site 内）：`antifont.html`（字体反爬）、`anticrypto.html`（JS接口加密）、`water_test.png`（带LSB水印测试图）。

## 设计上的安全边界

- **默认全速、不限制域名**：请求间隔为 0，发现的外站链接也会被爬取；需要礼貌模式时用 `--delay` 指定间隔。
- **可审计**：每次运行都保存完整访问记录和报告，方便复盘。
- **页面体量控制**：单页只读前 3MB、总页面数有上限，避免内存/带宽失控。
- **仅限授权目标**：本工具默认已放开限速与跨域，请只对你拥有或已获书面授权的目标使用；爬取第三方站点前请自行评估其服务条款与当地法律。

## 常见问题

- **证书报错**：本地靶场多为自签名 HTTPS，加 `--no-verify`。
- **登录后才能看到内容**：先抓登录 Cookie，用 `--cookie` 带上。
- **中文乱码**：报告文件均为 UTF-8；PowerShell 显示乱码属控制台编码问题，文件本身正常。
- **页面抓下来是空的**：内容由 JS 生成，加 `--render dynamic`；首次使用需
  `pip install playwright` + `playwright install chromium`。
- **依赖装了但功能仍缺失**：请用启动脚本运行（它会自动创建并使用项目内的 `.venv`），
  避免依赖装到另一个 Python 解释器里。
