#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
range_crawler.py —— Gecko 引擎 v1.5.0（通用实战爬虫）

单引擎设计：全部能力都在本文件内实现，不依赖任何第三方爬虫框架。
实战能力面向真实网站；使用即视为使用者自愿遵守目标站点条款与当地法律法规。

基础能力：
  1. 站点爬取（BFS，可配深度 / 并发 / 限速 / 页面上限）
  2. 表单发现：action / method / input / textarea / select / button
  3. 资源收集：JS / CSS / iframe，以及 canonical / icon / manifest 等 <link> 元信息
  4. HTML 注释提取（--comments，靶场里常藏提示）
  5. 敏感路径标记：login / admin / upload / api / flag / .git / swagger 等
  6. robots.txt 信息收集（--respect-robots 可跳过 Disallow 并采纳 Crawl-delay）
  7. sitemap 种子发现：自动解析 sitemap.xml / sitemap_index（--no-sitemap 关闭）
  8. 内联 JS 中的 URL 提取：让 SPA / 前后端分离站点也能爬到接口（--no-js-urls 关闭）
  9. 多格式报告：report.json / report.md / urls.txt / forms.csv / interesting.txt
     / comments.txt / items.csv / items.json
 10. 跳过已抓页面：visited.json 记录已抓页面，再次运行自动跳过（--fresh 强制重爬）
 11. 代理轮换：--proxy 单代理 / --proxy-file 列表轮换，并对 429/5xx 做退避重试

进阶能力（v3.5 自带，无需额外框架）：
 12. 浏览器指纹伪装：curl_cffi 后端伪装 Chrome/Safari/Firefox 的 TLS 与 HTTP2 指纹
     （--backend curl_cffi --impersonate chrome）
 13. 每域名并发控制：--per-domain N，避免把单个站点压垮
 14. 自适应限速：--autothrottle，按响应时间自动调整请求间隔（站点越慢越保守）
 15. 断点续爬：--checkpoint，中断后再次运行从断点继续，不重复抓取
 16. 响应缓存：--cache，重复调试时直接复用上次结果，不打扰目标站点
 17. 浏览器渲染：--render dynamic|stealthy，用真实 Chromium 渲染 SPA / JS 动态内容，
     stealthy 模式额外做反检测处理（默认关闭，按需开启）
 18. 自定义字段提取：--select "字段名=CSS选择器"，把结构化数据导出成 items.csv/json

v1.5.0 实战增强（对齐 Scrapling 一类现代框架的核心能力）：
 19. 自愈选择器：--no-adaptive 关闭。网站改版导致选择器失配时，按元素指纹
     （标签 / class / id / 属性 / 文本 / 父链）加权相似度自动重定位
 20. XHR 接口捕获：--no-xhr 关闭。渲染模式下把后台 XHR/fetch 的 JSON 响应
     一并留存到 xhr_api.json —— 数据常常根本不在 HTML 里
 21. 代理轮换策略：--proxy-strategy cyclic|random，配合 --no-proxy-eject 可关掉
     失败淘汰；连续失败 3 次的出口自动降权，全部失效时回直连
 22. Markdown 导出：--export 增加 md，产出 LLM/RAG 可直接使用的表格文本

v2.0 相对 v1.x 的关键修正：
  - URL 规范化（参数排序去重、默认端口归一、剥离 utm 等跟踪参数）→ 消除重复抓取与爬虫陷阱
  - 页面预算按「本次实际抓取页数」计算 → 断点续爬不会因历史记录而被判为已满
  - <link> 不再一律当成 CSS，按 rel 分类
  - 连接池大小与并发数对齐 → 消除 "Connection pool is full" 警告
  - 编码探测支持 HTML meta 回退 + GB18030 → 中文站不再乱码
  - 429/503 等按 Retry-After / 指数退避重试
  - 共享状态的更新全部收进同一把锁，事件回调移出锁外

用法示例：
  python range_crawler.py -u http://127.0.0.1:8000 -d 3 --threads 5
  python range_crawler.py -u https://range.example.com --cookie "session=abc" --no-verify
  python range_crawler.py -u http://127.0.0.1:8000 --fresh -o my_output

安全边界：
  - 默认不限速、不限制域名：请求间隔 0，发现的外站链接也会爬取（--delay 可自行加限速）
  - 所有报告与访问记录落盘，便于审计与复盘
  - 工具本身不设目标限制；使用者自愿遵守法律法规与目标站点的服务条款
"""

import argparse
import base64
import csv
import hashlib
import html as html_mod
import json
import os
import queue
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import (
    urljoin, urlparse, urlunparse, urldefrag, parse_qsl, urlencode,
)

try:
    import requests
    from requests.adapters import HTTPAdapter
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "缺少依赖，请先执行: python -m pip install -r requirements.txt\n"
        f"详情: {exc}\n"
    )
    sys.exit(1)

# 可选增强依赖，缺失时自动降级到内置实现，不影响运行
try:
    from w3lib.url import canonicalize_url as _w3_canonicalize
except Exception:  # pragma: no cover
    _w3_canonicalize = None

try:  # lxml 解析更快，缺失时退回 html.parser
    import lxml  # noqa: F401
    BS_PARSER = "lxml"
except Exception:  # pragma: no cover
    BS_PARSER = "html.parser"

# HTTP 后端：requests（默认）或 curl_cffi（浏览器 TLS/HTTP2 指纹伪装）
try:
    import http_backend
except Exception:  # 被当作模块从别处导入时，补上自身所在目录
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import http_backend
    except Exception:  # pragma: no cover
        http_backend = None

# v4.0 结构化提取与多格式导出（见 smart_extract.py），缺失时自动降级为纯链路报告
try:
    import smart_extract
except Exception:  # pragma: no cover
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import smart_extract
    except Exception:
        smart_extract = None

# v1.5.0 自愈选择器（网站改版后按元素指纹自动重定位），纯标准库实现
try:
    import adaptive
except Exception:  # pragma: no cover
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import adaptive
    except Exception:
        adaptive = None

ENGINE_VERSION = "1.5.1"   # 1.5.1：修掉自愈选择器在无目标列表页面上抽出 ▼/装饰元素噪声行的问题；
                           # 1.5.0：更名 Gecko；自愈选择器 / XHR 接口捕获 / 代理轮换策略 / Markdown 导出；放开工具层目标限制

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_UA = "RangeCrawler/1.0 (+authorized-testing-only)"

# 在靶场侦察中通常值得关注的路径关键词（小写匹配）
INTERESTING_PATTERNS = (
    "login", "signin", "auth", "admin", "manage", "console", "dashboard",
    "upload", "file", "download", "api", "ajax", "backup", "config",
    "install", "debug", "flag", "secret", "swagger", "graphql",
    "phpmyadmin", ".git", ".svn", "robots.txt", "sitemap",
)

# 会被当作"非 HTML 页面"跳过解析的常见静态资源后缀
SKIP_PARSE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip",
    ".rar", ".gz", ".tar", ".mp4", ".mp3", ".avi", ".mov",
}

# 危险协议链接（不爬取也不记录为资源）
SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "file:", "ftp:")

# 默认剥离的跟踪/会话类 query 参数（避免同一页面因参数不同被反复抓取）
DEFAULT_IGNORE_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_eid", "spm", "from", "ref", "referrer",
)

# 值得重试的状态码（限流 / 网关暂时性故障）
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

# --rotate-ua 使用的指纹池。刻意混入 Win/Mac、Chrome/Edge/Firefox 多种组合：
# 真实访客本就千差万别，清一色的单一 UA 反而更容易被风控聚类识别。
UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 Edg/122.0.2365.92",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
)

# --pager 页码自动扩展：识别这些查询参数做 N±1 推算。
# 覆盖中英文站最常见的分页命名习惯。
PAGER_PARAMS = ("page", "pageno", "pageindex", "pagenum", "page_id", "p",
                "pn", "currentPage", "ye", "pageNum")

# <link rel="next"> 之外，这些锚文本也视为"下一页"线索
NEXT_TEXT = ("下一页", "下页", "后一页", "next", "next page", "›", "»", ">", "更多")

# v3.9：风控挑战页识别。
# 判定写成纯函数而非靠正文猜：428/429/418 本身就是「先过验证再说」的语义；
# 401/403/406/503 则必须正文同时命中挑战词才认定，避免把普通无权访问也算成挑战。
CHALLENGE_HARD_STATUS = {428, 429, 418}
CHALLENGE_SOFT_STATUS = {401, 403, 406, 503}
CHALLENGE_TOKENS = (
    "challenge", "captcha", "recaptcha", "turnstile", "slider",
    "请输入计算结果", "完成验证", "安全校验", "人机验证", "滑动验证", "滑块",
    "访问验证", "访问过于频繁", "请求过于频繁", "风控", "正在检测",
)

# v3.9：允许通过 --method 指定的请求方法（GET 之外用于表单提交 / 接口调用）
SUPPORTED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# 单页读取默认上限（--max-body-mb 可调），防止把大文件全部拉进内存
DEFAULT_MAX_BODY_MB = 3.0

# 浏览器渲染模式：空串=纯 HTTP；dynamic=真实浏览器；stealthy=反检测浏览器
RENDER_MODES = ("", "dynamic", "stealthy")

# 自适应限速的目标区间：站点响应越慢，请求间隔越大（单位：秒）
AUTOTHROTTLE_MIN_DELAY = 0.0
AUTOTHROTTLE_MAX_DELAY = 5.0
AUTOTHROTTLE_TARGET_LATENCY = 1.0   # 响应时间超过 1 秒就认为站点吃力

# 浏览器渲染时丢弃的资源类型（页面结构与 JS 不受影响，但能显著提速）
RENDER_BLOCK_RESOURCE_TYPES = ("image", "media", "font")

# 反检测注入脚本：抹掉最常见的自动化痕迹（stealthy 模式启用）
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
if (!window.chrome) { window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}}; }
const _perm = navigator.permissions && navigator.permissions.query;
if (_perm) {
  navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _perm(p)
  );
}
"""

# 视为样式表的 rel，以及单独记录的元信息类 rel
STYLE_RELS = {"stylesheet"}
META_LINK_RELS = {"canonical", "icon", "shortcut", "apple-touch-icon",
                 "manifest", "alternate", "preload", "dns-prefetch", "preconnect"}

LOG_HOOK = None  # 可选日志钩子: callable(level: str, message: str)，供 GUI 实时收集日志


def log(msg, level="INFO", verbose=False, quiet=False):
    """简单控制台输出，避免 Windows 控制台编码问题。日志钩子不受 quiet/verbose 影响。"""
    if LOG_HOOK:
        try:
            LOG_HOOK(level, msg)
        except Exception:
            pass
    if quiet and level not in ("WARN", "ERROR"):
        return
    if not verbose and level == "DEBUG":
        return
    try:
        line = f"[{level}] {msg}"
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.write(line.encode("ascii", "replace").decode("ascii") + "\n")


# ---------------------------------------------------------------------------
# URL 工具
# ---------------------------------------------------------------------------

def _canonicalize_fallback(url):
    """内置 URL 规范化：query 参数排序去重、去空值、去锚点。"""
    parts = urlparse(url)
    pairs = parse_qsl(parts.query, keep_blank_values=False)
    pairs.sort()
    return urlunparse((parts.scheme, parts.netloc, parts.path,
                       parts.params, urlencode(pairs), ""))


def canonicalize(url, ignore_params=()):
    """统一 URL 形态，消除「同一页面、不同写法」导致的重复抓取。

    依次完成：去锚点 → 主机名小写 → 默认端口归一（http:80 / https:443）→
    query 参数排序去重 → 剥离跟踪参数。
    优先使用 w3lib（Scrapy 官方 URL 库），缺失时退回内置实现。
    """
    if _w3_canonicalize is not None:
        try:
            url = _w3_canonicalize(url, keep_blank_values=False, keep_fragments=False)
        except Exception:
            url = _canonicalize_fallback(url)
    else:
        url = _canonicalize_fallback(url)

    if ignore_params:
        low_ignore = {p.lower() for p in ignore_params}
        parts = urlparse(url)
        pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                 if k.lower() not in low_ignore]
        pairs.sort()
        url = urlunparse((parts.scheme, parts.netloc, parts.path,
                          parts.params, urlencode(pairs), ""))
    return url


def normalize_url(url, base_url=None, ignore_params=()):
    """规范化 URL：相对路径转绝对、去锚点、统一小写主机、默认端口归一、参数排序去重。"""
    if base_url and url:
        url = urljoin(base_url, url)
    url = (url or "").strip()
    if not url:
        return None
    low = url.lower()
    if any(low.startswith(s) for s in SKIP_SCHEMES):
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    # 去掉 fragment
    url = urldefrag(urlunparse(parts))[0]
    parts = urlparse(url)

    # 归一化主机名（去掉 userinfo、统一小写、去掉默认端口）
    host = parts.netloc.split("@")[-1].lower()
    hostname, port = host, ""
    if ":" in host:
        h, p = host.rsplit(":", 1)
        if p.isdigit():
            hostname = h
            is_default = ((parts.scheme == "http" and p == "80")
                          or (parts.scheme == "https" and p == "443"))
            port = "" if is_default else ":" + p
        else:
            hostname = host
    netloc = hostname.rstrip("/") + port
    path = parts.path or "/"

    url = urlunparse((parts.scheme, netloc, path, parts.params, parts.query, ""))
    return canonicalize(url, ignore_params)


def origin_of(url):
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def in_scope(url, scope_netloc, allow_subdomains, extra_domains=None):
    """判断 URL 是否在爬取范围内（默认仅同主机，用于区分站内/站外）。

    extra_domains：v3.9 新增的「额外放行主机」集合（--extra-domain），
    用于同一台机器上跑着多个端口/多个站点的靶场（如 8000 的种子页指向 3000 的接口）。
    未传时行为与旧版完全一致。
    """
    if not url:
        return False
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.split("@")[-1].lower()
    if host == scope_netloc:
        return True
    if allow_subdomains and host.endswith("." + scope_netloc):
        return True
    if extra_domains:
        for dom in extra_domains:
            dom = str(dom).strip().lower().rstrip("/")
            if not dom:
                continue
            if host == dom or host.endswith("." + dom):
                return True
    return False


def looks_challenged(status, body_text):
    """判断响应是不是「风控挑战页」而不是普通的报错页。

    规则（确定性，不靠猜）：
    - 428（需要先决条件）/ 429（限流）/ 418（我是茶壶，反爬常用）→ 直接判定为挑战；
    - 401/403/406/503 这类本身也可能是「没权限」，必须正文同时命中挑战词才认定。
    """
    try:
        st = int(status)
    except (TypeError, ValueError):
        return False
    if st in CHALLENGE_HARD_STATUS:
        return True
    if st not in CHALLENGE_SOFT_STATUS:
        return False
    low = (body_text or "").lower()
    if not low:
        return False
    return any(tok.lower() in low for tok in CHALLENGE_TOKENS)


# ---------------------------------------------------------------------------
# 编码探测
# ---------------------------------------------------------------------------

META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I)


def detect_encoding(body: bytes, headers) -> str:
    """优先用响应头 charset，其次 HTML <meta charset>，都没给出返回空串。"""
    ct = headers.get("Content-Type") or ""
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        return m.group(1).lower()
    m = META_CHARSET_RE.search(body[:4096])
    if m:
        return m.group(1).decode("ascii", "replace").lower()
    return ""


