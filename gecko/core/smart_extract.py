# -*- coding: utf-8 -*-
"""结构化数据提取与导出（v4.0 · 企业级）

为什么要有这一层
----------------
旧版工具能"把页面抓回来"，但抓回来的是一堆 HTML —— 真正想要的是
**可直接进 Excel / 数据库 / BI 的字段**。这一步过去要靠使用者自己再写一遍解析脚本，
等于工具只做了一半。v4.0 把这一半补上：

    HTML ──▶ 多数据源并行提取 ──▶ 扁平记录 ──▶ CSV / JSONL / SQLite / XLSX

提取的数据源，按结构化程度（可信度）从高到低排列：

1. JSON-LD   ``<script type="application/ld+json">`` —— schema.org 官方格式，
             电商商品、招聘岗位、新闻、企业信息几乎必带，字段最干净
2. Microdata ``itemscope/itemprop/itemtype`` —— 老站仍在用，同样源自 schema.org
3. Meta      OG / Twitter Card / description / keywords / generator —— 标题摘要这一层
4. Tables    ``<table>`` —— 报价表、参数表、榜单，中小企业官网最常见的数据载体
5. 联系方式   正文里的邮箱 / 电话 / QQ / 微信 —— 官网页面上很难找却极有价值的字段

设计原则
--------
* **只做提取，不做猜测**：拿不到就留空，绝不编造 —— 这是对数据负责的最低要求。
* **全流程异常兜底**：HTML 再畸形也不能让整条爬取中断 —— 跑量时的铁律。
* **大页面主动限流**：超过 SIZE_LIMIT 不再做全文正则扫描，避免拖垮整体吞吐。
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime

try:
    from bs4 import BeautifulSoup
except ImportError:                                    # pragma: no cover
    BeautifulSoup = None

# 超过这个体积的 HTML 不再做全文正则扫描（表格/联系方式捞取是 O(n) 的，超大页会拖慢整体）
SIZE_LIMIT = 3 * 1024 * 1024
VALUE_LEN = 800          # 单个字段最大长度，防止一个字段塞进整篇正文
MAX_TABLES = 20          # 单页最多导出多少张表
MAX_ROWS = 2000          # 单表最多导出多少行


def _soup(html):
    """按可用性选择解析器，失败统一返回 None（调用方负责跳过）。"""
    if BeautifulSoup is None or not html:
        return None
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def clean(v, limit=VALUE_LEN):
    """把任意取值压成"适合进表格的字符串"：去控制字符、压空白、限长。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (list, tuple, set)):
        v = " | ".join(clean(x, VALUE_LEN) for x in v if x not in (None, ""))
    s = str(v)
    s = s.replace("\x00", "")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


# ---------------------------------------------------------------------------
# 1. JSON-LD
# ---------------------------------------------------------------------------

def _load_jsonld(raw):
    """JSON-LD 常常写到"能跑但不严谨"，这里做容错：去注释、去尾逗号后重试。"""
    raw = (raw or "").strip().lstrip("\ufeff")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        cleaned = re.sub(r"//.*?(?=\n|$)", "", raw)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        return json.loads(cleaned)
    except ValueError:
        return None


def _iter_jsonld_nodes(obj):
    """把 @graph 嵌套 / 数组 / itemListElement 全部摊平成一条条独立节点。

    一个页面往往同时声明 Organization + WebSite + BreadcrumbList，
    只取第一个会丢掉八成信息，所以必须递归展开。
    """
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_jsonld_nodes(x)
    elif isinstance(obj, dict):
        for g in (obj.get("@graph") or []):
            yield from _iter_jsonld_nodes(g)
        if obj.get("@type") or obj.get("name") or obj.get("headline"):
            yield obj
        for key in ("itemListElement", "mainEntity", "item", "hasVariant"):
            sub = obj.get(key)
            if isinstance(sub, (list, dict)):
                for x in (sub if isinstance(sub, list) else [sub]):
                    yield from _iter_jsonld_nodes(x.get("item", x)
                                                  if isinstance(x, dict) else x)


