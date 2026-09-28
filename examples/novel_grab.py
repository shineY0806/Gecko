#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""novel_grab.py —— 长篇小说正文抓取管线（调用 Gecko 站点的通用适配器）

面向「先摸清站点规律 → 批量抓取 → 清洗 → 落地 TXT」的完整流程，命令行驱动。

设计要点
--------
1. **两阶段**：先抓目录页得到章节索引（写 chapters_index.csv），再并发抓章节正文。
2. **断点续跑**：正文按章节落成独立小文件，重跑时已存在的章节直接跳过。
3. **可自证**：``--probe N`` 只做抽取不落盘，打印字数 / 段落数等质量指标，
   可以先小批量验证再决定是否全量跑。
4. **公平爬取**：默认单线程 + 请求间隔，并发与间隔均可通过参数控制。
5. **清洗可插拔**：站点水印通过正则清单过滤，可用 ``--noise`` 追加自定义规则。

依赖
----
    pip install curl_cffi beautifulsoup4

用法示例
--------
    # 1) 先探测 5 章，只看质量指标、不写正文
    python examples/novel_grab.py --probe 5 --catalog-url http://www.uuwx.la/ls/28_28215/

    # 2) 小批量试跑 20 章，落到 output_novel/
    python examples/novel_grab.py --limit 20 --catalog-url http://www.uuwx.la/ls/28_28215/

    # 3) 全量抓取并合并成单文件
    python examples/novel_grab.py --all --workers 4 --delay 0.4 --merge

注意
----
本脚本的行为完全由调用者决定。请仅对你拥有合法权益或已获授权的站点内容使用，
抓取频率请遵守目标站点的 robots 与服务条款。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install beautifulsoup4")

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install curl_cffi")


UA_POOL: Tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

# 站点水印 / 推广行：命中整行丢弃
DEFAULT_NOISE: Tuple[str, ...] = (
    r"最新网址[:：]?",
    r"www\.uuwx\.la",
    r"uuwx\.la",
    r"手机阅读",
    r"天才一秒记住",
    r"请记住本站",
    r"笔趣阁|笔趣看|顶点小说",
    r"^\s*\[[^\]]{0,20}(广告|推荐|APP|app)\]",
)

_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "两": 2,
}


def cn_to_int(text: str) -> int:
    """把「三百六十八」这类中文数字转成 int。

    Args:
        text: 中文数字串，支持到万位。

    Returns:
        对应的阿拉伯数字；无法解析时返回 0。
    """
    total, section, number = 0, 0, 0
    for ch in text.strip():
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        elif ch == "千":
            section += (number or 1) * 1000
            number = 0
        elif ch == "万":
            total += (section + number) * 10000
            section = number = 0
        elif ch.isdigit():
            number = int(ch)
        else:
            break
    return total + section + number


def parse_ordinal(title: str) -> int:
    """从章节标题里取出序号（「第三百六十八节：xxx」→ 368）。

    Args:
        title: 章节标题。

    Returns:
        序号；标题里没有「第X节/第X章」时返回 0。
    """
    match = re.search(r"第([零一二两三四五六七八九十百千万\d]+)[章节]", title)
    return cn_to_int(match.group(1)) if match else 0


@dataclass
class Chapter:
    """一章的目录信息。"""

    cid: str
    title: str
    url: str
    ordinal: int = 0


@dataclass
class ChapterResult:
    """一章的抓取结果。"""

    chapter: Chapter
    body: str = ""
    ok: bool = False
    skipped: bool = False
    error: str = ""
    chars: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.chars = len(self.body)


