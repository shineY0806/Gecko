#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webui.py —— Gecko 图形界面（本地服务）

双击「启动Gecko.bat」即可：本服务启动后自动打开浏览器，
在网页上配置参数、点「开始爬取」，即可实时看到进度、日志和结果报告。

依赖：仅 Python 标准库 + range_crawler.py 所需的 requests / beautifulsoup4。
用法：
  python webui.py                 # 默认端口 8765，自动打开浏览器
  python webui.py --no-browser    # 不自动打开浏览器
  python webui.py --port 9000     # 指定端口
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import requests

# ---------------------------------------------------------------------------
# 包路径引导：本文件位于 gecko/ui/，引擎位于 gecko/core/。
# 源码直接运行时把「项目根」与「core 目录」加进 sys.path，
# 使 `import range_crawler` 这类平铺写法照旧可用（不必改写几百处 import）；
# 打包后 PyInstaller 已把模块收集完毕，此处插入的路径不存在也不会有影响。
# ---------------------------------------------------------------------------
_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_UI_DIR))
for _p in (os.path.join(_PROJECT_ROOT, "gecko", "core"), _PROJECT_ROOT):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# v1.4.3 性能：引擎与反爬模块懒加载——本地服务先起、界面先开，
# 首次真正用到（启动爬取 / 打开工具面板）时再导入，缩短双击到出界面的等待
_rc = None
_ac = None

def _get_rc():
    global _rc
    if _rc is None:
        import range_crawler as _m
        _m.LOG_HOOK = lambda level, msg: manager.on_event(
            {"type": "log", "level": level, "message": msg})
        _rc = _m
    return _rc

def _get_ac():
    global _ac
    if _ac is None:
        import anticrawl as _m
        _ac = _m
    return _ac

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resource(name):
    """定位随程序一起分发的资源文件。

    打包成单文件 exe 后，PyInstaller 会把资源解压到 sys._MEIPASS 临时目录，
    此时 __file__ 所在的目录不再是源码目录，直接拼接会找不到 webui.html。
    """
    bundle_dir = getattr(sys, "_MEIPASS", None)
    base = bundle_dir or BASE_DIR
    return os.path.join(base, name)


HTML_FILE = _resource("webui.html")

# ---------------------------------------------------------------------------
# 爬取任务管理
# ---------------------------------------------------------------------------