def extract_jsonld(soup):
    """抽出页面上所有 JSON-LD 节点，返回 list[dict]。"""
    out = []
    if soup is None:
        return out
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        data = _load_jsonld(tag.string or tag.get_text() or "")
        if data is None:
            continue
        for node in _iter_jsonld_nodes(data):
            if not isinstance(node, dict):
                continue
            rec = {}
            for k, v in node.items():
                if k.startswith("@"):
                    if k == "@type":
                        rec["类型"] = clean(v)
                    continue
                if isinstance(v, str):
                    if "<" in v:                       # JSON-LD 里常混着 <a>/<br>
                        v = re.sub(r"<[^>]+>", "", v)
                    v = clean(v)
                # dict / list 原样保留 —— 下游 flatten_schema 负责把
                # offers / brand / aggregateRating 这类嵌套降维成可用字段；
                # 这里若提前压成字符串，降维就无从下手（曾踩过这个坑）。
                rec[clean(k, 60)] = v
            if rec:
                out.append(rec)
    return out[:50]


# ---------------------------------------------------------------------------
# 2. Microdata
# ---------------------------------------------------------------------------

def extract_microdata(soup):
    """抽出 Microdata 条目（itemscope 容器内的所有 itemprop）。"""
    out = []
    if soup is None:
        return out
    for scope in soup.find_all(attrs={"itemscope": True}):
        rec = {}
        itype = scope.get("itemtype")
        if itype:
            rec["类型"] = clean(itype.rstrip("/").split("/")[-1], 80)
        for prop in scope.find_all(attrs={"itemprop": True}):
            name = clean(prop.get("itemprop"), 60)
            if not name or name in rec:
                continue
            val = (prop.get("content") or prop.get("href") or prop.get("src")
                   or prop.get("datetime") or "")
            if not val:
                val = prop.get_text(" ", strip=True)
            rec[name] = clean(val)
        if len(rec) > 1:
            out.append(rec)
    return out[:50]


# ---------------------------------------------------------------------------
# 3. Meta / 头部字段
# ---------------------------------------------------------------------------

OG_KEYS = ("og:title", "og:description", "og:image", "og:type", "og:site_name",
           "og:url", "og:price:amount", "og:price:currency", "article:published_time",
           "article:modified_time", "article:author", "product:price:amount")
META_NAMES = ("description", "keywords", "author", "generator", "robots",
              "publishdate", "publish_date", "copyright")


def extract_meta(soup, final_url=""):
    """标题 / 摘要 / 关键词 / OG / 站点名 / 语言 / favicon —— 每个站都有，最稳的一层。"""
    rec = {}
    if soup is None:
        return rec
    if soup.title and soup.title.string:
        rec["标题"] = clean(soup.title.string)
    h1 = soup.find("h1")
    if h1:
        rec["主标题"] = clean(h1.get_text(" ", strip=True))
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        rec["语言"] = clean(html_tag.get("lang"), 20)

    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if not key or not content:
            continue
        if key in OG_KEYS or key in META_NAMES or key.startswith("twitter:"):
            rec[clean(key, 60)] = clean(content)

    for rel, label in (("canonical", "规范地址"), ("shortlink", "短地址")):
        link = soup.find("link", rel=lambda r, _rel=rel: r and _rel in str(r).lower())
        if link and link.get("href"):
            rec[label] = clean(link.get("href"))
    icon = soup.find("link", rel=lambda r: r and "icon" in str(r).lower())
    if icon and icon.get("href"):
        rec["站点图标"] = clean(icon.get("href"))
    return rec


# ---------------------------------------------------------------------------
# 4. 表格
# ---------------------------------------------------------------------------

def extract_tables(soup):
    """抽出页面上的 <table>，整理成 {index, headers, rows}。

    报价表、参数表、排行榜在企业官网里几乎都是 table ——
    这是"看起来最土但最好用"的数据源。
    """
    tables = []
    if soup is None:
        return tables
    for idx, tb in enumerate(soup.find_all("table")[:MAX_TABLES]):
        headers, rows = [], []
        for tr in tb.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            vals = [clean(c.get_text(" ", strip=True), 300) for c in cells]
            if not any(vals):
                continue
            if not headers and tr.find_all("th"):
                headers = vals
            else:
                rows.append(vals)
        if rows:
            tables.append({"index": idx, "headers": headers, "rows": rows[:MAX_ROWS]})
    return tables