class NovelGrabber:
    """小说抓取器：串起「取 HTML → 解析 → 清洗 → 落地」四个环节。

    Attributes:
        out_dir: 产出根目录。
        delay: 每次请求后的休眠秒数。
        workers: 并发线程数。
        noise: 水印过滤正则清单。
        content_selector: 正文容器 CSS 选择器。
        title_selector: 章节标题 CSS 选择器。
    """

    def __init__(
        self,
        out_dir: str,
        delay: float = 0.4,
        workers: int = 1,
        noise: Sequence[str] = DEFAULT_NOISE,
        content_selector: str = "div#content",
        title_selector: str = "h1",
        timeout: int = 25,
        retries: int = 3,
    ) -> None:
        self.out_dir = out_dir
        self.delay = delay
        self.workers = max(1, workers)
        self.noise = [re.compile(p, re.I) for p in noise]
        self.content_selector = content_selector
        self.title_selector = title_selector
        self.timeout = timeout
        self.retries = retries
        self.session = curl_requests.Session(impersonate="chrome")
        self.chapter_dir = os.path.join(out_dir, "chapters")
        os.makedirs(self.chapter_dir, exist_ok=True)

    # ---------- 网络层 ----------

    def fetch(self, url: str, referer: Optional[str] = None) -> str:
        """取回 HTML 文本，带重试与 Referer 伪装。

        Args:
            url: 目标 URL。
            referer: 来源页 URL，用于通过防盗链。

        Returns:
            解码后的 HTML 字符串。

        Raises:
            RuntimeError: 重试次数用尽仍然失败。
        """
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(
                    url,
                    timeout=self.timeout,
                    headers={
                        "User-Agent": random.choice(UA_POOL),
                        "Accept-Language": "zh-CN,zh;q=0.9",
                        **({"Referer": referer} if referer else {}),
                    },
                )
                if resp.status_code >= 400:
                    raise RuntimeError("HTTP %s" % resp.status_code)
                return self._decode(resp)
            except Exception as exc:  # noqa: BLE001 - 网络层统一兜底
                last = exc
                if attempt < self.retries:
                    time.sleep(self.delay * attempt * 2)
        raise RuntimeError("抓取失败 %s：%s" % (url, last))

    @staticmethod
    def _decode(resp) -> str:
        """按 headers → meta → 回落顺序判定编码。

        Args:
            resp: curl_cffi 响应对象。

        Returns:
            解码后的字符串。
        """
        raw = resp.content
        enc = None
        ctype = (resp.headers or {}).get("Content-Type", "")
        matched = re.search(r"charset=([\w-]+)", ctype, re.I)
        if matched:
            enc = matched.group(1)
        if not enc:
            head = raw[:2048].decode("ascii", "ignore")
            meta = re.search(r'charset=["\']?([\w-]+)', head, re.I)
            enc = meta.group(1) if meta else None
        for cand in (enc, "utf-8", "gb18030"):
            if not cand:
                continue
            try:
                return raw.decode(cand)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "ignore")

    # ---------- 解析层 ----------

    def parse_catalog(self, html: str, base_url: str) -> List[Chapter]:
        """解析目录页，返回去重排序后的章节列表。

        Args:
            html: 目录页 HTML。
            base_url: 目录页 URL，用于把相对链接补全成绝对链接。

        Returns:
            按「序号 → 章节 ID」排序的章节列表。
        """
        soup = BeautifulSoup(html, "html.parser")
        host_path = urlparse(base_url).path.rstrip("/")
        seen: dict[str, str] = {}
        for anchor in soup.select("a[href]"):
            href = urljoin(base_url, anchor["href"])
            matched = re.search(r"(%s)/(\d+)\.html?$" % re.escape(host_path), href)
            if not matched:
                continue
            title = anchor.get_text(strip=True)
            if len(title) < 2 or title in {"开始阅读", "返回", "首页"}:
                continue
            # 同一 cid 可能被导航与正文区块重复引用，保留更完整的那个标题
            prev = seen.get(matched.group(2))
            if prev is None or len(title) > len(prev):
                seen[matched.group(2)] = title
        chapters = [
            Chapter(cid=cid, title=title, url=urljoin(base_url, "%s/%s.html" % (host_path, cid)))
            for cid, title in seen.items()
        ]
        for chapter in chapters:
            chapter.ordinal = parse_ordinal(chapter.title)
        # 关键：很多站点的「第X节」是按卷重新计数的（本书就有 9 处回落），
        # 一旦按 (ordinal, cid) 排序会把各卷的第一节全部提到最前面，顺序全乱。
        # 章节 ID（cid）通常是按发布时间递增分配的，用它排序才是真实阅读顺序。
        chapters.sort(key=lambda c: int(c.cid) if c.cid.isdigit() else 0)
        return chapters

    @staticmethod
    def detect_volumes(chapters: Sequence["Chapter"]) -> List[int]:
        """依据节号回落切分卷。

        长篇站点常让「第X节」在每卷开头重新计数，节号出现回落即为分卷位置。

        Args:
            chapters: 已按 cid 排序的章节列表。

        Returns:
            每卷起始下标列表，首元素恒为 0。
        """
        starts = [0]
        prev: Optional[int] = None
        for idx, chapter in enumerate(chapters):
            if prev and chapter.ordinal and chapter.ordinal <= prev:
                starts.append(idx)
            if chapter.ordinal:
                prev = chapter.ordinal
        return starts

    def parse_chapter(self, html: str) -> Tuple[str, str]:
        """从章节页抽出标题与正文。

        Args:
            html: 章节页 HTML。

        Returns:
            ``(标题, 正文)`` 二元组；容器缺失时正文为空串。
        """
        soup = BeautifulSoup(html, "html.parser")
        head = soup.select_one(self.title_selector)
        title = head.get_text(strip=True) if head else ""
        container = soup.select_one(self.content_selector)
        if container is None:
            return title, ""
        return title, self.clean(container)

    def clean(self, container) -> str:
        """把正文容器清洗成纯文本。

        处理顺序：去 script/style → 去子 div（通常是广告位）→ `<br>` 转换行
        → 过滤水印行 → 压掉连续空行。

        Args:
            container: BeautifulSoup 节点。

        Returns:
            清洗后的纯文本。
        """
        for junk in container.find_all(["script", "style", "div", "iframe", "ins"]):
            junk.decompose()
        for br in container.find_all("br"):
            br.replace_with("\n")

        lines: List[str] = []
        for line in container.get_text("\n").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(pattern.search(stripped) for pattern in self.noise):
                continue
            lines.append(stripped)

        text = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # ---------- 调度层 ----------

    def chapter_path(self, chapter: Chapter, index: int) -> str:
        """返回章节正文的落盘路径（零填充序号保证排序稳定）。"""
        name = "%05d_%s.txt" % (index, chapter.cid)
        return os.path.join(self.chapter_dir, name)

    def grab_one(self, chapter: Chapter, index: int, base_url: str, force: bool = False) -> ChapterResult:
        """抓取单章；已存在且非 force 时直接跳过。

        Args:
            chapter: 章节元信息。
            index: 在全书中的位次（用于文件命名）。
            base_url: 目录页 URL，用作 Referer。
            force: 为 True 时即使已存在也重新抓取。

        Returns:
            抓取结果对象。
        """
        path = self.chapter_path(chapter, index)
        if not force and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return ChapterResult(chapter, handle.read(), ok=True, skipped=True)
        try:
            html = self.fetch(chapter.url, referer=base_url)
            title, body = self.parse_chapter(html)
            if title and not chapter.title:
                chapter.title = title
            if not body:
                return ChapterResult(chapter, "", ok=False, error="正文容器为空")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("%s\n\n%s\n" % (chapter.title, body))
            time.sleep(self.delay)
            return ChapterResult(chapter, body, ok=True)
        except Exception as exc:  # noqa: BLE001 - 单章失败不影响整批
            return ChapterResult(chapter, "", ok=False, error=str(exc)[:160])

    def grab_all(
        self, chapters: Sequence[Chapter], base_url: str, force: bool = False
    ) -> List[ChapterResult]:
        """并发抓取全部章节。

        Args:
            chapters: 章节列表。
            base_url: 目录页 URL。
            force: 是否忽略已有文件重抓。

        Returns:
            与输入顺序一致的结果列表。
        """
        results: List[Optional[ChapterResult]] = [None] * len(chapters)
        if self.workers == 1:
            for idx, chapter in enumerate(chapters, 1):
                results[idx - 1] = self.grab_one(chapter, idx, base_url, force)
                self._report(idx, len(chapters), results[idx - 1])
            return [r for r in results if r]

        with futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            tasks = {
                pool.submit(self.grab_one, chapter, idx, base_url, force): idx
                for idx, chapter in enumerate(chapters, 1)
            }
            done = 0
            for task in futures.as_completed(tasks):
                idx = tasks[task]
                results[idx - 1] = task.result()
                done += 1
                if done % 20 == 0 or done == len(chapters):
                    print("  进度 %d/%d" % (done, len(chapters)), flush=True)
        return [r for r in results if r]

    @staticmethod
    def _report(idx: int, total: int, result: ChapterResult) -> None:
        """单线程模式下的进度输出。"""
        if idx % 20 == 0 or idx == total:
            state = "跳过" if result.skipped else ("OK" if result.ok else "FAIL")
            print("  [%d/%d] %s %s %s" % (
                idx, total, state, result.chapter.title[:24],
                ("" if result.ok else "→ " + result.error),
            ), flush=True)

    # ---------- 输出层 ----------

    def write_index(self, chapters: Sequence[Chapter]) -> str:
        """把章节索引写成 CSV。

        Args:
            chapters: 章节列表。

        Returns:
            CSV 文件路径。
        """
        import csv

        path = os.path.join(self.out_dir, "chapters_index.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["序号", "章节ID", "标题", "URL"])
            for idx, chapter in enumerate(chapters, 1):
                writer.writerow([idx, chapter.cid, chapter.title, chapter.url])
        return path

    def merge(self, chapters: Sequence[Chapter], book_name: str) -> str:
        """把已抓取的章节合并成单个 TXT。

        Args:
            chapters: 章节列表（决定合并顺序）。
            book_name: 书名，用于标题与文件名。

        Returns:
            合并后的 TXT 路径。
        """
        path = os.path.join(self.out_dir, "%s.txt" % book_name)
        starts = self.detect_volumes(chapters)
        start_set = set(starts)
        written = 0
        with open(path, "w", encoding="utf-8") as out:
            out.write("%s\n%s\n\n" % (book_name, "=" * 60))
            for idx, chapter in enumerate(chapters, 1):
                src = self.chapter_path(chapter, idx)
                if not os.path.isfile(src):
                    continue
                # 分卷处插入卷标题，便于按卷跳转阅读
                if (idx - 1) in start_set and len(starts) > 1:
                    out.write("\n\n%s\n第 %d 卷\n%s\n\n" % (
                        "#" * 60, starts.index(idx - 1) + 1, "#" * 60))
                with open(src, encoding="utf-8") as handle:
                    body = handle.read()
                out.write(body.rstrip() + "\n\n")
                written += 1
            out.write("=" * 60 + "\n共 %d 章 / %d 卷\n" % (written, len(starts)))
        return path


def probe(grabber: NovelGrabber, chapters: Sequence[Chapter], base_url: str, count: int) -> None:
    """只抽取不落盘，打印质量指标供人工判断数据量是否可信。

    Args:
        grabber: 抓取器实例。
        chapters: 章节列表。
        base_url: 目录页 URL。
        count: 抽查章数。
    """
    sample = list(chapters[:count])
    print("\n=== 探测模式（不写入正文）===")
    for idx, chapter in enumerate(sample, 1):
        try:
            html = grabber.fetch(chapter.url, referer=base_url)
            title, body = grabber.parse_chapter(html)
            paras = [p for p in body.splitlines() if p.strip()]
            print("  %-4s %-26s 标题=%-22s 字数=%-6d 段落=%d" % (
                idx, chapter.cid, (title or "(无)")[:22], len(body), len(paras)))
        except Exception as exc:  # noqa: BLE001
            print("  %-4s %-26s 失败：%s" % (idx, chapter.cid, str(exc)[:80]))
        time.sleep(grabber.delay)
    print("============================\n")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="novel_grab",
        description="长篇小说正文抓取管线（通用站点适配器）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--catalog-url", default="http://www.uuwx.la/ls/28_28215/",
                        help="目录页 URL（默认：UU小说网《蛊真人》目录页）")
    parser.add_argument("--book-name", default="蛊真人", help="书名，用于合并文件命名")
    parser.add_argument("--out-dir", default="output_novel", help="产出目录")
    parser.add_argument("--limit", type=int, default=0, help="只抓前 N 章（0 表示不限）")
    parser.add_argument("--all", action="store_true", help="抓取全部章节（默认只抓前 20 章）")
    parser.add_argument("--probe", type=int, default=0, metavar="N",
                        help="只探测前 N 章的质量，不写入正文")
    parser.add_argument("--workers", type=int, default=1, help="并发线程数（默认 1）")
    parser.add_argument("--delay", type=float, default=0.4, help="每请求间隔秒（默认 0.4）")
    parser.add_argument("--force", action="store_true", help="忽略已有文件重新抓取")
    parser.add_argument("--merge", action="store_true", help="抓取完成后合并成单个 TXT")
    parser.add_argument("--only-numbered", action="store_true",
                        help="丢弃无序号章节（如「上架感言」「今天无更」等单章）")
    parser.add_argument("--content-selector", default="div#content", help="正文容器 CSS 选择器")
    parser.add_argument("--title-selector", default="h1", help="章节标题 CSS 选择器")
    parser.add_argument("--noise", nargs="*", default=[], help="追加的水印过滤正则")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表，默认取 ``sys.argv[1:]``。

    Returns:
        进程退出码。
    """
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    grabber = NovelGrabber(
        out_dir=args.out_dir,
        delay=args.delay,
        workers=args.workers,
        noise=tuple(DEFAULT_NOISE) + tuple(args.noise),
        content_selector=args.content_selector,
        title_selector=args.title_selector,
    )

    print("[1/3] 解析目录：%s" % args.catalog_url)
    catalog_html = grabber.fetch(args.catalog_url)
    chapters = grabber.parse_catalog(catalog_html, args.catalog_url)
    if args.only_numbered:
        dropped = [c for c in chapters if not c.ordinal]
        chapters = [c for c in chapters if c.ordinal]
        print("  已丢弃无序号章节 %d 条（感言/请假条等）" % len(dropped))
    index_path = grabber.write_index(chapters)
    print("  章节数：%d → %s" % (len(chapters), index_path))
    if not chapters:
        print("目录解析为空，请核对 --catalog-url 或改指定 --content-selector")
        return 1

    if args.probe:
        probe(grabber, chapters, args.catalog_url, args.probe)
        return 0

    if args.limit:
        target = chapters[:args.limit]
    elif args.all:
        target = chapters
    else:
        target = chapters[:20]
        print("  未指定 --all/--limit，默认抓取前 20 章")

    print("\n[2/3] 抓取正文：%d 章，workers=%d，delay=%.2fs" % (len(target), args.workers, args.delay))
    results = grabber.grab_all(target, args.catalog_url, force=args.force)
    ok = [r for r in results if r.ok]
    fresh = [r for r in ok if not r.skipped]
    failed = [r for r in results if not r.ok]

    print("\n[3/3] 汇总")
    print("  成功 %d（新抓 %d / 跳过 %d），失败 %d" % (len(ok), len(fresh), len(ok) - len(fresh), len(failed)))
    if failed:
        print("  失败样例：")
        for item in failed[:5]:
            print("    %s → %s" % (item.chapter.title[:30], item.error))
    total_chars = sum(r.chars for r in ok)
    print("  正文字数合计：%s" % format(total_chars, ","))

    with open(os.path.join(args.out_dir, "run_report.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "catalog_url": args.catalog_url,
            "total_chapters": len(chapters),
            "target_chapters": len(target),
            "ok": len(ok),
            "fresh": len(fresh),
            "failed": len(failed),
            "chars": total_chars,
        }, handle, ensure_ascii=False, indent=2)

    if args.merge:
        merged = grabber.merge(chapters, args.book_name)
        print("  合并文件：%s" % merged)
    print("  产出目录：%s" % os.path.abspath(args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