class CrawlManager:
    """持有当前任务状态，接收爬虫事件并整理成前端可读的快照。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {
            "running": False,
            "finished": False,
            "stopped": False,
            "error": None,
            "seed": None,
            "output_dir": None,
            "last_config": None,
            "stats": {},
            "pages": [],
            "forms": [],
            "interesting": [],
            "comments": [],
            "external": {},
            "log": [],
        }
        self.crawler = None
        # v1.4.3 性能：rev 状态版本号（有任何事件就 +1），log_total 日志总条数
        # 前端带 rev 轮询，无变化时只回几个字节，避免空闲时反复传全量快照
        self.rev = 0
        self.log_total = 0

    # ---- 事件入口（爬虫线程调用） ----

    def on_event(self, event):
        with self.lock:
            s = self.state
            t = event.get("type")
            if t == "crawl_start":
                s.update({
                    "running": True, "finished": False, "stopped": False,
                    "error": None, "seed": event.get("seed"),
                    "output_dir": event.get("output_dir"),
                    "stats": {}, "pages": [], "forms": [],
                    "interesting": [], "comments": [], "external": {}, "log": [],
                })
                self.log_total = 0
            elif t == "page":
                s["pages"].append(event["page"])
                s["stats"] = event.get("stats") or {}
            elif t == "form":
                s["forms"].append(event["form"])
            elif t == "interesting":
                u = event.get("url")
                if u and u not in s["interesting"]:
                    s["interesting"].append(u)
            elif t == "comment":
                s["comments"].append({"page": event.get("page"), "comment": event.get("comment")})
            elif t == "log":
                s["log"].append({"level": event.get("level"), "message": event.get("message")})
                self.log_total += 1
            elif t == "crawl_end":
                s["stats"] = event.get("stats") or {}
                s["running"] = False
                s["finished"] = True
                s["stopped"] = bool(event.get("stopped"))
            elif t == "crawl_error":
                s["running"] = False
                s["finished"] = True
                s["error"] = event.get("message")
                s["log"].append({"level": "ERROR", "message": event.get("message")})
            # 防止长时间爬取把内存撑爆
            if len(s["pages"]) > 5000:
                s["pages"] = s["pages"][-5000:]
            if len(s["log"]) > 800:
                s["log"] = s["log"][-800:]
            self.rev += 1

    # ---- 动作 ----

    def start(self, cfg):
        with self.lock:
            if self.state["running"]:
                return {"ok": False, "error": "已有任务正在运行，请先等待完成或点击停止"}

        # 与 CLI 共用同一套配置构造/校验逻辑（URL 规范化、输出目录、参数夹取、正则编译），
        # 避免两处各自拼装参数导致行为漂移。
        gui_cfg = dict(cfg)
        gui_cfg["verbose"] = False
        gui_cfg["quiet"] = True

        # 与 CLI 共用同一套配置构造/校验逻辑（URL 规范化、输出目录、参数夹取、正则编译），
        # 避免两处各自拼装参数导致行为漂移。
        config, err = _get_rc().build_config(gui_cfg)
        if err:
            return {"ok": False, "error": err}
        crawler = _get_rc().RangeCrawler(config)

        crawler.on_progress = self.on_event
        crawler.stop_requested.clear()
        self.crawler = crawler
        with self.lock:
            self.state["last_config"] = {
                "url": config.seed, "depth": config.depth, "threads": config.threads,
                "delay": config.delay, "max_pages": config.max_pages,
                "per_domain": config.per_domain, "render": config.render,
                "autothrottle": config.autothrottle, "checkpoint": config.checkpoint,
                "cache": config.cache, "selectors": config.selectors,
                "output": config.output_dir, "comments": config.comments,
                "no_verify": not config.verify, "cookie": config.cookie,
                "url_file": config.url_file, "qps": config.qps,
                "method": config.method, "same_host": config.same_host,
                # v4.0
                "extract": config.extract, "extract_tables": config.extract_tables,
                "export": ",".join(config.export), "dedup": config.dedup,
                "pager": config.pager, "incremental": config.incremental,
                "rotate_ua": config.rotate_ua, "referer_auto": config.referer_auto,
                # v1.5.0
                "adaptive": config.adaptive, "capture_xhr": config.capture_xhr,
                "proxy_strategy": config.proxy_strategy,
                "proxy_fail_eject": config.proxy_fail_eject,
            }
        threading.Thread(target=self._run, args=(crawler, config.seed), daemon=True).start()
        return {"ok": True, "output": config.output_dir}

    def _run(self, crawler, seed):
        try:
            crawler.crawl(seed)
            crawler.write_reports()
        except Exception as exc:  # 兜底，不让后台线程静默死掉
            import traceback
            traceback.print_exc()
            self.on_event({"type": "crawl_error", "message": f"{exc}"})

    def stop(self):
        c = self.crawler
        if c is None:
            return {"ok": False, "error": "没有运行中的任务"}
        c.stop_requested.set()
        return {"ok": True}

    def snapshot(self, rev=None):
        """rev 相同（无新事件）时只回一个极小的应答，空闲轮询不再传全量快照。"""
        with self.lock:
            if rev is not None and rev == self.rev:
                return {"rev": self.rev, "changed": False}
            data = json.loads(json.dumps(self.state, ensure_ascii=False))
            data["rev"] = self.rev
            data["log_total"] = self.log_total
            data["changed"] = True
            return data


def _fallback_icon_svg():
    """图标资源缺失时的兜底：白底 + 发丝描边的简约标记，保证界面不出天窗。"""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="3" fill="#FFFFFF"/>'
            '<rect x="0.5" y="0.5" width="31" height="31" rx="3" fill="none" stroke="#C9C9CE"/>'
            '<path d="M8 9h16v3H8zm0 6h16v3H8zm0 6h10v3H8z" fill="#17181A"/></svg>')


manager = CrawlManager()

# 爬虫引擎日志接线在 _get_rc() 首次导入时完成


def list_report_files():
    out = manager.snapshot().get("output_dir")
    if not out or not os.path.isdir(out):
        return {"output_dir": out, "files": []}
    items = []
    for f in sorted(os.listdir(out)):
        fp = os.path.join(out, f)
        if os.path.isfile(fp):
            items.append({
                "name": f,
                "size": os.path.getsize(fp),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(fp))),
            })
    return {"output_dir": out, "files": items}


def capability_info():
    """告诉前端当前环境支持哪些能力（浏览器渲染是否可用、指纹列表等）。"""

    def _has(module):
        try:
            __import__(module)
            return True
        except Exception:
            return False

    browsers = []
    if _has("patchright"):
        browsers.append("patchright")
    if _has("playwright"):
        browsers.append("playwright")

    return {
        "engine": _get_rc().ENGINE_VERSION,
        "render_available": bool(browsers),
        "render_drivers": browsers,
        "render_hint": ("" if browsers else
                        "未安装浏览器依赖，--render 不可用。"
                        "安装命令: python -m pip install playwright && python -m playwright install chromium"),
        "curl_cffi": _has("curl_cffi"),
        "impersonates": ["", "chrome", "chrome110", "chrome123", "safari", "firefox", "edge101"],
        "features": ["sitemap 种子", "内联 JS 提取", "429 退避重试", "跳过已抓页面",
                     "每域名并发", "自适应限速", "断点续爬", "响应缓存",
                     "浏览器渲染", "自定义字段提取",
                     # v1.5.0
                     "自愈选择器", "XHR 接口捕获", "代理轮换与失败淘汰", "Markdown 导出"],
    }


# ---------------------------------------------------------------------------
# 反爬对抗模块接口（anticrawl.py）
# ---------------------------------------------------------------------------


def api_decode(payload):
    return _get_ac().TextDecoder.decode_chain(payload.get("text") or "")


def api_font(payload):
    if not (payload.get("url") or "").strip():
        return {"error": "请先填写目标页面 URL（含 @font-face 的页面）"}
    f = _get_ac().FontAntiCrawl(cookie=payload.get("cookie") or "",
                         verify=not bool(payload.get("no_verify")))
    return f.full_run(payload.get("url") or "",
                      text=payload.get("text") or "",
                      ref_font_path=payload.get("ref_font") or None)


def api_water(payload):
    action = payload.get("action") or "detect"
    try:
        data = base64.b64decode(payload.get("image_b64") or "")
    except Exception:
        return {"error": "图片数据解码失败"}
    if not data:
        return {"error": "未收到图片数据"}
    if action == "clean":
        cleaned, method = _get_ac().WatermarkTool.clean(data, payload.get("method") or "auto")
        return {"action": "clean", "method_used": method,
                "image_b64": base64.b64encode(cleaned).decode("ascii"),
                "bytes": len(cleaned)}
    return _get_ac().WatermarkTool.detect(data, payload.get("name") or "image")


def api_auth(payload):
    t = _get_ac().AuthTester(cookie=payload.get("cookie") or "",
                      verify=not bool(payload.get("no_verify")))
    return t.test(payload.get("url") or "",
                  method=payload.get("method") or "GET",
                  body_json=payload.get("body_json") or None)


def api_js(payload):
    js = _get_ac().JSAnalyzer(cookie=payload.get("cookie") or "",
                       verify=not bool(payload.get("no_verify")))
    action = payload.get("action") or "scan"
    if action == "scan":
        url = payload.get("url") or ""
        out = js.scan(url)
        out["key_candidates"] = js.extract_keys(url)
        return out
    if action == "run":
        return _get_ac().JSAnalyzer.run_script(payload.get("script") or "")
    if action == "replay":
        return _get_ac().JSAnalyzer.replay(payload.get("captures") or "[]",
                                    cookie=payload.get("cookie") or "",
                                    verify=not bool(payload.get("no_verify")))
    return {"error": "未知的 js action"}


def api_waf(payload):
    return _get_ac().WAFDetector(cookie=payload.get("cookie") or "",
                          verify=not bool(payload.get("no_verify"))).detect(payload.get("url") or "")


def api_challenge(payload):
    return _get_ac().ChallengeBypass(cookie=payload.get("cookie") or "",
                              verify=not bool(payload.get("no_verify"))).analyze(payload.get("url") or "")


def api_captcha(payload):
    return _get_ac().CaptchaDetector(cookie=payload.get("cookie") or "",
                              verify=not bool(payload.get("no_verify"))).detect(payload.get("url") or "")


def api_proxy_test(payload):
    """逐条测试代理连通性，返回每个代理的出口信息。"""
    text = str(payload.get("proxies") or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return {"error": "代理列表为空（每行一个，如 http://127.0.0.1:7890）"}
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    results = []
    target = str(payload.get("target") or "http://ifconfig.me/ip")
    for proxy in lines:
        row = {"proxy": proxy}
        try:
            r = requests.get(target, proxies={"http": proxy, "https": proxy},
                             timeout=8, verify=False)
            row["status"] = r.status_code
            row["body"] = (r.text[:80].strip() or "")
        except Exception as exc:
            row["status"] = "ERR"
            row["body"] = str(exc)[:100]
        results.append(row)
    return {"results": results}


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    # v1.4.3 性能：HTTP/1.1 长连接，浏览器复用连接，轮询不再每次重新握手
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---- 工具 ----

    def _send(self, body: bytes, ctype: str, code=200, download_name=None,
              cache="no-store", etag=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _etag_of(self, *paths):
        """按文件大小 + mtime 生成弱 ETag，文件一变就失效，开发时不会拿到旧样式。"""
        import hashlib
        h = hashlib.sha1()
        for p in paths:
            try:
                st = os.stat(p)
                h.update(f"{st.st_mtime_ns}|{st.st_size}|".encode())
            except OSError:
                pass
        return '"' + h.hexdigest()[:20] + '"'

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    # ---- GET ----

    # ---- 主题兜底 ----

    @staticmethod
    def _with_inline_theme(html):
        """把 apple.css 的内容直接嵌进页面再返回。

        <link> 引入在个别机器上会拿不到（文件缺失 / 老内核的奇怪缓存策略），
        嵌进页面后只靠一次请求就能拿到完整样式。原来的 <link> 保留不动，
        两边规则一致，重复应用没有副作用。
        """
        try:
            with open(_resource("apple.css"), "rb") as fh:
                css = fh.read()
        except OSError:
            return html
        tag = b'<style id="apple-inline">\n' + css + b'\n</style>\n</head>'
        return html.replace(b"</head>", tag, 1) if b"</head>" in html else html

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                if not os.path.exists(HTML_FILE):
                    self._json({"error": "缺少 webui.html，请与 webui.py 放在同一目录"}, 500)
                    return
                etag = self._etag_of(HTML_FILE, _resource("apple.css"))
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                with open(HTML_FILE, "rb") as fh:
                    self._send(self._with_inline_theme(fh.read()),
                               "text/html; charset=utf-8", cache="no-cache", etag=etag)
            elif path in ("/apple.css", "/theme.css"):
                # 视觉覆盖层：只做外观，不动 JS。删掉 webui.html 里的引用即可回到原样式
                css = _resource("apple.css")
                if not os.path.exists(css):
                    self._json({"error": "缺少 apple.css"}, 404)
                    return
                etag = self._etag_of(css)
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                with open(css, "rb") as fh:
                    self._send(fh.read(), "text/css; charset=utf-8",
                               cache="no-cache", etag=etag)
            elif path in ("/gecko_favicon.png", "/logo.png", "/assets/gecko_favicon.png"):
                # 壁虎标志（透明底 PNG）。找不到就退回内联 SVG，界面不会开天窗
                png = _resource("gecko_favicon.png")
                if not os.path.exists(png):
                    png = os.path.join(_PROJECT_ROOT, "assets", "gecko_favicon.png")
                if os.path.exists(png):
                    with open(png, "rb") as fh:
                        self._send(fh.read(), "image/png", cache="public, max-age=86400")
                else:
                    self._send(_fallback_icon_svg().encode("utf-8"), "image/svg+xml")
            elif path == "/favicon.ico":
                ico = _resource("gecko.ico")
                if not os.path.exists(ico):
                    ico = os.path.join(_PROJECT_ROOT, "assets", "gecko.ico")
                if os.path.exists(ico):
                    with open(ico, "rb") as fh:
                        self._send(fh.read(), "image/x-icon", cache="public, max-age=86400")
                else:
                    self._send(_fallback_icon_svg().encode("utf-8"), "image/svg+xml")
            elif path == "/api/state":
                rev = qs.get("rev", [None])[0]
                self._json(manager.snapshot(int(rev) if rev is not None and rev.isdigit() else None))
            elif path in ("/api/engines", "/api/capabilities"):
                # /api/engines 是旧路径，保留别名避免旧页面 404
                self._json(capability_info())
            elif path == "/api/files":
                self._json(list_report_files())
            elif path == "/api/file":
                self._serve_report(qs, download=False)
            elif path == "/api/download":
                self._serve_report(qs, download=True)
            else:
                self._json({"error": "接口不存在"}, 404)
        except Exception as exc:  # 服务端异常不中断
            self._json({"error": f"服务器内部错误: {exc}"}, 500)

    def _serve_report(self, qs, download):
        names = qs.get("name") or []
        if not names:
            self._json({"error": "缺少 name 参数"}, 400)
            return
        name = os.path.basename(names[0])  # 防目录穿越
        out = manager.snapshot().get("output_dir")
        if not out:
            self._json({"error": "还没有输出目录"}, 400)
            return
        try:
            real_out = os.path.realpath(out)
            real_file = os.path.realpath(os.path.join(out, name))
            if not real_file.startswith(real_out + os.sep) or not os.path.isfile(real_file):
                self._json({"error": "文件不存在"}, 404)
                return
        except OSError:
            self._json({"error": "文件不存在"}, 404)
            return
        size = os.path.getsize(real_file)
        with open(real_file, "rb") as fh:
            if download:
                data = fh.read()
                self._send(data, "application/octet-stream", download_name=name)
            else:
                limit = 300 * 1024
                data = fh.read(limit)
                if size > limit:
                    data += f"\n\n... [内容过长已截断，共 {size} 字节，可点下载获取完整文件]".encode("utf-8")
                ctype = "text/plain; charset=utf-8"
                if name.endswith(".json"):
                    ctype = "application/json; charset=utf-8"
                elif name.endswith(".csv"):
                    ctype = "text/csv; charset=utf-8"
                self._send(data, ctype)

    # ---- POST ----

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        try:
            if path == "/api/start":
                self._json(manager.start(payload))
            elif path == "/api/stop":
                self._json(manager.stop())
            elif path == "/api/decode":
                self._json(api_decode(payload))
            elif path == "/api/font":
                self._json(api_font(payload))
            elif path == "/api/water":
                self._json(api_water(payload))
            elif path == "/api/auth":
                self._json(api_auth(payload))
            elif path == "/api/js":
                self._json(api_js(payload))
            elif path == "/api/waf":
                self._json(api_waf(payload))
            elif path == "/api/challenge":
                self._json(api_challenge(payload))
            elif path == "/api/captcha":
                self._json(api_captcha(payload))
            elif path == "/api/proxy-test":
                self._json(api_proxy_test(payload))
            else:
                self._json({"error": "接口不存在"}, 404)
        except Exception as exc:
            self._json({"error": f"服务器内部错误: {exc}"}, 500)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def pick_port(start_port):
    for port in range(start_port, start_port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            return port, srv
        except OSError:
            continue
    return None, None


def find_app_browser():
    """找一个能以「应用窗口」模式启动的浏览器（Edge / Chrome）。

    --app=URL 会开一个没有地址栏、没有标签页的独立窗口，看起来就是一个桌面程序。
    """
    local = os.environ.get("LOCALAPPDATA") or ""
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)") or "", r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles") or "", r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles") or "", r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)") or "", r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def open_app_window(url, exe):
    """用 --app 模式打开无地址栏窗口；失败返回 False 由调用方回退。"""
    try:
        subprocess.Popen(
            [exe, f"--app={url}", "--window-size=1440,940",
             "--disable-features=Translate", "--no-first-run",
             "--no-default-browser-check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def pywebview_available():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def run_pywebview(url, title="Gecko"):
    """内嵌 WebView 桌面窗口：没有地址栏，双击就是一个独立应用。"""
    import webview
    webview.create_window(title, url, width=1440, height=940, min_size=(1000, 700))
    kwargs = {}
    if sys.platform.startswith("win"):
        # 实测：Windows 上 private_mode=True 时 WebView2 起不来
        # （"Main window failed to start"），必须用非隐私模式 + 显式 edgechromium。
        kwargs.update(private_mode=False, gui="edgechromium")
    webview.start(**kwargs)   # 阻塞，窗口关闭后返回


def main():
    parser = argparse.ArgumentParser(description="Gecko 图形界面（本地服务）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开任何窗口")
    parser.add_argument("--window", choices=["auto", "pywebview", "app", "browser", "none"],
                        default="auto",
                        help="打开方式：auto=优先桌面窗口；pywebview=内嵌 WebView 独立窗口；"
                             "app=Edge/Chrome 无地址栏应用窗口；browser=系统默认浏览器；none=不开窗口")
    args = parser.parse_args()
    mode = "none" if args.no_browser else args.window

    port, srv = pick_port(args.port)
    if srv is None:
        print(f"[ERROR] 端口 {args.port}-{args.port + 9} 均被占用，请换端口重试。", flush=True)
        sys.exit(1)
    if port != args.port:
        print(f"[WARN] 端口 {args.port} 已被占用（可能是之前的旧实例还在运行），"
              f"本次改用 {port}。若页面显示异常，请结束旧的 python 进程后重开。", flush=True)

    url = f"http://127.0.0.1:{port}/"
    if mode == "auto":
        # 先试无地址栏应用窗口（直接复用 Edge/Chrome 内核，最稳），
        # 其次内嵌桌面窗口，最后才是系统浏览器——默认浏览器可能是老 IE 内核，
        # 页面脚本会整体失效（表现为按钮点了没反应），所以放最后
        if find_app_browser():
            mode = "app"
        elif pywebview_available():
            mode = "pywebview"
        else:
            mode = "browser"

    how = {"pywebview": "桌面窗口（内嵌 WebView，无地址栏）",
           "app": "应用窗口（Edge/Chrome，无地址栏）",
           "browser": "系统默认浏览器",
           "none": "不开窗口（请手动访问下面的地址）"}.get(mode, mode)
    print("=" * 52, flush=True)
    print("  Gecko 图形界面已启动", flush=True)
    print(f"  打开方式: {how}", flush=True)
    print(f"  本地地址: {url}", flush=True)
    print("  关闭窗口即可停止服务（正在进行的爬取会中断）", flush=True)
    print("=" * 52, flush=True)

    if mode == "pywebview":
        # 服务放后台线程，主线程留给 GUI 事件循环（Windows 要求 GUI 在主线程）
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            run_pywebview(url)
        except Exception as exc:
            print(f"[WARN] 桌面窗口不可用（{exc}），改用 Edge/Chrome 应用窗口。", flush=True)
            exe = find_app_browser()
            if exe and open_app_window(url, exe):
                print("       已打开应用窗口。", flush=True)
            else:
                print("       未找到 Edge/Chrome，改用系统默认浏览器打开。", flush=True)
                webbrowser.open(url)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n已停止服务。", flush=True)
        finally:
            try:
                srv.shutdown()
            except Exception:
                pass
            srv.server_close()
        return

    if mode == "app":
        exe = find_app_browser()
        if exe:
            threading.Timer(0.8, lambda: open_app_window(url, exe)).start()
        else:  # 找不到 Edge/Chrome 就退回默认浏览器
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    elif mode == "browser":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