# ---------------------------------------------------------------------------
# 5. 联系方式
# ---------------------------------------------------------------------------

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 手机号 / 带区号或分隔符的座机 —— 形态明确，可直接判定
RE_PHONE = re.compile(
    r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}-?\d{7,8}|\d{3,4}-\d{7,8}(?:-\d{1,5})?)(?!\d)")
# 一串裸数字要先看它前面是不是"电话/手机/…"才算数。
# 不做这层约束的话，正文里的 QQ 号、订单号、备案号都会被当成电话，整列数据报废。
PHONE_CTX = ("电话", "手机", "座机", "固话", "联系方式", "热线", "传真", "总机",
             "tel", "phone", "mobile", "fax", "contact")
RE_CTX_PHONE = re.compile(
    r"(?:电话|手机|座机|固话|联系方式|热线|传真|总机|tel|phone|mobile|fax|contact)"
    r"[^0-9]{0,6}(\d{7,11})", re.I)
RE_QQ = re.compile(r"(?:QQ|qq|扣扣)[^0-9]{0,4}(\d{5,12})")
RE_WECHAT = re.compile(r"(?:微信|weixin|WeChat|WX)[^A-Za-z0-9_-]{0,4}([A-Za-z0-9_-]{5,20})")


def extract_contacts(text, limit=SIZE_LIMIT):
    """从正文里捞联系方式。企业名录 / 招商线索采集基本就靠这组字段。"""
    out = {}
    if not text:
        return out
    if len(text) > limit:
        text = text[:limit]
    emails, phones = [], []

    def _add(bucket, val):
        """去重 + 至少 7 位数字才算电话，过滤掉纯噪音。"""
        v = str(val).strip()
        if len(re.sub(r"\D", "", v)) >= 7 and v not in bucket:
            bucket.append(v)

    for e in RE_EMAIL.findall(text):
        if e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".css", ".js")):
            continue                       # 过滤静态资源里的假邮箱
        if e not in emails:
            emails.append(e)
    for p in RE_PHONE.findall(text):
        _add(phones, p)
    for p in RE_CTX_PHONE.findall(text):
        _add(phones, p)
    if emails:
        out["邮箱"] = " | ".join(emails[:5])
    if phones:
        out["电话"] = " | ".join(phones[:5])
    qq = RE_QQ.search(text)
    if qq:
        out["QQ"] = qq.group(1)
    wx = RE_WECHAT.search(text)
    if wx:
        out["微信"] = wx.group(1)
    return out


# ---------------------------------------------------------------------------
# 汇总：一个页面 -> 一条扁平记录
# ---------------------------------------------------------------------------

PRIORITY_TYPES = ("Product", "Article", "NewsArticle", "JobPosting", "Organization",
                  "LocalBusiness", "Event", "Recipe", "Review", "Offer", "Person",
                  "BlogPosting", "SoftwareApplication", "Course", "Hotel", "Restaurant")

KEY_MAP = {
    "name": "名称", "headline": "名称", "title": "名称",
    "description": "描述", "abstract": "描述", "summary": "描述",
    "availability": "库存", "itemCondition": "成色", "releaseDate": "上市时间",
    "offers": "价格", "price": "价格", "priceCurrency": "币种", "lowPrice": "最低价",
    "brand": "品牌", "sku": "货号", "gtin13": "条码", "mpn": "型号",
    "datePublished": "发布时间", "datePosted": "发布时间", "published_time": "发布时间",
    "dateModified": "更新时间", "author": "作者", "publisher": "发布方",
    "articleBody": "正文", "text": "正文",
    "telephone": "电话", "email": "邮箱", "url": "官网", "address": "地址",
    "addressLocality": "城市", "addressRegion": "省份", "streetAddress": "街道",
    "postalCode": "邮编", "image": "图片", "logo": "图标",
    "ratingValue": "评分值", "reviewCount": "评价数", "keywords": "关键词",
    "jobTitle": "职位", "hiringOrganization": "招聘单位", "baseSalary": "薪资",
    "employmentType": "用工形式", "experienceRequirements": "经验要求",
    "educationRequirements": "学历要求", "jobLocation": "工作地点",
    "category": "分类", "applicationCategory": "分类", "operatingSystem": "系统",
}