def decode_body(body: bytes, headers) -> str:
    """按 响应头 → meta → utf-8 → gb18030 的顺序探测解码，最后兜底 replace。"""
    enc = detect_encoding(body, headers)
    candidates = ([enc] if enc else []) + ["utf-8", "gb18030"]
    for cand in candidates:
        try:
            return body.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 页面解析
# ---------------------------------------------------------------------------

# 内联脚本里的字符串常量，常见形态："/api/user/list"、"https://x.y/z"、'a/b?c=1'
JS_STRING_RE = re.compile(r"""(["'`])((?:https?://|/)[^"'`\s<>\\]{2,300})\1""")

# JS 里不太可能是真实路径的噪声（模板占位、转义、纯锚点等）
JS_NOISE_TOKENS = ("%s", "%d", "{}", "${", "\\n", "\\t", "data:", "javascript:")


def _rel_list(tag):
    rel = tag.get("rel")
    if isinstance(rel, (list, tuple)):
        return [str(r).lower() for r in rel]
    if rel:
        return [x.lower() for x in str(rel).split()]
    return []


def looks_like_path(candidate: str) -> bool:
    """过滤从 JS 里提取出来的噪声字符串。"""
    if len(candidate) < 3:
        return False
    if any(tok in candidate for tok in JS_NOISE_TOKENS):
        return False
    path = urlparse(candidate).path
    if path in ("", "/"):
        return False
    # 至少要有「路径层级」或「文件后缀」之一，避免把 "/" 类的片段也塞进队列
    return path.count("/") >= 1


def extract_js_urls(script_text: str, page_url: str, ignore_params=()):
    """从内联 JS 中提取可能指向接口/页面的 URL（SPA 站点的主要链接来源）。"""
    if not script_text or len(script_text) > 512 * 1024:
        return set()
    out = set()
    for _quote, cand in JS_STRING_RE.findall(script_text):
        cand = cand.strip()
        if not looks_like_path(cand):
            continue
        norm = normalize_url(cand, page_url, ignore_params)
        if norm:
            out.add(norm)
    return out


def parse_page(html_text, page_url, config):
    """解析 HTML，返回链接、表单、资源、元信息、注释、JS 中提取的 URL。"""
    links = set()
    scripts = set()
    styles = set()
    frames = set()
    misc = []          # canonical / icon / manifest 等 <link> 元信息
    forms = []
    comments = []
    js_urls = set()

    soup = BeautifulSoup(html_text, BS_PARSER)

    next_pages = []    # v1.5.0：分页线索（<link rel=next> 与「下一页」锚文本）
    for tag in soup.find_all(["a", "area"]):
        href = tag.get("href")
        if href:
            norm = normalize_url(href, page_url, config.ignore_params)
            if norm:
                links.add(norm)
                text = re.sub(r"\s+", "", tag.get_text() or "").lower()
                if text and any(t in text for t in NEXT_TEXT) and len(text) <= 12:
                    next_pages.append(norm)

    for tag in soup.find_all("script"):
        src = tag.get("src")
        if src:
            norm = normalize_url(src, page_url, config.ignore_params)
            if norm:
                scripts.add(norm)
            continue
        # 内联脚本：提取其中硬编码的接口/路径
        if config.js_urls:
            js_urls |= extract_js_urls(tag.string or tag.get_text() or "",
                                       page_url, config.ignore_params)

    for tag in soup.find_all("link"):
        href = tag.get("href")
        if not href:
            continue
        norm = normalize_url(href, page_url, config.ignore_params)
        if not norm:
            continue
        rels = _rel_list(tag)
        typ = (tag.get("type") or "").lower()
        as_attr = (tag.get("as") or "").lower()
        # 只有真正是样式表才归入 CSS；其余 <link> 记为元信息（canonical 常暴露真实路径）
        if (STYLE_RELS & set(rels)) or (not rels and "css" in typ) \
                or ("preload" in rels and as_attr == "style"):
            styles.add(norm)
        elif (META_LINK_RELS & set(rels)) or not rels:
            misc.append({"url": norm, "rel": ",".join(rels) or "-", "type": typ or "-"})
        # v1.5.0：<link rel="next"> 是最规范的分页线索，优先级高于锚文本
        if "next" in rels:
            next_pages.append(norm)

    for tag in soup.find_all(["iframe", "frame"]):
        src = tag.get("src")
        if src:
            norm = normalize_url(src, page_url, config.ignore_params)
            if norm:
                frames.add(norm)

    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if action:
            norm = normalize_url(action, page_url, config.ignore_params)
            if not norm:
                continue
        else:
            norm = page_url  # 无 action 时提交到当前页
        method = (form.get("method") or "GET").upper()
        inputs = []
        for inp in form.find_all("input"):
            inputs.append({
                "name": inp.get("name"),
                "type": inp.get("type") or "text",
                "value": inp.get("value"),
            })
        for inp in form.find_all(["textarea", "select", "button"]):
            inputs.append({"name": inp.get("name"), "type": inp.name, "value": None})
        forms.append({
            "page": page_url,
            "action": norm,
            "method": method,
            "inputs": [i for i in inputs if i["name"]],
        })

    if config.comments:
        comments = [c.strip() for c in re.findall(r"<!--(.*?)-->", html_text, re.S) if c.strip()]

    return {
        "links": links,
        "scripts": scripts,
        "styles": styles,
        "frames": frames,
        "misc": misc,
        "forms": forms,
        "comments": comments,
        "js_urls": js_urls,
        "next_pages": list(dict.fromkeys(next_pages)),   # v1.5.0 分页线索
    }


def looks_html(headers, url):
    """判断响应是否值得当成 HTML 解析。"""
    ct = (headers.get("Content-Type") or "").lower()
    if ct and "html" in ct:
        return True
    if ct and ("json" in ct or "javascript" in ct):
        return False
    if ct and ("text/" not in ct) and ("xml" not in ct):
        return False
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in SKIP_PARSE_EXT:
        return False
    return True


# ---------------------------------------------------------------------------
# 配置（CLI / GUI / 脚本共用的单一来源）
# ---------------------------------------------------------------------------

@dataclass
class Config:
    seed: str = ""
    scope_netloc: str = ""
    output_dir: str = ""
    depth: int = 3
    threads: int = 5
    delay: float = 0.0
    max_pages: int = 500
    max_body_mb: float = DEFAULT_MAX_BODY_MB
    timeout: float = 10.0
    retries: int = 2
    user_agent: str = DEFAULT_UA
    cookie: str = ""
    proxy: str = ""
    proxy_file: str = ""
    headers: dict = field(default_factory=dict)
    verify: bool = True
    backend: str = "auto"          # auto | requests | curl_cffi
    impersonate: str = ""          # 浏览器指纹名，空串表示由 backend 决定
    subdomains: bool = False
    respect_robots: bool = False
    exclude: list = field(default_factory=list)   # 已编译的正则对象列表
    comments: bool = False
    js_urls: bool = True
    sitemap: bool = True
    ignore_params: tuple = DEFAULT_IGNORE_PARAMS
    fresh: bool = False
    verbose: bool = False
    quiet: bool = False
    started: str = ""

    # —— v3.5 进阶能力 ——
    per_domain: int = 0            # 每域名最大并发，0=不限
    autothrottle: bool = False     # 自适应限速
    checkpoint: bool = False       # 断点续爬
    cache: bool = False            # 响应缓存
    render: str = ""               # "" | dynamic | stealthy
    headless: bool = True          # 浏览器是否无头
    network_idle: bool = False     # 浏览器模式：等到网络空闲
    wait_selector: str = ""        # 浏览器模式：等待该 CSS 选择器出现
    selectors: dict = field(default_factory=dict)  # 自定义字段提取：字段名 -> CSS 选择器
    select_each: str = ""      # v4.0：逐条模式，每页按该容器拆成多行（列表页必备）

    # —— v3.9 靶场实战新增：批量 URL / 跨主机 / 表单提交 / 全局限速 ——
    url_file: str = ""             # URL 清单文件路径
    url_list: list = field(default_factory=list)   # 由 url_file 读入并规范化后的种子
    extra_domains: set = field(default_factory=set)  # 额外放行的主机（含端口）
    same_host: bool = False        # 只抓站内：把待爬队列限制在种子主机（+extra-domain）内
    method: str = "GET"            # 请求方法，非 GET 时配合 --url-file / -d 0 使用
    data: str = ""                 # 请求体（表单串 a=1&b=2）
    json_body: str = ""            # 请求体（JSON 原文）
    content_type: str = ""         # 显式指定 Content-Type
    qps: float = 0.0               # 全局每秒请求数上限，0=不限

    # —— v4.0 企业级：抓到"可用数据"而非只是一堆 HTML ——
    extract: bool = False          # 结构化提取（JSON-LD / Microdata / Meta / 表格 / 联系方式）
    extract_tables: bool = False   # 额外把 <table> 明细导出（tables.csv / data.db）
    export: list = field(default_factory=list)   # 导出格式: csv/jsonl/sqlite/xlsx
    dedup: bool = False            # 正文 SHA1 去重：内容相同的页面只入库一次
    pager: int = 0                 # 分页自动扩展层数（0=关闭）：顺着 rel=next / 页码参数多抓 N 页
    incremental: bool = False      # 增量抓取：带 If-Modified-Since / If-None-Match，304 直接跳过
    rotate_ua: bool = False        # 每次请求随机轮换 UA（应对 UA 黑名单）
    referer_auto: bool = False     # 自动带上来源页 Referer（应对 Referer 校验）

    # —— v1.5.0：自愈选择器 / XHR 捕获 / 代理轮换策略 ——
    adaptive: bool = True          # 自愈选择器：选择器失配时按元素指纹自动重定位
    capture_xhr: bool = True       # 渲染模式捕获后台 XHR/fetch 的 JSON 接口响应
    proxy_strategy: str = "cyclic" # 代理轮换策略：cyclic 顺序轮换 | random 随机
    proxy_fail_eject: bool = True  # 代理连续失败自动降权淘汰（全部淘汰时回直连）

    @property
    def checkpoint_path(self):
        return os.path.join(self.output_dir, "checkpoint.json")

    @property
    def cache_dir(self):
        return os.path.join(self.output_dir, ".cache")