def _pick_main_type(nodes):
    """挑出这条记录"最值钱"的 schema 类型，便于按类分组查看。"""
    for n in nodes:
        t = str(n.get("类型", ""))
        for p in PRIORITY_TYPES:
            if p.lower() in t.lower():
                return p
    return nodes[0].get("类型", "") if nodes else ""


# 这些子键被视为"占位性质的主值"：当嵌套对象只产出它时，改用父键的中文映射命名。
# 例：brand:{"@type":"Brand","name":"华东智控"} -> 品牌=华东智控（而不是 名称=华东智控）
MAIN_KEYS = {"名称", "值", "网址", "正文", "描述", "NaN"}
MAX_DEPTH = 4          # schema 嵌套递归上限，防御畸形数据里的自引用


def flatten_schema(node, depth=0):
    """把一条 schema 记录递归压平成一层的 {"中文字段": 值}。

    JSON-LD 里嵌套是常态：price 藏在 offers 里、评分藏在 aggregateRating 里、
    城市藏在 address 里。不平铺的话导出到 Excel 就是一串 Python 字典字符串，
    等于白提取。命名优先级：KEY_MAP 映射 > 原字段名（冲突时不覆盖，先到先得）。
    """
    out = {}
    if depth > MAX_DEPTH or not isinstance(node, dict):
        return out
    for k, v in node.items():
        if k.startswith("@") or v in ("", None):
            continue
        cn = KEY_MAP.get(k) or k
        if isinstance(v, dict):
            sub = flatten_schema(v, depth + 1)
            # 子对象只产出一个主值时，继承父字段名；否则按自身键名铺开
            if len(sub) == 1:
                only_k, only_v = next(iter(sub.items()))
                out.setdefault(cn if only_k in MAIN_KEYS else only_k, only_v)
            else:
                for sk, sv in sub.items():
                    out.setdefault(sk, sv)
        elif isinstance(v, list):
            dicts = [x for x in v if isinstance(x, dict)]
            scalars = [clean(x) for x in v if not isinstance(x, (dict, list))]
            if dicts:
                for d in dicts[:3]:
                    for sk, sv in flatten_schema(d, depth + 1).items():
                        if len(flatten_schema(d, depth + 1)) == 1 and sk in MAIN_KEYS:
                            out.setdefault(cn, sv)
                        else:
                            out.setdefault(sk, sv)
            if scalars:
                out.setdefault(cn, " | ".join([s for s in scalars if s]))
        else:
            out.setdefault(cn, clean(v) if isinstance(v, str) else v)
    return out


def build_record(url, html, soup=None, extra=None):
    """一个页面 -> 一条可直接入库的宽表记录。返回 (record, tables)。"""
    soup = soup or _soup(html)
    rec = {"网址": url}
    if extra:
        rec.update(extra)

    jsonld = extract_jsonld(soup) if soup is not None else []
    micro = extract_microdata(soup) if soup is not None else []
    tables = extract_tables(soup) if soup is not None else []

    # 先铺 meta（最稳），再用结构化数据覆盖 —— 后者可信度更高
    for k, v in extract_meta(soup, url).items():
        rec.setdefault(k, v)
    rec.setdefault("标题", clean(soup.title.string if soup and soup.title else ""))

    nodes = jsonld + micro
    rec["数据类型"] = _pick_main_type(nodes)
    rec["结构化来源"] = "+".join(
        [n for n, has in (("JSON-LD", jsonld), ("Microdata", micro),
                          ("表格", tables)) if has]) or "HTML"
    for node in nodes[:6]:
        for k, v in flatten_schema(node).items():
            if k not in rec or not rec[k]:
                rec[k] = v
    rec["结构化条目数"] = len(nodes)

    if tables:
        rec["表格数"] = len(tables)
        rec["表格行数"] = sum(len(t["rows"]) for t in tables)

    if soup is not None:
        try:
            text = soup.get_text(" ", strip=True)
        except Exception:
            text = ""
        if not text:
            text = re.sub(r"<[^>]+>", " ", html or "")
        for k, v in extract_contacts(text).items():
            rec.setdefault(k, v)
        rec["正文首段"] = clean(re.sub(r"\s{2,}", " ", text)[:500])

    return rec, tables


# ---------------------------------------------------------------------------
# 导出器
# ---------------------------------------------------------------------------

CREATE_PAGES = """CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY, final_url TEXT, status INTEGER, content_type TEXT,
  title TEXT, depth INTEGER, size INTEGER, content_hash TEXT, fetched_at TEXT)"""

CREATE_ITEMS = """CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, data_type TEXT,
  fields_json TEXT, fetched_at TEXT)"""

CREATE_TABLES = """CREATE TABLE IF NOT EXISTS tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, table_index INTEGER,
  headers TEXT, rows_json TEXT)"""


def _excel_safe(v):
    """Excel 拒绝 NUL 与多数控制字符；超长会被截断，这里先自查。"""
    if isinstance(v, (int, float)):
        return v
    s = str(v or "")
    s = "".join(ch for ch in s if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return s[:32000]


class Exporter:
    """把提取结果落到多种载体。

    SQLite 是**边跑边写**（流式），爬 10 万页也不会把内存撑爆；
    CSV / XLSX / JSONL 需要全量列集合，所以在收尾时统一落盘。
    """

    def __init__(self, output_dir, formats, quiet=False):
        self.dir = output_dir
        self.formats = [f.strip().lower() for f in (formats or []) if f.strip()]
        self.quiet = quiet
        self.lock = threading.Lock()
        self._items = []
        self._tables = []
        self._conn = None
        self._n = 0
        if "sqlite" in self.formats:
            self._conn = self._open_db()

    # ---- SQLite ----
    def _open_db(self):
        try:
            conn = sqlite3.connect(os.path.join(self.dir, "data.db"), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")     # 并发写更快、更抗崩
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(CREATE_PAGES)
            conn.execute(CREATE_ITEMS)
            conn.execute(CREATE_TABLES)
            conn.commit()
            return conn
        except sqlite3.Error as exc:
            if not self.quiet:
                print(f"[导出] SQLite 初始化失败，跳过该格式: {exc}")
            return None

    def add_page(self, rec):
        """写一条响应级记录到 pages 表。"""
        if not self._conn:
            return
        with self.lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                    (rec.get("url", ""), rec.get("final_url", ""), rec.get("status"),
                     rec.get("content_type", ""), rec.get("title", ""), rec.get("depth"),
                     rec.get("size"), rec.get("content_hash", ""),
                     datetime.now().isoformat(timespec="seconds")))
                self._n += 1
                if self._n % 200 == 0:                  # 批量提交，兼顾性能与安全
                    self._conn.commit()
            except sqlite3.Error:
                pass

    def add_record(self, rec, tables=None):
        """写一条结构化记录（items 表）+ 可选表格（tables 表）。"""
        with self.lock:
            self._items.append(rec)
        if self._conn:
            with self.lock:
                try:
                    self._conn.execute(
                        "INSERT INTO items (url,data_type,fields_json,fetched_at)"
                        " VALUES (?,?,?,?)",
                        (rec.get("网址", ""), rec.get("数据类型", ""),
                         json.dumps(rec, ensure_ascii=False),
                         datetime.now().isoformat(timespec="seconds")))
                except sqlite3.Error:
                    pass
        if tables:
            with self.lock:
                for t in tables:
                    self._tables.append({"url": rec.get("网址", ""), **t})
            if self._conn:
                with self.lock:
                    try:
                        for t in tables:
                            self._conn.execute(
                                "INSERT INTO tables (url,table_index,headers,rows_json)"
                                " VALUES (?,?,?,?)",
                                (rec.get("网址", ""), t.get("index"),
                                 json.dumps(t.get("headers") or [], ensure_ascii=False),
                                 json.dumps(t.get("rows") or [], ensure_ascii=False)))
                    except sqlite3.Error:
                        pass

    # ---- 收尾落盘 ----
    def close(self):
        written = []
        if self._conn:
            try:
                self._conn.commit()
                self._conn.close()
                written.append(os.path.join(self.dir, "data.db"))
            except sqlite3.Error:
                pass
            self._conn = None

        items, tables = self._items, self._tables
        self._items, self._tables = [], []
        if not items:
            return written

        keys = []
        for r in items:
            for k in r:
                if k not in keys:
                    keys.append(k)

        if "jsonl" in self.formats:
            p = os.path.join(self.dir, "structured.jsonl")
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    for r in items:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                written.append(p)
            except OSError:
                pass

        if "csv" in self.formats:
            p = os.path.join(self.dir, "structured.csv")
            try:
                with open(p, "w", encoding="utf-8-sig", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
                    w.writeheader()
                    for r in items:
                        w.writerow({k: _excel_safe(r.get(k, "")) for k in keys})
                written.append(p)
            except OSError:
                pass

            if tables:
                p = os.path.join(self.dir, "tables.csv")
                try:
                    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
                        w = csv.writer(fh)
                        w.writerow(["来源网址", "表序号", "行序号", "内容"])
                        for t in tables:
                            for i, row in enumerate(t.get("rows") or [], 1):
                                w.writerow([t.get("url", ""), t.get("index"), i,
                                            " | ".join(str(x) for x in row)])
                    written.append(p)
                except OSError:
                    pass

        if "xlsx" in self.formats:
            p = self._write_xlsx(items, keys, tables)
            if p:
                written.append(p)

        if "md" in self.formats:
            p = self._write_md(items, keys, tables)
            if p:
                written.append(p)

        return written

    def _write_md(self, items, keys, tables):
        """Markdown 表格：喂 LLM / RAG 或贴进文档直接可用。"""
        p = os.path.join(self.dir, "structured.md")

        def cell(v):
            s = "" if v is None else str(v)
            s = s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
            return s[:300]

        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(f"# 结构化数据（{len(items)} 条）\n\n")
                fh.write(f"> 导出时间：{datetime.now().isoformat(timespec='seconds')}\n\n")
                if keys:
                    fh.write("| " + " | ".join(cell(k) for k in keys) + " |\n")
                    fh.write("|" + "|".join("---" for _ in keys) + "|\n")
                    for r in items:
                        fh.write("| " + " | ".join(cell(r.get(k, "")) for k in keys) + " |\n")
                    fh.write("\n")
                if tables:
                    fh.write(f"## 表格数据（{len(tables)} 张）\n\n")
                    for t in tables:
                        rows = t.get("rows") or []
                        heads = t.get("headers") or []
                        fh.write(f"### 来源 {t.get('url', '')} · 表 {t.get('index')}\n\n")
                        if heads:
                            fh.write("| " + " | ".join(cell(h) for h in heads) + " |\n")
                            fh.write("|" + "|".join("---" for _ in heads) + "|\n")
                        for row in rows:
                            fh.write("| " + " | ".join(cell(x) for x in row) + " |\n")
                        fh.write("\n")
            return p
        except OSError as exc:
            if not self.quiet:
                print(f"[导出] Markdown 写入失败: {exc}")
            return None

    def _write_xlsx(self, items, keys, tables):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
        except ImportError:
            if not self.quiet:
                print("[导出] 未安装 openpyxl，跳过 XLSX（pip install openpyxl 可启用）")
            return None
        p = os.path.join(self.dir, "structured.xlsx")
        try:
            wb = Workbook()
            ws = wb.active
            ws.title = "结构化数据"
            head_font = Font(bold=True, color="FFFFFF")
            fill = PatternFill("solid", fgColor="C00000")
            ws.append(keys)
            for c in ws[1]:
                c.font, c.fill = head_font, fill
            for r in items:
                ws.append([_excel_safe(r.get(k, "")) for k in keys])
            ws.freeze_panes = "A2"
            if tables:
                ws2 = wb.create_sheet("表格数据")
                ws2.append(["来源网址", "表序号", "行序号", "内容"])
                for c in ws2[1]:
                    c.font, c.fill = head_font, fill
                for t in tables:
                    for i, row in enumerate(t.get("rows") or [], 1):
                        ws2.append([t.get("url", ""), t.get("index"), i,
                                    " | ".join(str(x) for x in row)])
            wb.save(p)
            return p
        except Exception as exc:
            if not self.quiet:
                print(f"[导出] XLSX 写入失败: {exc}")
            return None