def _parse_headers(raw):
    """把 dict 或「Name: Value」多行文本统一成请求头字典。"""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(k).strip():
                out[str(k).strip()] = str(v)
    elif isinstance(raw, str):
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _num(raw_value, default, lo, hi):
    """读取数值并夹到 [lo, hi] 区间。"""
    try:
        v = float(raw_value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return min(max(v, lo), hi)


def _pick_backend(raw):
    """规范化后端选择。显式要 curl_cffi 但本机没装时，降级为 requests 而不是报错。"""
    v = str(raw or "auto").strip().lower()
    if v in ("curl", "curl_cffi", "curlcffi", "impersonate"):
        try:
            if http_backend and http_backend.available_backends().get("curl_cffi"):
                return "curl_cffi"
        except Exception:
            pass
        return "requests"
    if v in ("requests", "plain"):
        return "requests"
    return "auto"


def _pick_render(raw):
    """规范化渲染模式；未装浏览器依赖时降级为纯 HTTP 而不是报错。"""
    v = str(raw or "").strip().lower()
    if v in ("", "none", "off", "static", "http"):
        return ""
    if v in ("dynamic", "browser", "playwright"):
        return "dynamic"
    if v in ("stealthy", "stealth", "patchright"):
        return "stealthy"
    return ""


def _parse_hosts(raw):
    """把 --extra-domain 的多种输入统一成「主机[:端口]」集合。

    接受：单个字符串、逗号/换行分隔的字符串、列表。
    用户写完整 URL 也能认（自动取 netloc），写纯主机名同样认。
    """
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[\n,;]", str(raw))
    out = set()
    for it in items:
        it = it.strip().rstrip("/")
        if not it or it.startswith("#"):
            continue
        if "://" in it:
            host = (urlparse(it).netloc or "").split("@")[-1].lower()
        else:
            host = it.lower()
        if host:
            out.add(host)
    return out


VALID_EXPORTS = ("csv", "jsonl", "sqlite", "xlsx", "md")


def _parse_export(raw):
    """把 --export csv,sqlite,xlsx 解析成格式列表。

    未知格式直接丢弃并提示，而不是在收尾时才崩 —— 参数错误应该在第一秒就暴露。
    GUI 传列表、CLI 传 "csv,sqlite" 或 ["csv","xlsx"] 都支持。
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[\n,; ]", str(raw))
    out, bad = [], []
    for it in items:
        v = it.strip().lower()
        if not v:
            continue
        if v in VALID_EXPORTS:
            if v not in out:
                out.append(v)
        else:
            bad.append(v)
    if bad:
        log(f"忽略未知的导出格式: {','.join(bad)}（可选: {'/'.join(VALID_EXPORTS)}）", "WARN")
    return out


def _pick_proxy_strategy(raw):
    """代理轮换策略；不认识的写法一律按 cyclic 处理。"""
    v = str(raw or "cyclic").strip().lower()
    return v if v in ("cyclic", "random") else "cyclic"


def _pick_method(raw):
    """规范化请求方法；不认识的方法一律回退 GET，避免请求发不出去。"""
    v = str(raw or "GET").strip().upper()
    return v if v in SUPPORTED_METHODS else "GET"


# 选择器末尾的 @属性 语法。属性名只能是合法标识符，避免误伤含 @ 的 CSS（如属性选择器）
RE_SELECTOR_ATTR = re.compile(r"^(.*[^\s])@([A-Za-z_:][-A-Za-z0-9_:.]*)$")


def _attr_value(node, attr):
    """读属性值。注意 class 这类多值属性返回的是列表，
    直接 str() 会得到 "['star-rating', 'Three']" 这种 Python 痕迹，需拼成空格串。"""
    v = node.get(attr, "")
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v).strip()
    return str(v or "").strip()


def _split_selector(spec):
    """把 "h3 a@title" 拆成 ("h3 a", "title")；没写 @ 时属性为空（表示取文本）。"""
    spec = str(spec or "").strip()
    m = RE_SELECTOR_ATTR.match(spec)
    if m:
        return m.group(1).strip(), m.group(2)
    return spec, ""


def _parse_selectors(raw):
    """把「字段名=CSS选择器」解析成 dict。

    接受三种输入：dict、单行字符串、多行/多元素列表（CLI 的 --select 可重复指定）。
    """
    out = {}
    if isinstance(raw, dict):
        items = [f"{k}={v}" for k, v in raw.items()]
    elif isinstance(raw, str):
        items = [ln for ln in re.split(r"[\n;]", raw)]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        items = []
    for it in items:
        it = str(it).strip()
        if not it or "=" not in it:
            continue
        name, sel = it.split("=", 1)
        name, sel = name.strip(), sel.strip()
        if name and sel:
            out[name] = sel
    return out


def _read_url_file(path, ignore_params=()):
    """读取 URL 清单文件（v3.9）。

    格式：一行一个 URL；空行与 # 开头的行跳过；行内 # 之后的内容当注释剔除。
    返回 (URL 列表, 错误信息)。
    """
    if not path:
        return [], None
    if not os.path.exists(path):
        return [], f"URL 清单文件不存在: {path}"
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:   # utf-8-sig 兼容 BOM
            raw_lines = fh.read().splitlines()
    except OSError as exc:
        return [], f"URL 清单文件读取失败: {path} -> {exc}"

    out = []
    for ln in raw_lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        ln = ln.split(" #", 1)[0].strip()
        if not ln:
            continue
        url = normalize_url(ln, ignore_params=ignore_params)
        if url and url not in out:
            out.append(url)
    return out, None


def build_config(src):
    """从任意 dict（CLI 参数 / GUI 表单 / 脚本调用）构造并校验 Config。

    返回 (Config, error_message)；error 非空表示校验失败，此时 Config 为 None。
    CLI 与 GUI 共用此函数，避免两处各自拼装参数导致行为漂移。
    """
    if isinstance(src, Config):
        return src, None

    # v3.9：URL 清单（--url-file）—— 批量抓取成百上千个已知地址时，
    # 不必再为每个 URL 单独启动一次进程。给了清单就不强制要求 -u。
    ignore_params = tuple(DEFAULT_IGNORE_PARAMS)   # 下面可能被覆盖，这里先取默认值
    url_list, url_err = _read_url_file(str(src.get("url_file") or "").strip(),
                                       ignore_params)
    if url_err:
        return None, url_err

    seed_raw = str(src.get("url") or src.get("seed") or "").strip()
    seed = normalize_url(seed_raw, ignore_params=ignore_params) if seed_raw else ""
    if not seed and url_list:
        seed = url_list[0]              # 只给清单时，用第一条定为目标主机
    if not seed:
        return None, ("请填写种子 URL（-u）或 URL 清单文件（--url-file），"
                      "例如 http://127.0.0.1:8000")

    netloc = urlparse(seed).netloc.split("@")[-1].lower()

    # 先校验排除正则，再创建输出目录：避免校验失败时留下空的垃圾目录
    exclude_raw = src.get("exclude") or []
    if isinstance(exclude_raw, str):
        exclude_raw = re.split(r"[\n,]", exclude_raw)
    exclude = []
    for rx in exclude_raw:
        rx = str(rx).strip()
        if not rx:
            continue
        try:
            exclude.append(re.compile(rx))
        except re.error as exc:
            return None, f"无效的排除正则 '{rx}': {exc}"

    output = str(src.get("output") or src.get("output_dir") or "").strip()
    if not output:
        output = "output_" + re.sub(r"[^0-9a-zA-Z._-]", "_", netloc)
    try:
        os.makedirs(output, exist_ok=True)
    except OSError as exc:
        return None, f"无法创建输出目录 {output}: {exc}"

    ignore_raw = src.get("ignore_params")
    if ignore_raw is None:
        ignore_params = DEFAULT_IGNORE_PARAMS
    elif isinstance(ignore_raw, (list, tuple, set)):
        ignore_params = tuple(str(x).strip().lower() for x in ignore_raw if str(x).strip())
    elif isinstance(ignore_raw, str):
        ignore_params = tuple(x.strip().lower()
                              for x in re.split(r"[\n,]", ignore_raw) if x.strip())
    else:
        ignore_params = ()

    def flag(key, default=False):
        v = src.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(v)

    cfg = Config(
        seed=seed,
        scope_netloc=netloc,
        output_dir=output,
        depth=int(_num(src.get("depth"), 3, 0, 200)),   # 0 = 只抓种子页
        threads=int(_num(src.get("threads"), 5, 1, 256)),
        delay=_num(src.get("delay"), 0, 0, 600),
        max_pages=int(_num(src.get("max_pages"), 500, 1, 10000000)),
        max_body_mb=_num(src.get("max_body_mb"), DEFAULT_MAX_BODY_MB, 1, 200),
        timeout=_num(src.get("timeout"), 10, 1, 600),
        retries=int(_num(src.get("retries"), 2, 0, 20)),
        user_agent=str(src.get("user_agent") or DEFAULT_UA),
        cookie=str(src.get("cookie") or ""),
        proxy=str(src.get("proxy") or ""),
        proxy_file=str(src.get("proxy_file") or ""),
        headers=_parse_headers(src.get("headers")),
        verify=not flag("no_verify", False),
        backend=_pick_backend(src.get("backend")),
        impersonate=str(src.get("impersonate") or "").strip().lower(),
        subdomains=flag("subdomains", False),
        respect_robots=flag("respect_robots", False),
        exclude=exclude,
        comments=flag("comments", False),
        js_urls=not flag("no_js_urls", False),
        sitemap=not flag("no_sitemap", False),
        ignore_params=ignore_params,
        fresh=flag("fresh", False),
        verbose=flag("verbose", False),
        quiet=flag("quiet", False),
        started=datetime.now().isoformat(timespec="seconds"),
        per_domain=int(_num(src.get("per_domain"), 0, 0, 128)),
        autothrottle=flag("autothrottle", False),
        checkpoint=flag("checkpoint", False),
        cache=flag("cache", False),
        render=_pick_render(src.get("render") or src.get("render_mode")),
        headless=not flag("no_headless", False),
        network_idle=flag("network_idle", False),
        wait_selector=str(src.get("wait_selector") or "").strip(),
        selectors=_parse_selectors(src.get("select") or src.get("selectors")),
        select_each=str(src.get("select_each") or "").strip(),
        # v3.9
        url_file=str(src.get("url_file") or "").strip(),
        url_list=url_list,
        extra_domains=_parse_hosts(src.get("extra_domain") or src.get("extra_domains")),
        same_host=flag("same_host", False),
        method=_pick_method(src.get("method")),
        data=str(src.get("data") or ""),
        json_body=str(src.get("json_body") or src.get("json") or ""),
        content_type=str(src.get("content_type") or "").strip(),
        qps=_num(src.get("qps"), 0, 0, 200),
        # v4.0 企业级：结构化提取 + 多格式导出 + 分页/增量/去重 + 指纹轮换
        extract=flag("extract", False),
        extract_tables=flag("extract_tables", False),
        export=_parse_export(src.get("export")),
        dedup=flag("dedup", False),
        pager=int(_num(src.get("pager"), 0, 0, 200)),
        incremental=flag("incremental", False),
        rotate_ua=flag("rotate_ua", False),
        referer_auto=flag("referer_auto", False),
        # v1.5.0：默认开启的两项实战能力。GUI 直接传 adaptive/capture_xhr 布尔值，
        # CLI 用 --no-adaptive / --no-xhr 关闭；两种写法都要生效，故用「与」组合
        adaptive=flag("adaptive", True) and not flag("no_adaptive", False),
        capture_xhr=flag("capture_xhr", True) and not flag("no_xhr", False),
        proxy_strategy=_pick_proxy_strategy(src.get("proxy_strategy")),
        proxy_fail_eject=not flag("no_proxy_eject", False),
    )
    return cfg, None


# ---------------------------------------------------------------------------
# v3.5 进阶能力：响应缓存 / 自适应限速 / 域名并发 / 浏览器渲染
# ---------------------------------------------------------------------------


class CIDict(dict):
    """大小写不敏感的响应头字典。

    为什么要它：requests 保留原始大小写（Content-Type），而 curl_cffi 一律返回小写
    （content-type）。默认后端恰好是 curl_cffi，于是按 "Content-Type" 取值会全部落空 ——
    ETag / Last-Modified 拿不到，增量抓取形同虚设；页面也可能因读不到 charset 而乱码。
    命中缓存时 headers 来自 JSON，同样是普通 dict，一并包一层保证行为一致。
    """

    def get(self, key, default=None):
        v = super().get(key)
        if v is not None:
            return v
        lk = str(key).lower()
        for k, val in self.items():
            if str(k).lower() == lk:
                return val
        return default

    def __contains__(self, key):
        return self.get(key) is not None


class ResponseCache:
    """响应缓存：把「状态码 + 响应头 + 正文」落在 .cache/ 目录里。

    用途：写规则、调参数时会反复跑同一个站点，开缓存后第二次起直接读本地结果，
    既不打扰目标站点，也让调试快很多。想重新抓取就删掉输出目录下的 .cache/。
    """

    def __init__(self, directory, enabled):
        self.dir = directory
        self.enabled = bool(enabled)
        self.hits = 0
        self.misses = 0
        if self.enabled:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as exc:
                log(f"缓存目录创建失败，已关闭缓存: {exc}", "WARN")
                self.enabled = False

    def _file(self, url):
        return os.path.join(self.dir, hashlib.sha1(
            url.encode("utf-8", "ignore")).hexdigest()[:32] + ".json")

    def get(self, url):
        if not self.enabled:
            return None
        path = self._file(url)
        if not os.path.exists(path):
            self.misses += 1
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.hits += 1
            return (data.get("status"), CIDict(data.get("headers") or {}),
                    base64.b64decode(data.get("body_b64") or ""),
                    data.get("final_url") or url)
        except Exception:
            self.misses += 1
            return None

    def put(self, url, status, headers, body, final_url):
        if not self.enabled:
            return
        data = {
            "url": url,
            "status": status,
            "headers": dict(headers or {}),
            "final_url": final_url,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        path = self._file(url)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(path + ".tmp", path)   # 原子替换，避免半截文件
        except OSError:
            pass


class AutoThrottle:
    """自适应限速：按近期响应时间自动伸缩请求间隔。

    站点响应变慢（通常是压力上来了）就放慢，响应快就逐步恢复，
    比写死一个 --delay 更不容易把靶场打挂。
    """

    def __init__(self, enabled, base_delay=0.0):
        self.enabled = bool(enabled)
        self.base = base_delay
        self._lock = threading.Lock()
        self._avg_latency = 0.0
        self._delay = base_delay

    def record(self, latency):
        """记录一次请求的耗时（秒），并据此调整下一次的间隔。"""
        with self._lock:
            self._avg_latency = (latency if self._avg_latency == 0
                                 else self._avg_latency * 0.7 + latency * 0.3)
            if self._avg_latency > AUTOTHROTTLE_TARGET_LATENCY:
                overshoot = self._avg_latency - AUTOTHROTTLE_TARGET_LATENCY
                self._delay = min(AUTOTHROTTLE_MAX_DELAY, self._delay + 0.2 + overshoot)
            else:
                self._delay = max(self.base, self._delay - 0.1)
            if not self.enabled:
                self._delay = self.base

    def wait(self):
        """请求前调用：按当前间隔让出时间片。"""
        with self._lock:
            delay = self._delay
        if delay > 0:
            time.sleep(delay)

    @property
    def current_delay(self):
        with self._lock:
            return self._delay

    @property
    def avg_latency(self):
        with self._lock:
            return self._avg_latency


class GlobalRateLimiter:
    """全局 QPS 闸门（v3.9）：把所有线程的请求总量压到每秒 N 个以内。

    为什么需要它：--delay 是每个请求前各睡一段，并发 8 条线程时实际 QPS 约等于
    线程数 / delay，很容易算错；风控系统看的是 QPS，不是单请求间隔。
    --qps 直接对「全局请求时刻表」排队，不管开多少线程都不会超限。
    """

    def __init__(self, qps):
        self.qps = float(qps or 0)
        self.min_interval = (1.0 / self.qps) if self.qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = 0.0        # 下一个允许发出的时刻（monotonic 秒）

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._next_slot <= now:
                self._next_slot = now + self.min_interval
                return
            target = self._next_slot
            self._next_slot = target + self.min_interval
        # 睡在锁外，避免所有线程串行等待
        time.sleep(max(0.0, target - time.monotonic()))


class DomainLimiter:
    """每域名并发闸门：同一个域名同时最多 N 个请求，0 表示不限。

    爬取往往会发现成百上千个同域 URL，不限并发容易把站点压垮，
    也容易触发对方的限流。设成 2~5 通常就能兼顾速度与礼貌。
    """

    def __init__(self, per_domain):
        self.per_domain = int(per_domain or 0)
        self._lock = threading.Lock()
        self._semaphores = {}

    def acquire(self, url):
        if not self.per_domain:
            return None
        host = (urlparse(url).netloc or "").lower()
        with self._lock:
            sem = self._semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(self.per_domain)
                self._semaphores[host] = sem
        sem.acquire()
        return sem

    @staticmethod
    def release(sem):
        if sem is not None:
            sem.release()


class BrowserRenderer:
    """浏览器渲染器：用真实 Chromium 打开页面，拿到 JS 执行后的 HTML。

    为什么用单线程 + 队列：Playwright 的对象不是线程安全的，直接在工作线程里
    各起一个浏览器既吃内存又容易出错。这里只开一个渲染线程统一处理，
    工作线程把 URL 丢进队列等着拿结果，安全且内存可控。
    """

    def __init__(self, config):
        self.config = config
        self.mode = config.render
        self.driver = ""          # 实际生效的驱动名，便于日志排查
        self.error = None
        self.rendered = 0
        self._jobs = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._closed = False
        # v1.5.0：渲染时顺手捡起后台接口响应（数据常常不在 HTML 里）
        self.capture_xhr = bool(getattr(config, "capture_xhr", True))
        self.last_captures = []

    @property
    def enabled(self):
        return bool(self.mode) and self.error is None

    def start(self):
        """启动渲染线程；返回是否可用。不可用时 error 里有原因。"""
        if not self.mode:
            return False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=90):
            self.error = "浏览器启动超时（90s），请检查 Chromium 是否已安装"
        return self.error is None

    def render(self, url):
        """渲染一个 URL，返回与 Fetcher.fetch 同构的四元组。"""
        if not self.enabled:
            return None, None, None, None
        box = {}
        done = threading.Event()

        def _collect(result):
            box["result"] = result
            done.set()

        self._jobs.put((url, _collect))
        if not done.wait(timeout=self.config.timeout + 60):
            return None, None, None, None
        result = box.get("result")
        if result:
            self.rendered += 1
        return result or (None, None, None, None)

    def stop(self):
        if self._thread is None or self._closed:
            return
        self._closed = True
        try:
            self._jobs.put(None)
            self._thread.join(timeout=10)
        except Exception:
            pass

    # ---- 内部：全部运行在同一个渲染线程里 ----

    def _worker(self):
        starter = self._import_driver()
        if starter is None:
            self._ready.set()
            return
        manager = None
        try:
            manager = starter().start()
            browser = manager.chromium.launch(
                headless=self.config.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._ready.set()
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                url, collect = job
                try:
                    collect(self._render_one(browser, url))
                except Exception as exc:
                    log(f"浏览器渲染失败 {url}: {exc}", "WARN", self.config.verbose)
                    collect((None, None, None, None))
        except Exception as exc:
            self.error = f"浏览器启动失败: {exc}"
            self._ready.set()
        finally:
            if manager is not None:
                try:
                    manager.stop()
                except Exception:
                    pass

    def _import_driver(self):
        """stealthy 优先用 patchright（反检测补丁版），没装就用官方 playwright。"""
        try:
            if self.mode == "stealthy":
                try:
                    from patchright.sync_api import sync_playwright
                    self.driver = "patchright"
                except Exception:
                    from playwright.sync_api import sync_playwright
                    self.driver = "playwright(反检测脚本)"
            else:
                from playwright.sync_api import sync_playwright
                self.driver = "playwright"
            return sync_playwright
        except Exception as exc:
            self.error = (f"未安装浏览器依赖（{exc}）。"
                          f"请先执行: python -m pip install playwright && python -m playwright install chromium")
            return None

    def _render_one(self, browser, url):
        cfg = self.config
        ctx_kwargs = {
            "ignore_https_errors": not cfg.verify,
            "user_agent": cfg.user_agent or None,
        }
        if cfg.proxy:
            ctx_kwargs["proxy"] = {"server": cfg.proxy}
        context = browser.new_context(**ctx_kwargs)

        try:
            if cfg.cookie:
                cookies = []
                host = urlparse(url).hostname or ""
                for part in cfg.cookie.split(";"):
                    part = part.strip()
                    if "=" not in part:
                        continue
                    name, value = part.split("=", 1)
                    cookies.append({"name": name.strip(), "value": value.strip(),
                                    "domain": host, "path": "/"})
                if cookies:
                    try:
                        context.add_cookies(cookies)
                    except Exception:
                        pass

            page = context.new_page()
            if self.mode == "stealthy":
                page.add_init_script(STEALTH_INIT_SCRIPT)

            # v1.5.0：捕获后台 XHR / fetch 接口响应。
            # 大量站点的数据由 JS 从接口拉取（HTML 里根本没有），把这些接口连同
            # 响应体留下来，等于直接拿到了结构化数据源。
            seen = []

            def _on_response(resp):
                if not self.capture_xhr:
                    return
                try:
                    rtype = (resp.request.resource_type or "").lower()
                    if rtype not in ("xhr", "fetch"):
                        return
                    ctype = (resp.headers or {}).get("content-type", "").lower()
                    if not any(t in ctype for t in ("json", "javascript", "text", "xml")):
                        return
                    try:
                        body = resp.text()[:20000]
                    except Exception:
                        body = ""
                    if not body:
                        return
                    seen.append({
                        "url": resp.url,
                        "status": resp.status,
                        "resource_type": rtype,
                        "content_type": ctype,
                        "body": body,
                    })
                except Exception:
                    pass

            if self.capture_xhr:
                try:
                    page.on("response", _on_response)
                except Exception:
                    pass

            # 丢掉图片/字体/媒体，只留页面本身需要的东西，渲染快很多
            page.route("**/*", self._route_filter)

            timeout_ms = int(cfg.timeout * 1000)
            response = page.goto(
                url,
                timeout=timeout_ms,
                wait_until="networkidle" if cfg.network_idle else "domcontentloaded",
            )
            if cfg.wait_selector:
                try:
                    page.wait_for_selector(cfg.wait_selector, timeout=timeout_ms)
                except Exception:
                    log(f"等待选择器超时: {cfg.wait_selector} @ {url}",
                        "DEBUG", cfg.verbose)
            elif self.capture_xhr and not cfg.network_idle:
                # 首屏往往在 DOM 就绪后才发接口；留 300ms 捕获窗口，
                # 代价极小却能捡到大部分延迟发出的请求
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            html_text = page.content()
            final_url = page.url
            status = response.status if response is not None else None
            headers = {}
            if response is not None:
                try:
                    headers = dict(response.headers or {})
                except Exception:
                    headers = {}
            headers.setdefault("Content-Type", "text/html; charset=utf-8")
            self.last_captures = seen
            return status, headers, html_text.encode("utf-8"), final_url
        finally:
            try:
                context.close()
            except Exception:
                pass

    def take_captures(self):
        """取走上一次渲染捕获的接口响应（取完清空，避免串到下一个页面）。"""
        caps, self.last_captures = self.last_captures, []
        return caps or []

    @staticmethod
    def _route_filter(route):
        """拦掉图片/媒体/字体，减少无关下载。"""
        try:
            if route.request.resource_type in RENDER_BLOCK_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 抓取器
# ---------------------------------------------------------------------------

class Fetcher:
    """线程安全的 HTTP 抓取器（每线程独立 session，支持代理轮换与退避重试）。"""

    def __init__(self, config):
        self.config = config
        self._local = threading.local()
        self._proxies = []
        self._proxy_lock = threading.Lock()
        self._proxy_index = 0
        self._backend_lock = threading.Lock()
        self._backend_name = None
        # v1.5.0：代理健康度 —— 连续失败即降权淘汰，不让死代理拖慢整体
        self._proxy_fails = {}
        self._proxy_bad = set()
        self._xhr_lock = threading.Lock()
        self._xhr = {}          # url -> [捕获的接口响应]
        self._load_proxies()

        # v3.5：缓存 / 自适应限速 / 域名并发 / 浏览器渲染
        self.cache = ResponseCache(config.cache_dir, config.cache)
        self.throttle = AutoThrottle(config.autothrottle, config.delay)
        self.limiter = DomainLimiter(config.per_domain)
        self.renderer = BrowserRenderer(config)
        # v3.9：全局 QPS 闸门 + 非 GET 请求体
        self.rate = GlobalRateLimiter(config.qps)
        self._json_payload = None
        if config.json_body:
            try:
                self._json_payload = json.loads(config.json_body)
            except (ValueError, TypeError) as exc:
                log(f"--json 不是合法 JSON，将按纯文本发送: {exc}", "WARN", quiet=config.quiet)
                self._json_payload = None

        # v4.0：增量抓取的 HTTP 校验状态（URL -> ETag / Last-Modified）
        self._http_state = {}
        self._state_file = os.path.join(config.output_dir, "http_state.json")
        if config.incremental:
            self._load_http_state()

    def close(self):
        """收尾：保存增量状态 + 关闭浏览器渲染线程（没开渲染时后者是空操作）。"""
        if self.config.incremental and self._http_state:
            try:
                with open(self._state_file, "w", encoding="utf-8") as fh:
                    json.dump(self._http_state, fh, ensure_ascii=False)
            except OSError as exc:
                log(f"增量状态保存失败: {exc}", "WARN", verbose=self.config.verbose)
        self.renderer.stop()

    # ---- v4.0 增量抓取 ----

    def _load_http_state(self):
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r", encoding="utf-8") as fh:
                    self._http_state = json.load(fh)
                log(f"增量模式：加载 {len(self._http_state)} 条校验状态，"
                    f"未变更页面将返回 304 跳过重抓", "INFO", quiet=self.config.quiet)
            except (OSError, json.JSONDecodeError) as exc:
                log(f"http_state.json 读取失败，按全新抓取处理: {exc}",
                    "WARN", verbose=self.config.verbose)

    def _record_state(self, url, headers):
        """记录本页 ETag / Last-Modified，供下次增量请求使用。"""
        etag = (headers or {}).get("ETag") or ""
        lm = (headers or {}).get("Last-Modified") or ""
        if etag or lm:
            self._http_state[url] = {"etag": etag, "last_modified": lm}

    def _revalidated(self, url):
        """返回本次请求应带的协商缓存头；无历史记录时为 None。"""
        st = self._http_state.get(url) or {}
        extra = {}
        if st.get("etag"):
            extra["If-None-Match"] = st["etag"]
        if st.get("last_modified"):
            extra["If-Modified-Since"] = st["last_modified"]
        return extra or None

    def _load_proxies(self):
        """--proxy-file 优先（列表轮换）；否则用 --proxy 单出口。"""
        if self.config.proxy_file:
            path = self.config.proxy_file
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8-sig") as fh:  # utf-8-sig 兼容 BOM
                        lines = [ln.strip() for ln in fh
                                 if ln.strip() and not ln.lstrip().startswith("#")]
                except OSError as exc:
                    log(f"代理文件读取失败: {path} -> {exc}", "WARN", quiet=self.config.quiet)
                    lines = []
                if lines:
                    self._proxies = lines
            else:
                log(f"代理文件不存在: {path}", "WARN", verbose=self.config.verbose)
        elif self.config.proxy:
            self._proxies = [self.config.proxy]

        if self._proxies:
            log(f"代理轮换: 启用 {len(self._proxies)} 个出口（应对 IP 限流）",
                "INFO", quiet=self.config.quiet)

    def _next_proxy(self):
        """按策略取下一个代理；无代理或全部被淘汰时返回 None（直连）。

        cyclic：顺序轮换，请求均匀分布，适合稳定的代理池
        random：随机取，避免被目标按固定序列识别
        """
        if not self._proxies:
            return None
        with self._proxy_lock:
            pool = [p for p in self._proxies if p not in self._proxy_bad]
            if not pool:
                # 全军覆没：宁可直连也不要卡死，同时清空黑名单给它们一次机会
                if self.config.proxy_fail_eject and self._proxy_bad:
                    log("所有代理均被标记为失败，本次回退直连并重置代理池",
                        "WARN", quiet=self.config.quiet)
                    self._proxy_bad.clear()
                    self._proxy_fails.clear()
                pool = list(self._proxies)
            if self.config.proxy_strategy == "random" and len(pool) > 1:
                return random.choice(pool)
            p = pool[self._proxy_index % len(pool)]
            self._proxy_index += 1
            return p

    def _report_proxy(self, proxy_url, ok):
        """反馈一次代理使用结果：成功清零计数，连续失败 3 次即淘汰。

        失败既包括连不上，也包括被目标返回 403/429/503 —— 后者说明这个出口
        在目标眼里已经脏了，继续用只会拉低整体成功率。
        """
        if not proxy_url or not self.config.proxy_fail_eject:
            return
        with self._proxy_lock:
            if ok:
                self._proxy_fails.pop(proxy_url, None)
                return
            n = self._proxy_fails.get(proxy_url, 0) + 1
            self._proxy_fails[proxy_url] = n
            if n >= 3 and proxy_url not in self._proxy_bad:
                self._proxy_bad.add(proxy_url)
                log(f"代理连续失败 {n} 次，已淘汰: {proxy_url}",
                    "WARN", quiet=self.config.quiet)

    def record_xhr(self, url, captures):
        """记下某页捕获到的后台接口响应。"""
        if not captures:
            return
        with self._xhr_lock:
            self._xhr.setdefault(url, []).extend(captures)

    def drain_xhr(self, url):
        if not self._xhr:
            return []
        with self._xhr_lock:
            return self._xhr.pop(url, [])

    def _session(self):
        if not hasattr(self._local, "session"):
            base_headers = {
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            headers = dict(base_headers)
            if self.config.cookie:
                headers["Cookie"] = self.config.cookie
            for k, v in (self.config.headers or {}).items():
                headers[k] = v

            if http_backend is not None:
                # 连接池大小与并发数对齐，避免 "Connection pool is full" 与连接反复重建；
                # max_retries=0：重试由本类统一处理，才能同时做到换代理 + 读 Retry-After。
                # （curl_cffi 的 session 没有 adapters，里面会自动跳过 mount）
                s, backend_name = http_backend.create_session(
                    backend=self.config.backend,
                    impersonate=self.config.impersonate,
                    verify=self.config.verify,
                    headers=headers,
                    pool_size=max(self.config.threads, 10),
                )
            else:  # pragma: no cover - http_backend.py 缺失时的兜底
                s = requests.Session()
                s.verify = self.config.verify
                s.headers.update(headers)
                pool = max(self.config.threads, 10)
                adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                backend_name = "requests"
            self._local.session = s
            # 只在首次创建时播报一次后端，避免每线程刷屏
            with self._backend_lock:
                if self._backend_name is None:
                    self._backend_name = backend_name
                    log(f"HTTP 后端: {backend_name}", "INFO", quiet=self.config.quiet)
        return self._local.session

    def _retry_wait(self, resp_or_headers, attempt):
        """按 Retry-After 退避，没有该头时走指数退避 + 抖动。

        接受 response 或已拷贝的 headers dict —— 响应关闭后仍要读 Retry-After，
        传 dict 更稳妥。
        """
        headers = resp_or_headers.headers if hasattr(resp_or_headers, "headers") \
            else resp_or_headers
        ra = (headers.get("Retry-After") or "").strip()
        if ra:
            try:
                return min(max(float(ra), 0.5), 30.0)
            except ValueError:
                pass
        return min(2 ** attempt + random.uniform(0, 0.8), 15.0)

    def fetch(self, url):
        """统一抓取入口：缓存 -> 浏览器渲染 -> HTTP，顺序短路。

        返回 (status, headers_dict, body_bytes, final_url)；失败返回 (None, None, None, None)。
        """
        cached = self.cache.get(url)
        if cached is not None:
            return cached

        if self.config.render and self._ensure_renderer():
            result = self.renderer.render(url)
            if result[0] is not None:
                try:
                    self.record_xhr(url, self.renderer.take_captures())
                except Exception:
                    pass
                self.cache.put(url, result[0], result[1], result[2], result[3])
                return result
            log(f"浏览器渲染未取到内容，退回 HTTP 抓取: {url}", "DEBUG", self.config.verbose)

        sem = self.limiter.acquire(url)
        try:
            self.rate.wait()              # v3.9：全局 QPS 排队（不限速时是空操作）
            self.throttle.wait()          # 固定间隔或自适应间隔
            started = time.time()
            result = self._fetch_http(url)
            self.throttle.record(time.time() - started)
        finally:
            DomainLimiter.release(sem)

        if result[0] is not None:
            self.cache.put(url, result[0], result[1], result[2], result[3])
        return result

    def fetch_plain(self, url):
        """辅助请求专用（robots.txt / sitemap）：一律走纯 HTTP。

        这类文件是纯文本，用浏览器打开既慢又没必要，所以绕过渲染直连。
        """
        return self._fetch_http(url)

    def _ensure_renderer(self):
        """首次真正要用浏览器时才启动，避免纯 HTTP 抓取白白等一次冷启动。"""
        if self.renderer.error is not None:
            return False
        if self.renderer._thread is None:
            if self.renderer.start():
                log(f"浏览器渲染已就绪（{self.renderer.driver}）", "INFO", quiet=self.config.quiet)
            else:
                log(f"浏览器渲染不可用：{self.renderer.error}", "WARN", self.config.verbose)
        return self.renderer.enabled

    def _fetch_http(self, url):
        """纯 HTTP 抓取（含代理轮换与退避重试）。"""
        session = self._session()
        last_err = None
        for attempt in range(self.config.retries + 1):
            # v4.0：每次独立尝试都换一个 UA，让重试看起来像不同的访客
            if self.config.rotate_ua:
                try:
                    session.headers["User-Agent"] = random.choice(UA_POOL)
                except Exception:
                    pass
            try:
                proxies = None
                proxy_url = self._next_proxy()
                if proxy_url:
                    proxies = {"http": proxy_url, "https": proxy_url}
                # v3.9：统一走 session.request，GET 行为与旧版完全一致；
                # 非 GET 时把 --data / --json 作为请求体带上（表单提交、接口调用、
                # 过 JS 挑战的 verify 接口都靠它）。
                kwargs = {
                    "timeout": self.config.timeout,
                    "verify": self.config.verify,
                    "allow_redirects": True,
                    "stream": True,
                    "proxies": proxies,
                }
                payload_headers = {}
                if self.config.method != "GET":
                    if self._json_payload is not None:
                        kwargs["json"] = self._json_payload
                        payload_headers["Content-Type"] = "application/json"
                    elif self.config.data:
                        kwargs["data"] = self.config.data
                        payload_headers["Content-Type"] = \
                            "application/x-www-form-urlencoded"
                if self.config.content_type:
                    payload_headers["Content-Type"] = self.config.content_type
                # v4.0：Referer 校验是很多站点的第一道门。自动带上站内首页地址，
                # 既满足"必须来自本站"的校验，也不会伪装成某个具体来源页（那属于伪造）。
                if self.config.referer_auto:
                    parts = urlparse(url)
                    payload_headers.setdefault(
                        "Referer", f"{parts.scheme}://{parts.netloc}/")
                # v4.0：增量 —— 带上上次的 ETag / Last-Modified，站点可返回 304 免重传
                if self.config.incremental:
                    for k, v in (self._revalidated(url) or {}).items():
                        payload_headers.setdefault(k, v)
                if payload_headers:
                    kwargs["headers"] = payload_headers
                try:
                    resp = session.request(self.config.method, url, **kwargs)
                except TypeError:
                    # 个别后端不接受 json= 关键字，退回原文发送
                    kwargs.pop("json", None)
                    if self._json_payload is not None:
                        kwargs["data"] = self.config.json_body
                    resp = session.request(self.config.method, url, **kwargs)
                # 只读取前 max_body_mb，防止把大文件全部拉进内存
                limit = int(self.config.max_body_mb * 1024 * 1024)
                if http_backend is not None:
                    body, truncated = http_backend.response_bytes(resp, limit)
                    status = resp.status_code
                    # 两个后端的 headers 生命周期不同，统一拷成普通 dict 再离开作用域；
                    # CIDict 额外抹平 requests(原样大小写) 与 curl_cffi(全小写) 的差异。
                    headers = CIDict(resp.headers)
                    final_url = resp.url
                else:
                    body, truncated = b"", False
                    for chunk in resp.iter_content(chunk_size=65536):
                        body += chunk
                        if len(body) > limit:
                            log(f"页面过大截断: {url} (>{self.config.max_body_mb}MB)",
                                "DEBUG", self.config.verbose)
                            truncated = True
                            break
                    status = resp.status_code
                    headers = CIDict(resp.headers)
                    final_url = resp.url
                if truncated:
                    log(f"页面过大截断: {url} (>{self.config.max_body_mb}MB)",
                        "DEBUG", self.config.verbose)
                http_backend.close_response(resp) if http_backend is not None else resp.close()

                # 增量：站点判定未变更 -> 没有正文，直接返回 304 让上层跳过解析
                if self.config.incremental and status == 304:
                    return status, headers, b"", final_url
                # 增量：记下本次校验凭据，下次请求带上即可触发 304
                if self.config.incremental and 200 <= status < 300:
                    self._record_state(url, headers)

                # 限流/网关类状态码：换代理 + 退避后重试
                if status in RETRY_STATUS and attempt < self.config.retries:
                    wait = self._retry_wait(headers, attempt)
                    log(f"状态码 {status}，{wait:.1f}s 后重试 "
                        f"(第{attempt + 1}次): {url}", "DEBUG", self.config.verbose)
                    self._report_proxy(proxy_url, False)
                    time.sleep(wait)
                    continue
                self._report_proxy(proxy_url, status < 500 and status not in RETRY_STATUS)
                return status, headers, body, final_url
            except requests.exceptions.SSLError as exc:
                last_err = exc
                log(f"SSL 错误(可尝试 --no-verify): {url} -> {exc}", "WARN", self.config.verbose)
                break
            except requests.exceptions.Timeout:
                last_err = "timeout"
                log(f"超时(第{attempt + 1}次): {url}", "DEBUG", self.config.verbose)
            except requests.exceptions.ConnectionError as exc:
                last_err = exc
                log(f"连接错误(第{attempt + 1}次): {url} -> {exc}", "DEBUG", self.config.verbose)
            except requests.exceptions.RequestException as exc:
                last_err = exc
                self._report_proxy(proxy_url, False)
                log(f"请求失败: {url} -> {exc}", "WARN", self.config.verbose)
                break
            except Exception as exc:
                # curl_cffi 抛的是自己的异常体系，不保证继承 requests 的基类，
                # 这里兜底，避免换后端后一次异常就中断整个重试循环
                last_err = exc
                self._report_proxy(proxy_url, False)
                log(f"请求异常({type(exc).__name__}): {url} -> {exc}", "WARN", self.config.verbose)
                break
            time.sleep(self.config.delay * (attempt + 1))
        log(f"抓取失败: {url} ({last_err})", "WARN", self.config.verbose)
        return None, None, None, None


# ---------------------------------------------------------------------------
# 爬虫主逻辑
# ---------------------------------------------------------------------------

class RangeCrawler:
    def __init__(self, config):
        self.config = config
        self.fetcher = Fetcher(config)
        self.visited = set()       # 已抓取过的 URL（落盘为 visited.json）
        self.discovered = set()    # 所有发现过的 URL（含未抓取的）
        self.pages = []            # 已成功抓取的页面记录
        self.forms = []            # 发现的表单
        self.assets = {"scripts": set(), "styles": set(), "frames": set()}
        self.misc_links = []       # canonical / icon / manifest 等
        self.comments = []         # (page, comment)
        self.external = {}         # 站外链接 -> 出现次数
        self.interesting = []      # 命中敏感关键词的 URL
        self.robots = []           # robots.txt 的 Disallow 条目
        self.robots_sitemaps = []  # robots.txt 里声明的 sitemap 地址
        self.robots_crawl_delay = None
        self.sitemap_seeds = []    # 从 sitemap 解析出的种子 URL
        self.js_url_count = 0      # 从内联 JS 中提取出的 URL 数量
        self.js_extracted = set()  # 从内联 JS 中提取出的 URL（仅指字面量，拼接需 JS 执行才能还原）
        self.items = []            # --select 抽取出的结构化数据
        self.challenges = []       # v3.9：被风控挑战/拦截的 URL（428/429/418 或 403+挑战词）
        self.records = []          # v4.0：结构化提取出的宽表记录（每条对应一个页面）
        self.hashes = set()        # v4.0：正文 SHA1 指纹集合（--dedup 去重）
        self.export_files = []     # v4.0：导出器实际写出的文件清单
        self.exporter = None
        self.stats = {
            "http_2xx": 0, "http_3xx": 0, "http_4xx": 0, "http_5xx": 0, "errors": 0,
            "cache_hits": 0, "rendered": 0, "items": 0, "challenged": 0,
            "structured": 0, "duplicates": 0, "not_modified": 0, "tables": 0,
            # v1.5.0
            "healed": 0,   # 自愈选择器重定位成功的次数
            "xhr": 0,      # 渲染模式捕获到的后台接口条数
        }
        self.lock = threading.Lock()
        self.stop_requested = threading.Event()  # GUI 停止信号
        self.on_progress = None                  # 进度回调: callable(event: dict)
        # v1.5.0：自愈选择器（网站改版后自动重定位失配的选择器）
        self.adaptive = None
        if config.adaptive and adaptive is not None:
            try:
                self.adaptive = adaptive.AdaptiveSelector(enabled=True)
            except Exception:
                self.adaptive = None
        self.xhr_captures = []     # v1.5.0：{page, api_url, status, content_type, body}
        self._pager_seeds = []     # v1.5.0：--pager 发现的分页链接（独立于深度推进）

        # v4.0 导出器：sqlite 边跑边写（流式），其余格式收尾统一落盘
        if self.config.export and smart_extract is not None:
            try:
                self.exporter = smart_extract.Exporter(
                    self.config.output_dir, self.config.export, quiet=self.config.quiet)
            except Exception as exc:
                log(f"导出器初始化失败，仍会正常生成报告: {exc}", "WARN", verbose=self.config.verbose)
        elif self.config.export and smart_extract is None:
            log("未找到 smart_extract.py，跳过结构化导出", "WARN", verbose=self.config.verbose)

    # v3.9：站内判定。默认引擎会跟随站外链接（靶场常见多端口互相引用），
    # --same-host 时才用它去过滤待爬队列；无论如何它都决定「站外引用」的统计口径。
    def in_crawl_scope(self, url):
        return in_scope(url, self.config.scope_netloc, self.config.subdomains,
                        self.config.extra_domains)

    # ---- 访问记录 ----

    def load_visited(self):
        path = os.path.join(self.config.output_dir, "visited.json")
        if not self.config.fresh and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.visited = set(data.get("visited", []))
                log(f"加载上次访问记录 {len(self.visited)} 条 (--fresh 可忽略)",
                    "INFO", quiet=self.config.quiet)
            except (OSError, json.JSONDecodeError) as exc:
                log(f"visited.json 读取失败，将重新爬取: {exc}", "WARN", self.config.verbose)

    def save_visited(self):
        path = os.path.join(self.config.output_dir, "visited.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"saved_at": datetime.now().isoformat(), "visited": sorted(self.visited)},
                          fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"保存访问记录失败: {exc}", "WARN", self.config.verbose)

    # ---- 断点续爬 ----

    def _load_checkpoint(self):
        """读取断点。返回 (层号, 待爬队列)；没有断点或种子已变时返回 (0, None)。"""
        if not self.config.checkpoint:
            return 0, None
        path = self.config.checkpoint_path
        if not os.path.exists(path):
            return 0, None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log(f"断点文件读取失败，从头开始: {exc}", "WARN", self.config.verbose)
            return 0, None
        if data.get("seed") != self.config.seed:
            log("断点属于另一个种子地址，忽略并从头开始", "INFO", quiet=self.config.quiet)
            return 0, None
        pending = [u for u in data.get("pending", []) if isinstance(u, str)]
        log(f"发现断点：第 {data.get('level', 0)} 层，待爬 {len(pending)} 个 URL",
            "INFO", quiet=self.config.quiet)
        return int(data.get("level", 0) or 0), pending

    def _save_checkpoint(self, level, pending):
        """保存断点：记录当前层号与还没爬的队列。"""
        if not self.config.checkpoint:
            return
        data = {
            "seed": self.config.seed,
            "level": level,
            "pending": list(dict.fromkeys(pending))[:20000],
            "visited": len(self.visited),
            "crawled": len(self.pages),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        path = self.config.checkpoint_path
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log(f"断点保存失败: {exc}", "WARN", self.config.verbose)

    def _remaining(self, todo, next_level):
        """停止时还没爬到的 URL：本层未处理完的 + 下一层已发现的。"""
        out = []
        for u in list(todo or []) + list(next_level or []):
            if u not in self.visited and u not in out:
                out.append(u)
        return out

    def _clear_checkpoint(self):
        """全部爬完后清掉断点，避免下次运行误以为还要续爬。"""
        if not self.config.checkpoint:
            return
        try:
            if os.path.exists(self.config.checkpoint_path):
                os.remove(self.config.checkpoint_path)
        except OSError:
            pass

    # ---- 自定义字段提取 ----

    @staticmethod
    def _has_content(item):
        """整条记录是否凑得出 ≥2 个「实质字符」（文字 / 数字）。

        页面压根没有目标列表时，自愈选择器可能把 ▼、箭头、分隔条之类的装饰元素
        当成「最像的容器」救回来，产出整行噪声。按整条记录累计而非逐字段要求，
        既滤掉纯装饰行，也不会误伤「某个字段只有一个数字」的正常记录。
        """
        n = 0
        for key, val in item.items():
            if key == "url" or not val:
                continue
            for ch in str(val):
                if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
                    n += 1
                    if n >= 2:
                        return True
        return False

    def _select_nodes(self, soup, selector, scope=None, strict=False, fallback=True):
        """按选择器取节点，失配时交给自愈选择器按元素指纹重定位。

        scope 为逐条模式下的容器节点：字段先在容器内找，找不到再退回整页。
        strict=True 表示这是「记录容器」之类的关键选择器，自愈判定更严格。
        返回 (nodes, healed)；healed=True 表示这次是靠自愈救回来的 —— 记一笔，
        说明网站结构已经变了，配置该更新了。
        """
        try:
            if scope is not None:
                # fallback=False 时只在容器内找：容器本身是靠自愈救回来的，
                # 再退回整页取值会让「容器选错」看起来像「字段抽到了」，产出整行噪声
                nodes = (scope.select(selector) or
                         (soup.select(selector) if fallback else []))
            else:
                nodes = soup.select(selector)
        except Exception as exc:
            log(f"选择器无效 '{selector}': {exc}", "WARN", self.config.verbose)
            return [], False
        if nodes:
            if self.adaptive is not None:
                self.adaptive.learn(selector, nodes[0])
            return nodes, False
        if self.adaptive is not None:
            healed, ok = self.adaptive.heal(soup, selector, scope, strict=strict)
            if ok and healed:
                self.adaptive.learn(selector, healed[0])
                with self.lock:
                    self.stats["healed"] += 1
                log(f"自愈重定位: '{selector}' -> <{healed[0].name}> "
                    f"（网站结构已变化，建议同步更新选择器）",
                    "INFO", quiet=self.config.quiet)
                return healed, True
        return [], False

    def _extract_items(self, url, body_text):
        """按 --select "字段名=CSS选择器" 抽取结构化数据。

        字段名后面可以用 @属性 指定取元素属性值 —— 真实网站的书名常写在 title 里、
        链接在 href 里、图片在 src/data-src 里，只取文本会丢掉一半数据。
        例：--select "书名=h3 a@title" "链接=h3 a@href" "封面=img@src"
        """
        if not self.config.selectors or not body_text:
            return
        try:
            soup = BeautifulSoup(body_text, BS_PARSER)
        except Exception:
            return

        # v4.0 逐条模式：--select-each "容器选择器" —— 一个列表页里有 20 本书时，
        # 旧逻辑会把 20 个书名挤进同一个单元格；这里按容器拆成 20 条独立记录。
        if self.config.select_each:
            buckets = self._extract_each(soup, url)
            if buckets:
                with self.lock:
                    self.items.extend(buckets)
                    self.stats["items"] = len(self.items)
                for it in buckets:
                    self._emit({"type": "item", "item": it})
            # 逐条模式抽不到就到此为止：此时页面多半压根没有目标列表（或全是噪声），
            # 再回退整页取值，几乎必然把 ▼、箭头、分隔条这类装饰元素当成一条记录入库
            return

        item = {"url": url}
        for name, spec in self.config.selectors.items():
            selector, attr = _split_selector(spec)
            try:
                nodes, _ = self._select_nodes(soup, selector)
                if attr:
                    vals = [_attr_value(n, attr) for n in nodes[:5]]
                else:
                    vals = [n.get_text(" ", strip=True) for n in nodes]
                value = " | ".join(v for v in vals if v)
            except Exception as exc:
                value = ""
                log(f"选择器无效 '{spec}': {exc}", "WARN", self.config.verbose)
            item[name] = value[:500]
        # 一条记录的字段全空说明选择器没命中，记下来的人就会白等一场
        if any(v for k, v in item.items() if k != "url"):
            # 整页模式同样可能被自愈救回装饰元素，一并判废
            if not self._has_content(item):
                log(f"丢弃自愈噪声记录（页面可能没有目标内容）: {url}",
                    "DEBUG", self.config.verbose)
                return
            with self.lock:
                self.items.append(item)
                self.stats["items"] = len(self.items)
            self._emit({"type": "item", "item": item})

    def _extract_each(self, soup, url):
        """逐条模式：每个容器产出一条记录（--select-each "article.product_pod"）。

        字段选择器先在容器内部相对查找；若某字段容器里没有，则退回整页取第一个 ——
        这样"商品名在卡片内、联系电话在页脚"这种混合布局也能一条记录全拿齐。
        """
        try:
            # 容器是关键选择器：自愈判定用严格档，防止把装饰元素当列表救回来
            containers, healed = self._select_nodes(soup, self.config.select_each, strict=True)
        except Exception as exc:
            log(f"--select-each 选择器无效: {exc}", "WARN", self.config.verbose)
            return []
        if not containers:
            return []
        out = []
        for node in containers:
            item = {"url": url}
            for name, spec in self.config.selectors.items():
                selector, attr = _split_selector(spec)
                try:
                    found, _ = self._select_nodes(soup, selector, scope=node,
                                                  fallback=not healed)
                    if attr:
                        vals = [_attr_value(n, attr) for n in found[:5]]
                    else:
                        vals = [n.get_text(" ", strip=True) for n in found[:5]]
                    item[name] = " | ".join(v for v in vals if v)[:500]
                except Exception as exc:
                    item[name] = ""
                    log(f"选择器无效 '{spec}': {exc}", "WARN", self.config.verbose)
            # 全空的行多半是容器选错或该条目缺字段，入库只会污染数据集
            if not any(v for k, v in item.items() if k != "url"):
                continue
            # 整行一个像样的文字都没有（▼、箭头、分隔条之类），留下只会污染数据
            if not self._has_content(item):
                log(f"丢弃自愈噪声记录（页面可能没有目标列表）: {url}",
                    "DEBUG", self.config.verbose)
                continue
            out.append(item)
        return out

    def _emit(self, event):
        """向 GUI 推送结构化事件（CLI 模式下 on_progress 为 None，无副作用）。"""
        if self.on_progress:
            try:
                self.on_progress(event)
            except Exception:
                pass

    # ---- 单页处理 ----

    def process_url(self, url, depth):
        """抓取并解析单个页面。返回 (新发现的待爬链接, 需一并标记为已访问的 URL)。"""
        try:
            status, headers, body, final_url = self.fetcher.fetch(url)
        except Exception as exc:  # 防御性兜底
            log(f"处理异常 {url}: {exc}", "ERROR", self.config.verbose)
            with self.lock:
                self.stats["errors"] += 1
            return [], []

        if status is None:
            with self.lock:
                self.stats["errors"] += 1
            return [], []

        # v1.5.0：渲染模式下顺带捕获到的后台接口（数据常藏在 XHR 里，HTML 里没有）
        try:
            caps = self.fetcher.drain_xhr(url)
        except Exception:
            caps = []
        if caps:
            page_url = final_url or url
            with self.lock:
                for c in caps:
                    self.xhr_captures.append({
                        "page": page_url, "api": c.get("url", ""),
                        "status": c.get("status"), "content_type": c.get("content_type", ""),
                        "body": c.get("body", ""),
                    })
                self.stats["xhr"] += len(caps)
            log(f"捕获后台接口 {len(caps)} 条: {url}", "DEBUG", self.config.verbose)

        # 解码与解析是 CPU 密集操作，放在锁外执行，避免多线程互相阻塞
        title = ""
        parsed = None
        body_text = ""
        if body:
            body_text = decode_body(body, headers)
            if looks_html(headers, url):
                parsed = parse_page(body_text, final_url or url, self.config)
                tm = re.search(r"<title[^>]*>(.*?)</title>", body_text, re.S | re.I)
                if tm:
                    title = html_mod.unescape(tm.group(1).strip())[:200]
                self._extract_items(final_url or url, body_text)

        # ---- v4.0：结构化提取 / 去重 / 流式导出（CPU 密集，一律放在锁外） ----
        page_hash = ""
        if self.exporter is not None or self.config.dedup:
            if body_text:
                page_hash = hashlib.sha1(
                    re.sub(r"\s+", "", body_text).encode("utf-8", "ignore")).hexdigest()
        if self.exporter is not None:
            self.exporter.add_page({
                "url": url, "final_url": final_url, "status": status,
                "content_type": (headers or {}).get("Content-Type", ""),
                "title": title, "depth": depth, "size": len(body or b""),
                "content_hash": page_hash,
            })
        if self.config.incremental and status == 304:
            with self.lock:
                self.stats["not_modified"] += 1

        if (self.config.extract and body_text and looks_html(headers, url)
                and smart_extract is not None):
            # 去重：同一套模板 + 同样正文（如分页内容重复、日历归档）只入库一次。
            # 注意只跳过"入库"，链接发现照常进行 —— 重复页面也可能挂着新线索。
            duplicated = False
            if self.config.dedup and page_hash:
                with self.lock:
                    duplicated = page_hash in self.hashes
                    if not duplicated:
                        self.hashes.add(page_hash)
                if duplicated:
                    with self.lock:
                        self.stats["duplicates"] += 1
            if not duplicated:
                try:
                    rec, tables = smart_extract.build_record(final_url or url, body_text)
                    rec["状态码"] = status
                    rec["抓取深度"] = depth
                    rec["抓取时间"] = datetime.now().isoformat(timespec="seconds")
                    if self.exporter is not None:
                        # 有导出器时由它持有数据（sqlite 已落盘），不再额外存内存，方便跑量
                        self.exporter.add_record(
                            rec, tables if self.config.extract_tables else None)
                    else:
                        with self.lock:
                            self.records.append(rec)
                    with self.lock:
                        self.stats["structured"] += 1
                        self.stats["tables"] += len(tables)
                    self._emit({"type": "record", "record": rec})
                except Exception as exc:
                    log(f"结构化提取失败({exc}): {url}", "DEBUG", self.config.verbose)

        events = []
        new_links = []
        extra_visited = []

        with self.lock:
            if 200 <= status < 300:
                self.stats["http_2xx"] += 1
            elif 300 <= status < 400:
                self.stats["http_3xx"] += 1
            elif 400 <= status < 500:
                self.stats["http_4xx"] += 1
            else:
                self.stats["http_5xx"] += 1

            page_record = {
                "url": url,
                "final_url": final_url,
                "status": status,
                "content_type": (headers or {}).get("Content-Type", ""),
                "server": (headers or {}).get("Server", ""),
                "powered_by": (headers or {}).get("X-Powered-By", ""),
                "title": title,
                "depth": depth,
            }
            self.pages.append(page_record)
            events.append({"type": "page", "page": page_record, "stats": dict(self.stats)})

            # v3.9：风控挑战页识别。这类响应不是「抓失败」而是「被拦了」，
            # 单独挑出来，一眼就能看出对抗点在哪、要不要换通道（如走移动端老接口）。
            if looks_challenged(status, body_text):
                self.stats["challenged"] += 1
                self.challenges.append({"url": url, "status": status,
                                        "title": title[:80]})

            # 重定向后的真实地址也标记为已访问，避免同一页面换个 URL 再抓一次
            if final_url and final_url != url:
                extra_visited.append(final_url)

            # 敏感路径标记：只要请求有响应就标记，不依赖是否成功解析
            if self._is_interesting(url):
                self.interesting.append(url)
                events.append({"type": "interesting", "url": url})

            if parsed:
                self.forms.extend(parsed["forms"])
                self.assets["scripts"].update(parsed["scripts"])
                self.assets["styles"].update(parsed["styles"])
                self.assets["frames"].update(parsed["frames"])
                self._merge_misc(parsed["misc"])
                for f in parsed["forms"]:
                    events.append({"type": "form", "form": f})
                for c in parsed["comments"]:
                    self.comments.append((url, c))
                    events.append({"type": "comment", "page": url, "comment": c})

                candidates = list(parsed["links"])
                for f in parsed["forms"]:
                    if f["action"]:
                        candidates.append(f["action"])   # 表单 action 也是重要端点
                if parsed["js_urls"]:
                    self.js_url_count += len(parsed["js_urls"])
                    self.js_extracted.update(parsed["js_urls"])
                    candidates.extend(parsed["js_urls"])

                for cand in candidates:
                    self.discovered.add(cand)
                    if cand not in self.visited:
                        new_links.append(cand)
                    if not self.in_crawl_scope(cand):
                        self.external[cand] = self.external.get(cand, 0) + 1

                for res in (list(parsed["scripts"]) + list(parsed["styles"])
                            + list(parsed["frames"])):
                    self.discovered.add(res)
                    if not self.in_crawl_scope(res):
                        self.external[res] = self.external.get(res, 0) + 1

                # v4.0：分页自动扩展 —— 列表页只给"下一页"按钮时，靠它把后续 N 页补齐
                if self.config.pager > 0:
                    # 注意：本段整体已在 `with self.lock` 内，这里绝不能再次获取
                    # self.lock —— 同一线程重复取非可重入锁会直接死锁。
                    for gen in self._pager_urls(final_url or url, parsed, body_text):
                        # 分页链接单独记一份：即使用户把深度设成 0（只想要列表页本身），
                        # --pager 也应当顺着翻下去，否则这个参数等于失效
                        if gen not in self._pager_seeds:
                            self._pager_seeds.append(gen)
                        self.discovered.add(gen)
                        if gen not in self.visited:
                            new_links.append(gen)

        # 事件回调放在锁外，避免与 GUI 的锁互相等待
        for e in events:
            self._emit(e)

        # 限速统一由 Fetcher 在请求前处理（固定 --delay 或 --autothrottle 自适应间隔），
        # 这里不再重复 sleep，否则实际间隔会翻倍。
        return new_links, extra_visited

    def _pager_urls(self, url, parsed, body_text=""):
        """顺延生成后续分页 URL。

        两条线索任一命中即可：① 页面明确写了 <link rel="next"> 或"下一页"锚文本；
        ② URL 本身带 page/p/pn 之类的页码参数。二者结合才推算，避免拿一个普通
        列表页瞎猜导致 URL 爆炸。生成量上限由 --pager N 控制。
        """
        out = []
        # ① 页面里明写的「下一页」链接（<link rel=next> 或锚文本）——最可靠的线索。
        #    真实站点大量列表页的 URL 根本不带页码参数，只靠参数推算是抓不动的。
        for nxt in (parsed.get("next_pages") or [])[:self.config.pager]:
            if nxt != url and nxt not in out:
                out.append(nxt)

        # ② URL 带页码参数时，直接从参数推算后续页，补足到 --pager 指定的数量
        parts = urlparse(url)
        qs = dict(parse_qsl(parts.query))
        start, key = None, ""
        for k, v in qs.items():
            if k.lower() in PAGER_PARAMS and str(v).isdigit():
                key, start = k, int(v)
                break
        has_next = bool(out) or bool(re.search(r'rel=["\']?next', body_text or "", re.I))
        if not has_next:
            links = [str(l) for l in (parsed.get("links") or [])]
            has_next = any(any(nt in ln.lower() for nt in NEXT_TEXT) for ln in links[:80])
        if start is not None and has_next and len(out) < self.config.pager:
            step = 0
            while len(out) < self.config.pager and step < self.config.pager + 5:
                step += 1
                qs[key] = str(start + step)
                cand = urlunparse(parts._replace(query=urlencode(qs, doseq=True)))
                if cand != url and cand not in out:
                    out.append(cand)
        elif start is None and not out and has_next:
            # ③ 既没有页码参数也没有可跟随的链接 —— 不再瞎猜，避免 URL 爆炸
            pass
        return out[:self.config.pager]

    def _crawl_pager_chain(self, budget):
        """顺着「下一页」链条继续抓，轮数上限 = --pager N，同时受页面上限约束。"""
        for _ in range(max(1, self.config.pager)):
            if self.stop_requested.is_set():
                break
            with self.lock:
                seeds = [u for u in dict.fromkeys(self._pager_seeds) if u not in self.visited]
                self._pager_seeds = []
            crawled = len(self.pages)
            if not seeds or crawled >= budget:
                break
            seeds = seeds[:max(0, budget - crawled)]
            log(f"分页续抓: 顺延 {len(seeds)} 个后续页", "INFO", quiet=self.config.quiet)
            with ThreadPoolExecutor(max_workers=self.config.threads) as executor:
                futures = {executor.submit(self.process_url, u, self.config.depth): u
                           for u in seeds}
                for fut in as_completed(futures):
                    u = futures[fut]
                    try:
                        _, extra_visited = fut.result()
                    except Exception as exc:
                        log(f"分页任务异常 {u}: {exc}", "ERROR", self.config.verbose)
                        continue
                    with self.lock:
                        self.visited.add(u)
                        for e in extra_visited:
                            self.visited.add(e)

    def _merge_misc(self, items):
        """合并 <link> 元信息，按 URL 去重（调用方需持锁）。"""
        seen = {m["url"] for m in self.misc_links}
        for m in items:
            if m["url"] not in seen:
                seen.add(m["url"])
                self.misc_links.append(m)

    # ---- 主循环（BFS） ----

    def crawl(self, seed_url):
        os.makedirs(self.config.output_dir, exist_ok=True)
        self.load_visited()

        self._emit({
            "type": "crawl_start",
            "seed": seed_url,
            "output_dir": self.config.output_dir,
            "config": {
                "depth": self.config.depth,
                "threads": self.config.threads,
                "max_pages": self.config.max_pages,
            },
        })

        # 先收集 robots.txt 信息（含 Crawl-delay 与 Sitemap 声明）
        self._fetch_robots(seed_url)

        # 断点续爬：有断点就从断点处继续，否则从种子页开始
        resume_level, resume_queue = self._load_checkpoint()

        seeds = [seed_url]
        if self.config.url_list:
            # v3.9：URL 清单批量模式——清单里的地址直接进队，不依赖页面链接发现。
            seeds.extend(self.config.url_list)
            log(f"URL 清单载入 {len(self.config.url_list)} 个地址作为种子",
                "INFO", quiet=self.config.quiet)
        # 纯批量模式（-d 0 + 清单）不去解析 sitemap，避免拉进一堆无关地址
        if self.config.sitemap and (not self.config.url_list or self.config.depth > 0):
            seeds.extend(self._fetch_sitemap(seed_url))
        seeds = list(dict.fromkeys(seeds))
        self.discovered.update(seeds)
        # v3.9：sitemap 种子很容易把页面预算一次性吃光（曾经让人误以为「跨端口链接没被抓」），
        # 这里主动说清楚，省得对着空结果排查半天。
        if len(seeds) > self.config.max_pages:
            log(f"提示: 种子（含 sitemap）共 {len(seeds)} 个，已超过页面上限 "
                f"{self.config.max_pages}，本次只会抓前 {self.config.max_pages} 个；"
                "想深挖链接请加 --no-sitemap 或调大 --max-pages",
                "WARN", quiet=self.config.quiet)

        if resume_queue:
            current = resume_queue
            level = resume_level
            log(f"断点续爬：从第 {level} 层继续，待爬 {len(current)} 个 URL",
                "INFO", quiet=self.config.quiet)
        else:
            current = seeds
            level = 0
        crawled = 0                      # 本次实际抓取的页数（不含历史记录）
        budget = self.config.max_pages
        todo = []                        # 当前层队列（停止时用来存断点）
        next_level = []

        try:
          while (current and level <= self.config.depth and crawled < budget
                 and not self.stop_requested.is_set()):
            todo = []
            seen = set()
            for u in current:
                if u in self.visited or u in seen:
                    continue
                if self._excluded(u):
                    continue
                seen.add(u)
                todo.append(u)

            remaining = budget - crawled
            if remaining <= 0:
                break
            if len(todo) > remaining:
                log(f"达到页面上限，本次 {len(todo)} 个 URL 只处理前 {remaining} 个",
                    "INFO", self.config.verbose)
                todo = todo[:remaining]
            if not todo:
                break

            log(f"第 {level} 层: 待抓取 {len(todo)} 个 URL (已抓取 {crawled}/{budget})",
                "INFO", quiet=self.config.quiet)
            self._emit({"type": "level", "level": level, "pending": len(todo),
                        "visited": crawled, "budget": budget})

            next_level = []
            with ThreadPoolExecutor(max_workers=self.config.threads) as executor:
                futures = {executor.submit(self.process_url, u, level): u for u in todo}
                for fut in as_completed(futures):
                    u = futures[fut]
                    try:
                        new_links, extra_visited = fut.result()
                    except Exception as exc:
                        log(f"任务异常 {u}: {exc}", "ERROR", self.config.verbose)
                        new_links, extra_visited = [], []
                    with self.lock:
                        self.visited.add(u)
                        for e in extra_visited:
                            self.visited.add(e)
                        crawled = len(self.pages)
                    next_level.extend(new_links)
                    if crawled >= budget:
                        log("达到 max_pages 上限，停止扩展", "INFO", quiet=self.config.quiet)
                        break
                    if self.stop_requested.is_set():
                        log("收到停止请求，停止扩展新页面", "INFO", quiet=self.config.quiet)
                        break

            if self.stop_requested.is_set():
                break

            # v1.5.0：分页续抓。--pager 是用户明确的翻页指令，
            # 即使深度设为 0（只想要列表页）也要顺着「下一页」翻下去，
            # 否则这个参数在 -d 0 时完全失效。
            if self.config.pager > 0:
                self._crawl_pager_chain(budget)

            # 下一层去重 + 排除已抓，优先处理命中敏感关键词的 URL
            next_level = [u for u in dict.fromkeys(next_level) if u not in self.visited]
            if self.config.same_host:      # v3.9：--same-host 时丢掉站外链接
                next_level = [u for u in next_level if self.in_crawl_scope(u)]
            next_level.sort(key=lambda x: (not self._is_interesting(x), len(urlparse(x).path)))
            current = next_level
            level += 1
            # 每爬完一层存一次断点，中途关窗口/断电也不会前功尽弃
            self._save_checkpoint(level, current)

        except KeyboardInterrupt:
            log(f"\n收到中断，正在保存断点（第 {level} 层）...", "WARN", quiet=self.config.quiet)
            self._save_checkpoint(level, self._remaining(todo, next_level))
            raise
        finally:
            self.fetcher.close()      # 关掉浏览器渲染线程（没开的话是空操作）

        # 结束判定：还有没爬的 URL 就存断点，全部爬完才清除
        pending = self._remaining(todo, next_level)
        if self.stop_requested.is_set():
            self._save_checkpoint(level, pending)
            log(f"已停止，剩余 {len(pending)} 个 URL 存入断点：{self.config.checkpoint_path}",
                "INFO", quiet=self.config.quiet)
        elif level > self.config.depth:
            # 深度已爬满：next_level 里剩下的都超出深度了，不再作为断点
            self._clear_checkpoint()
        elif pending:
            # 达到页面上限而提前结束，剩下的留给下次
            self._save_checkpoint(level, pending)
            log(f"本次结束，仍有 {len(pending)} 个 URL 未爬（已存入断点，再次运行可继续）",
                "INFO", quiet=self.config.quiet)
        else:
            self._clear_checkpoint()

        self.save_visited()
        self.stats["pages"] = len(self.pages)
        self.stats["urls_discovered"] = len(self.discovered)
        self.stats["forms"] = len(self.forms)
        self.stats["js_urls"] = self.js_url_count
        self.stats["cache_hits"] = self.fetcher.cache.hits
        self.stats["rendered"] = self.fetcher.renderer.rendered
        self.stats["items"] = len(self.items)
        self.stats["avg_latency"] = round(self.fetcher.throttle.avg_latency, 3)
        self._emit({
            "type": "crawl_end",
            "stats": dict(self.stats),
            "stopped": self.stop_requested.is_set(),
        })
        return self.stats

    # ---- 辅助 ----

    def _is_interesting(self, url):
        path = urlparse(url).path.lower()
        return any(p in path for p in INTERESTING_PATTERNS)

    def _excluded(self, url):
        """命中 --exclude 正则或 robots Disallow 的 URL 不爬。"""
        path = urlparse(url).path
        for pattern in self.config.exclude:
            if pattern.search(url):
                return True
        if self.config.respect_robots:
            for d in self.robots:
                if d and path.startswith(d):
                    return True
        return False

    def _fetch_robots(self, seed_url):
        origin = origin_of(seed_url)
        status, headers, body, _ = self.fetcher.fetch_plain(origin + "/robots.txt")
        if status != 200 or not body:
            return
        text = decode_body(body, headers or {})
        delay = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith("disallow:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    self.robots.append(val)
            elif low.startswith("sitemap:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    self.robots_sitemaps.append(val)
            elif low.startswith("crawl-delay:"):
                try:
                    delay = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        self.robots = list(dict.fromkeys(self.robots))
        self.robots_sitemaps = list(dict.fromkeys(self.robots_sitemaps))
        self.robots_crawl_delay = delay
        log(f"robots.txt: 解析到 {len(self.robots)} 条 Disallow"
            + (f"，{len(self.robots_sitemaps)} 条 Sitemap" if self.robots_sitemaps else "")
            + (f"，Crawl-delay {delay}s" if delay else ""),
            "INFO", quiet=self.config.quiet)
        # 仅在用户明确要求遵守 robots、且未自行指定限速时采纳 Crawl-delay
        if self.config.respect_robots and delay and not self.config.delay:
            self.config.delay = min(delay, 60)
            log(f"采纳 robots.txt 的 Crawl-delay: {self.config.delay}s",
                "INFO", quiet=self.config.quiet)

    def _fetch_sitemap(self, seed_url):
        """解析 sitemap 作为额外种子，覆盖站点里未被链接到的页面。"""
        origin = origin_of(seed_url)
        targets = list(self.robots_sitemaps) or [origin + p for p in
                                                 ("/sitemap.xml", "/sitemap_index.xml",
                                                  "/sitemap.txt")]
        targets = list(dict.fromkeys(targets))
        found = []
        child_sitemaps = []

        for url in targets[:5]:
            status, headers, body, _ = self.fetcher.fetch_plain(url)
            if status != 200 or not body:
                continue
            text = decode_body(body, headers or {})
            locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I | re.S)
            if not locs:
                locs = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("http")]
            for loc in locs:
                u = normalize_url(html_mod.unescape(loc.strip()), ignore_params=self.config.ignore_params)
                if not u:
                    continue
                # sitemap_index 里套的是子 sitemap，单独收集后跟进一层
                if "sitemap" in urlparse(u).path.lower() and u.endswith(".xml"):
                    child_sitemaps.append(u)
                else:
                    found.append(u)

        for url in child_sitemaps[:10]:
            status, headers, body, _ = self.fetcher.fetch_plain(url)
            if status != 200 or not body:
                continue
            text = decode_body(body, headers or {})
            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I | re.S):
                u = normalize_url(html_mod.unescape(loc.strip()), ignore_params=self.config.ignore_params)
                if u:
                    found.append(u)

        found = list(dict.fromkeys(found))
        self.sitemap_seeds = found
        if found:
            log(f"sitemap: 解析到 {len(found)} 个种子 URL", "INFO", quiet=self.config.quiet)
        return found

    # ---- 输出 ----

    def write_reports(self):
        os.makedirs(self.config.output_dir, exist_ok=True)

        report = {
            "meta": {
                "engine": ENGINE_VERSION,
                "seed": self.config.seed,
                "started": self.config.started,
                "depth": self.config.depth,
                "threads": self.config.threads,
                "user_agent": self.config.user_agent,
                "sitemap_seeds": len(self.sitemap_seeds),
                "crawl_delay": self.config.delay,
                "per_domain": self.config.per_domain,
                "autothrottle": self.config.autothrottle,
                "checkpoint": self.config.checkpoint,
                "cache": self.config.cache,
                "render": self.config.render or "http",
                # v3.9
                "method": self.config.method,
                "qps": self.config.qps or "不限",
                "url_file": self.config.url_file,
                "url_list_size": len(self.config.url_list),
                "extra_domains": sorted(self.config.extra_domains),
                "same_host": self.config.same_host,
                # v4.0
                "extract": self.config.extract,
                "export": ",".join(self.config.export) or "无",
                "pager": self.config.pager or "关",
                "incremental": self.config.incremental,
                "dedup": self.config.dedup,
                "rotate_ua": self.config.rotate_ua,
                "referer_auto": self.config.referer_auto,
                # v1.5.0
                "adaptive": self.config.adaptive,
                "capture_xhr": self.config.capture_xhr,
                "proxy_strategy": self.config.proxy_strategy,
                "proxy_fail_eject": self.config.proxy_fail_eject,
                "healed": self.stats.get("healed", 0),
                "xhr_captured": self.stats.get("xhr", 0),
            },
            "stats": self.stats,
            "pages": self.pages,
            "forms": self.forms,
            "assets": {k: sorted(v) for k, v in self.assets.items()},
            "meta_links": self.misc_links,
            "comments": [{"page": p, "comment": c} for p, c in self.comments],
            "robots_disallow": self.robots,
            "robots_sitemaps": self.robots_sitemaps,
            "js_extracted": sorted(self.js_extracted),
            "external_refs": dict(sorted(self.external.items(), key=lambda kv: -kv[1])),
            "interesting": self.interesting,
            "challenges": self.challenges,          # v3.9：被风控挑战/拦截的地址
        }
        with open(os.path.join(self.config.output_dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)

        # urls.txt —— 所有发现过的 URL（含未抓取的）
        with open(os.path.join(self.config.output_dir, "urls.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(self.discovered)) + "\n")

        # forms.csv
        with open(os.path.join(self.config.output_dir, "forms.csv"), "w",
                  encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["page", "action", "method", "input_names"])
            for f in self.forms:
                writer.writerow([f["page"], f["action"], f["method"],
                                 ";".join(i["name"] for i in f["inputs"])])

        # interesting.txt
        with open(os.path.join(self.config.output_dir, "interesting.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.interesting) + "\n")

        # challenges.txt（v3.9）：被风控拦下的地址，便于针对性换通道
        if self.challenges:
            with open(os.path.join(self.config.output_dir, "challenges.txt"), "w",
                      encoding="utf-8") as fh:
                for c in self.challenges:
                    fh.write(f"{c['status']}\t{c['url']}\t{c.get('title','')}\n")

        # xhr_api.json（v1.5.0）：渲染模式捕获到的后台接口，等于直接拿到数据源
        if self.xhr_captures:
            with open(os.path.join(self.config.output_dir, "xhr_api.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(self.xhr_captures, fh, ensure_ascii=False, indent=2)
            self.export_files.append(
                os.path.join(self.config.output_dir, "xhr_api.json"))
            log(f"导出: xhr_api.json（{len(self.xhr_captures)} 条接口响应）",
                "INFO", quiet=self.config.quiet)

        # comments.txt
        with open(os.path.join(self.config.output_dir, "comments.txt"), "w", encoding="utf-8") as fh:
            for page, c in self.comments:
                fh.write(f"# {page}\n{c}\n\n")

        # items.csv / items.json —— 只有配了 --select 才有内容
        if self.items:
            fieldnames = ["url"] + [k for k in self.items[0].keys() if k != "url"]
            with open(os.path.join(self.config.output_dir, "items.csv"), "w",
                      encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for it in self.items:
                    writer.writerow(it)
            with open(os.path.join(self.config.output_dir, "items.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(self.items, fh, ensure_ascii=False, indent=2)

        # ---- v4.0：收尾导出（csv / jsonl / xlsx 需要全量列集合，故放在最后） ----
        if self.exporter is not None:
            self.export_files = self.exporter.close()
            self.exporter = None
            for p in self.export_files:
                log(f"导出: {os.path.basename(p)}", "INFO", quiet=self.config.quiet)
        elif self.records:
            # 没配导出格式时，结构化记录至少落成一份 CSV，不让提取结果白丢
            keys = []
            for r in self.records:
                for k in r:
                    if k not in keys:
                        keys.append(k)
            path = os.path.join(self.config.output_dir, "structured.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                for r in self.records:
                    w.writerow(r)
            self.export_files.append(path)

        report["export_files"] = [os.path.basename(p) for p in self.export_files]
        report["structured_fields"] = len(self.records or []) and max(
            (len(r) for r in self.records), default=0) or 0

        self._write_report_md(report)
        return os.path.abspath(self.config.output_dir)

    def _write_report_md(self, report):
        """人类可读的 Markdown 摘要，便于归档与对比多次任务。"""
        s = self.stats
        cfg = self.config
        lines = [
            "# 爬取报告",
            "",
            f"- 目标：{cfg.seed}",
            f"- 开始时间：{cfg.started}",
            f"- 引擎版本：range_crawler {ENGINE_VERSION}",
            f"- 输出目录：{os.path.abspath(cfg.output_dir)}",
            "",
            "## 运行参数",
            "",
            f"- 深度 / 线程：{cfg.depth} / {cfg.threads}",
            f"- 页面上限：{cfg.max_pages}",
            f"- 每域名并发：{cfg.per_domain or '不限'}",
            f"- 限速：{'自适应（autothrottle）' if cfg.autothrottle else (str(cfg.delay) + 's')}",
            f"- 抓取方式：{cfg.render or 'HTTP'}"
            + (f"（{cfg.render}）" if cfg.render else ""),
            f"- 断点续爬：{'开' if cfg.checkpoint else '关'}",
            f"- 响应缓存：{'开' if cfg.cache else '关'}",
            "",
            "## 结果概览",
            "",
            f"- 已抓页面：{s.get('pages', 0)}",
            f"- 发现 URL：{s.get('urls_discovered', 0)}",
            f"- 表单：{s.get('forms', 0)}",
            f"- HTTP 2xx/3xx/4xx/5xx：{s.get('http_2xx', 0)}/{s.get('http_3xx', 0)}/"
            f"{s.get('http_4xx', 0)}/{s.get('http_5xx', 0)}",
            f"- 请求错误：{s.get('errors', 0)}",
        ]
        if s.get("cache_hits"):
            lines.append(f"- 缓存命中：{s['cache_hits']}")
        if s.get("rendered"):
            lines.append(f"- 浏览器渲染页：{s['rendered']}")
        if s.get("items"):
            lines.append(f"- 自定义字段提取：{s['items']} 条")
        if s.get("challenged"):
            lines.append(f"- **被风控挑战/拦截：{s['challenged']} 次**（见下方清单，需换通道或降速）")
        if s.get("structured"):
            lines.append(f"- **结构化提取：{s['structured']} 条记录**"
                         + (f"，含 {s.get('tables', 0)} 张表格" if s.get("tables") else ""))
        if s.get("duplicates"):
            lines.append(f"- 重复内容跳过：{s['duplicates']} 页（--dedup）")
        if s.get("not_modified"):
            lines.append(f"- 未变更跳过：{s['not_modified']} 页（--incremental，返回 304）")
        if self.export_files:
            lines.append(f"- 导出文件：{'、'.join(os.path.basename(p) for p in self.export_files)}")
        lines += [
            "",
            "## 值得关注的目标",
            "",
        ]
        if self.interesting:
            lines += [f"- `{u}`" for u in self.interesting[:50]]
        else:
            lines.append("- （无）")
        if self.challenges:
            lines += ["", "## 被风控挑战/拦截的地址", "",
                      "| 状态码 | 地址 | 标题 |", "|---|---|---|"]
            for c in self.challenges[:50]:
                ttl = (c.get("title") or "").replace("|", "/").replace("\n", " ")[:40]
                lines.append(f"| {c.get('status')} | `{c.get('url')}` | {ttl} |")
            if len(self.challenges) > 50:
                lines.append(f"| ... | 其余 {len(self.challenges) - 50} 条见 report.json | |")
        lines += ["", "## 页面清单", "", "| 状态 | 深度 | 标题 | 地址 |", "|---|---|---|---|"]
        for p in self.pages[:200]:
            title = (p.get("title") or "").replace("|", "/").replace("\n", " ")[:40]
            lines.append(f"| {p.get('status')} | {p.get('depth')} | {title} | `{p.get('url')}` |")
        if len(self.pages) > 200:
            lines.append(f"| ... | | 其余 {len(self.pages) - 200} 页见 report.json | |")
        with open(os.path.join(self.config.output_dir, "report.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def print_summary(self):
        s = self.stats
        log("=" * 56, "INFO", quiet=False)
        log(f"种子: {self.config.seed}", "INFO")
        log(f"已访问页面: {s.get('pages', 0)}", "INFO")
        log(f"发现 URL: {s.get('urls_discovered', 0)}", "INFO")
        if self.sitemap_seeds:
            log(f"sitemap 种子: {len(self.sitemap_seeds)}", "INFO")
        log(f"发现表单: {s.get('forms', 0)}", "INFO")
        log(f"HTTP 2xx/3xx/4xx/5xx: {s.get('http_2xx', 0)}/{s.get('http_3xx', 0)}/"
            f"{s.get('http_4xx', 0)}/{s.get('http_5xx', 0)}", "INFO")
        log(f"请求错误: {s.get('errors', 0)}", "INFO")
        if s.get("cache_hits"):
            log(f"缓存命中: {s['cache_hits']} 次（未重复请求目标站点）", "INFO")
        if s.get("rendered"):
            log(f"浏览器渲染: {s['rendered']} 页", "INFO")
        if s.get("items"):
            log(f"自定义字段提取: {s['items']} 条（items.csv / items.json）", "INFO")
        log(f"命中敏感路径: {len(self.interesting)}", "INFO")
        if s.get("challenged"):
            log(f"被风控挑战/拦截: {s['challenged']} 次（challenges.txt，需换通道或降速）",
                "INFO")
        log(f"站外引用: {len(self.external)}", "INFO")
        if self.comments:
            log(f"HTML 注释: {len(self.comments)} 条", "INFO")
        log(f"报告输出目录: {os.path.abspath(self.config.output_dir)}", "INFO")
        log("=" * 56, "INFO")


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="range_crawler",
        description="Gecko：站点爬取 + 表单/资源/注释发现 + 敏感路径标记。仅用于你拥有或已获授权的目标。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-u", "--url", default="",
                   help="种子 URL，例如 http://127.0.0.1:8000；给了 --url-file 时可省略")
    p.add_argument("--url-file", default="", metavar="PATH",
                   help="[v3.9] URL 清单文件：一行一个地址，批量抓取。"
                        "配合 -d 0 就是「只抓清单里的这些地址」，不再逐个启动进程")
    p.add_argument("-d", "--depth", type=int, default=3,
                   help="爬取深度（BFS 层数，0 表示只抓种子页），默认 3")
    p.add_argument("--threads", type=int, default=5, help="并发线程数，默认 5")
    p.add_argument("--delay", type=float, default=0,
                   help="请求间隔秒数（可选限速），默认 0 不限速")
    p.add_argument("--max-pages", type=int, default=500, help="本次最多抓取页面数，默认 500")
    p.add_argument("--timeout", type=float, default=10, help="请求超时秒数，默认 10")
    p.add_argument("--retries", type=int, default=2, help="失败重试次数，默认 2")
    p.add_argument("--max-body-mb", type=float, default=3,
                   help="单页最多读取多少 MB 后截断（默认 3；调大可抓完整大页面，但更耗内存）")
    p.add_argument("--user-agent", default=DEFAULT_UA, help="自定义 User-Agent")
    p.add_argument("--cookie", default="", help='携带 Cookie（如登录态）: --cookie "session=abc; uid=1"')
    p.add_argument("--proxy", default="", help="固定代理（http/socks5）: --proxy http://127.0.0.1:7890")
    p.add_argument("--proxy-file", default="", help="代理列表文件（每行一个，自动轮换应对 IP 限流）")
    p.add_argument("--header", action="append", default=[], metavar='"Name: Value"',
                   help="自定义请求头，可多次指定，如 --header 'Authorization: Bearer xxx'")
    p.add_argument("--no-verify", action="store_true", help="跳过 HTTPS 证书校验（自签名靶场常用）")
    p.add_argument("--backend", choices=["auto", "requests", "curl_cffi"], default="auto",
                   help="HTTP 后端：auto=装了 curl_cffi 就用它伪装 Chrome 指纹；"
                        "requests=传统后端；curl_cffi=强制浏览器指纹")
    p.add_argument("--impersonate", default=None, metavar="TARGET",
                   help="指定浏览器指纹，如 chrome / safari / firefox / chrome110；"
                        "留空时后端为 curl_cffi 则用 chrome。--backend requests 时无效")
    p.add_argument("--render", choices=["dynamic", "stealthy"], default="",
                   help="浏览器渲染（默认关闭，用纯 HTTP 抓取）：dynamic=真实 Chromium 渲染，"
                        "可抓 SPA / JS 动态内容；stealthy=反检测浏览器，额外抹掉自动化痕迹。"
                        "需先安装: python -m pip install playwright && python -m playwright install chromium")
    p.add_argument("--wait-selector", default="", metavar="CSS",
                   help="[需 --render] 等到该 CSS 选择器出现再取页面，如 --wait-selector '.list-item'")
    p.add_argument("--network-idle", action="store_true",
                   help="[需 --render] 等到网络空闲再取页面（比默认的 DOM 就绪更慢但更完整）")
    p.add_argument("--no-headless", action="store_true",
                   help="[需 --render] 显示浏览器窗口（默认无头运行）")
    p.add_argument("--per-domain", type=int, default=0, metavar="N",
                   help="每个域名的最大并发请求数，0=不限（建议 2~5，避免压垮目标站点）")
    p.add_argument("--autothrottle", action="store_true",
                   help="自适应限速：按响应时间自动调整请求间隔（站点变慢就放慢）")
    p.add_argument("--checkpoint", action="store_true",
                   help="断点续爬：中断或停止后，再次运行同一命令自动从断点继续")
    p.add_argument("--cache", action="store_true",
                   help="响应缓存：相同请求直接复用上次结果，调参数时不重复打扰目标站点")
    p.add_argument("--select", action="append", default=[], metavar="名称=CSS选择器",
                   help="自定义字段提取，可多次指定，如 --select '标题=h1' --select '价格=.price'；"
                        "结果写入 items.csv / items.json")
    p.add_argument("--extra-domain", action="append", default=[], metavar="HOST",
                   help="[v3.9] 额外放行的主机（含端口），可多次指定，"
                        "如 --extra-domain 127.0.0.1:3000；用于同机多端口靶场互相引用")
    p.add_argument("--same-host", action="store_true",
                   help="[v3.9] 只抓站内：把待爬队列限制在种子主机内"
                        "（配合 --extra-domain 放行指定主机）。默认会跟随站外链接")
    p.add_argument("--method", default="GET", metavar="METHOD",
                   help=f"[v3.9] 请求方法，默认 GET；可选 {','.join(SUPPORTED_METHODS)}。"
                        "非 GET 建议配合 -d 0 与 --url-file，避免对发现到的链接也用该方法")
    p.add_argument("--data", default="", metavar="BODY",
                   help='[v3.9] 请求体（表单串），如 --data "a=1&b=2"；需配合 --method POST')
    p.add_argument("--json", dest="json_body", default="", metavar="JSON",
                   help='[v3.9] 请求体（JSON），如 --json \'{"q":"abc"}\'；自动带 '
                        'Content-Type: application/json')
    p.add_argument("--content-type", default="", metavar="TYPE",
                   help="[v3.9] 显式指定 Content-Type，覆盖 --data/--json 的默认值")
    p.add_argument("--qps", type=float, default=0, metavar="N",
                   help="[v3.9] 全局每秒请求数上限（如 --qps 2）。不看线程数，"
                        "直接压住总 QPS，是躲风控最有效的一个旋钮；0=不限")
    p.add_argument("--subdomains", action="store_true",
                   help="连子域名一起抓（默认只抓种子所在主机）")
    p.add_argument("--respect-robots", action="store_true",
                   help="遵守 robots.txt 的 Disallow 规则并采纳 Crawl-delay（默认仅收集信息）")
    p.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="排除匹配正则的 URL，可多次指定，例如 --exclude 'logout'")
    p.add_argument("--comments", action="store_true", help="提取 HTML 注释（靶场隐藏提示）")
    p.add_argument("--no-js-urls", action="store_true",
                   help="关闭「从内联 JS 提取 URL」（默认开启，SPA 站点靠它发现接口）")
    p.add_argument("--no-sitemap", action="store_true",
                   help="关闭「sitemap.xml 种子发现」（默认开启）")
    p.add_argument("--ignore-param", action="append", default=[], metavar="NAME",
                   help="额外忽略的 query 参数（可多次），如 --ignore-param sid")
    p.add_argument("--keep-tracking-params", action="store_true",
                   help="保留 utm_* 等跟踪参数（默认剥离，避免同一页面反复抓取）")
    p.add_argument("-o", "--output", default="", help="输出目录，默认 output_<主机名>")
    p.add_argument("--fresh", action="store_true", help="忽略上次访问记录，重新爬取")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    p.add_argument("-q", "--quiet", action="store_true", help="静默模式（仅输出警告与最终摘要）")

    # ---- v4.0 企业级能力 ----
    g4 = p.add_argument_group("v4.0 结构化数据（把页面变成可直接用的表）")
    g4.add_argument("--extract", action="store_true",
                    help="结构化提取：JSON-LD / Microdata / OpenGraph / 联系方式，"
                         "每页产出一条宽表记录")
    g4.add_argument("--extract-tables", action="store_true",
                    help="额外导出页面上的 <table> 明细（报价表、参数表、榜单）")
    g4.add_argument("--export", default="", metavar="FORMATS",
                    help="导出格式，逗号分隔：csv / jsonl / sqlite / xlsx。"
                         "含 sqlite 时边跑边落盘，爬十万页也不撑内存")
    g4.add_argument("--select-each", default="", metavar="CSS",
                    help="配合 --select：指定「一条记录」的容器（如 article.product_pod），"
                         "把列表页逐项拆成多行记录。抓商品/房源/招聘列表时必用")

    g5 = p.add_argument_group("v4.0 翻页、增量与去重")
    g5.add_argument("--pager", type=int, default=0, metavar="N",
                    help="分页自动扩展：识别 <link rel=next> 或「下一页」后顺延 N 页；0=关闭")
    g5.add_argument("--incremental", action="store_true",
                    help="增量抓取：带 ETag / Last-Modified，站点返回 304 即跳过重抓")
    g5.add_argument("--dedup", action="store_true",
                    help="按正文指纹去重，内容相同的页面只入库一次")

    g6 = p.add_argument_group("v4.0 反爬对抗")
    g6.add_argument("--rotate-ua", action="store_true",
                    help="每次请求随机轮换浏览器 UA（应对 UA 黑名单）")
    g6.add_argument("--referer-auto", action="store_true",
                    help="自动带上站内首页 Referer（应对 Referer 白名单校验）")

    g7 = p.add_argument_group("v1.5.0 自愈、接口捕获与代理策略")
    g7.add_argument("--no-adaptive", action="store_true",
                    help="关闭自愈选择器（默认开启：网站改版后按元素指纹自动重定位失配的选择器）")
    g7.add_argument("--no-xhr", action="store_true",
                    help="关闭 XHR 接口捕获（默认开启：渲染模式下把后台 JSON 接口响应一并留存）")
    g7.add_argument("--proxy-strategy", default="cyclic", choices=["cyclic", "random"],
                    help="代理轮换策略：cyclic 顺序轮换 / random 随机（默认 cyclic）")
    g7.add_argument("--no-proxy-eject", action="store_true",
                    help="关闭代理失败淘汰（默认开启：连续失败 3 次的代理自动淘汰）")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    src = {
        "url": args.url,
        "output": args.output,
        "depth": args.depth,
        "threads": args.threads,
        "delay": args.delay,
        "max_pages": args.max_pages,
        "timeout": args.timeout,
        "retries": args.retries,
        "max_body_mb": args.max_body_mb,
        "user_agent": args.user_agent,
        "cookie": args.cookie,
        "proxy": args.proxy,
        "proxy_file": args.proxy_file,
        "headers": "\n".join(args.header or []),
        "no_verify": args.no_verify,
        "backend": args.backend,
        "impersonate": args.impersonate,
        "subdomains": args.subdomains,
        "respect_robots": args.respect_robots,
        "exclude": args.exclude,
        "comments": args.comments,
        "no_js_urls": args.no_js_urls,
        "no_sitemap": args.no_sitemap,
        "fresh": args.fresh,
        "render": args.render,
        "per_domain": args.per_domain,
        "autothrottle": args.autothrottle,
        "checkpoint": args.checkpoint,
        "cache": args.cache,
        "wait_selector": args.wait_selector,
        "network_idle": args.network_idle,
        "no_headless": args.no_headless,
        "select": args.select,
        "verbose": args.verbose,
        "quiet": args.quiet,
        # v3.9
        "url_file": args.url_file,
        "extra_domain": args.extra_domain,
        "same_host": args.same_host,
        "method": args.method,
        "data": args.data,
        "json_body": args.json_body,
        # v4.0
        "extract": args.extract,
        "extract_tables": args.extract_tables,
        "export": args.export,
        "pager": args.pager,
        "incremental": args.incremental,
        "dedup": args.dedup,
        "rotate_ua": args.rotate_ua,
        "referer_auto": args.referer_auto,
        "select_each": args.select_each,
        "content_type": args.content_type,
        "qps": args.qps,
        # v1.5.0
        "no_adaptive": args.no_adaptive,
        "no_xhr": args.no_xhr,
        "proxy_strategy": args.proxy_strategy,
        "no_proxy_eject": args.no_proxy_eject,
    }
    if args.keep_tracking_params:
        src["ignore_params"] = []
    elif args.ignore_param:
        src["ignore_params"] = list(DEFAULT_IGNORE_PARAMS) + list(args.ignore_param)

    config, err = build_config(src)
    if err:
        parser.error(err)

    if not config.verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log("警告: 已关闭 HTTPS 证书校验 (--no-verify)", "WARN")

    log(f"开始爬取: {config.seed}  深度={config.depth} 线程={config.threads} "
        f"限速={'自适应' if config.autothrottle else str(config.delay) + 's'} "
        f"每域并发={config.per_domain or '不限'} 上限={config.max_pages}"
        + (f" 全局QPS≤{config.qps:g}" if config.qps else "")
        + (f" 渲染={config.render}" if config.render else "")
        + (f" 方法={config.method}" if config.method != "GET" else "")
        + (f" URL清单={len(config.url_list)}条" if config.url_list else ""),
        "INFO", quiet=config.quiet)

    if config.method != "GET" and config.depth > 0:
        log(f"提示: --method {config.method} 会作用于所有请求（含发现到的链接），"
            "批量提交建议加 -d 0 --url-file", "WARN", quiet=config.quiet)

    crawler = RangeCrawler(config)
    try:
        crawler.crawl(config.seed)
    except KeyboardInterrupt:
        log("\n用户中断，已保存当前访问记录与报告"
            + ("，断点已写入 checkpoint.json（再次运行同一命令可续爬）"
               if config.checkpoint else "。"), "WARN")
    finally:
        crawler.write_reports()
        crawler.print_summary()

    return 0


if __name__ == "__main__":
    sys.exit(main())
