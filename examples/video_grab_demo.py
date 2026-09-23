#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Gecko 视频抓取教学脚本
=====================

用 Gecko 完成一整套「找到视频源 → 把视频取回来」的流程，分三步：

    第一步  扫描     调 Gecko 爬页面，把里面藏着的视频源地址挑出来（media.csv）
    第二步  挑选     按类型（HLS / 直链 / DASH / 音频）过滤、排序、限量
    第三步  下载     交给 Gecko 的 media_download 模块流式落盘

为什么分三步而不是一把梭：
    「发现」和「下载」是两件风险完全不同的事。发现只是读页面，下载才是复制内容。
    分开之后，你可以先看清楚扫出来的到底是什么、有多少、有多大，再决定要不要下。
    Gecko 本身的命令行也是这个设计：`--media-scan` 只发现，`--media-download` 才下载。

------------------------------------------------------------------
用途声明（请务必读完）
------------------------------------------------------------------
本脚本是**教学示例**，用来演示 Gecko 的媒体发现与下载能力是怎么串起来的。

示例地址用的是第二次实战的目标站（樱花动漫门户）。该站上的番剧是受版权保护的
作品，**未经授权下载整部剧集是侵权行为**。因此：

  * 本脚本**默认只扫描不下载**（`--download` 不显式给出就不会落任何视频文件）；
  * 真要下载，前提是你对该内容**拥有合法权利或已取得授权**；
  * 同类站群常捆绑恶意软件，别下载站内的 exe / apk / 所谓「播放器」。

教学资源是这套**流程与代码**，不是站点上的内容。

------------------------------------------------------------------
用法
------------------------------------------------------------------
    # 1. 只扫描，看看能发现什么（默认行为，不下载任何文件）
    python examples/video_grab_demo.py

    # 2. 扫另一个站
    python examples/video_grab_demo.py --url https://example.com/ --depth 2

    # 3. 确认无误后，真的下载前 3 个源，单个不超过 200MB
    python examples/video_grab_demo.py --download --limit 3 --max-mb 200

    # 4. 只要 HLS 流（m3u8），跳过直链
    python examples/video_grab_demo.py --download --kind hls

------------------------------------------------------------------
它会产出什么
------------------------------------------------------------------
    <输出目录>/media.csv     发现的视频源清单（类型 / 地址 / 来源页面 / 命中次数）
    <输出目录>/media.json    同上，JSON 格式
    <输出目录>/media/        下载回来的视频文件（仅 --download 时）
"""

import argparse
import csv
import os
import subprocess
import sys

# ---------------------------------------------------------------- 路径自检

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 项目根
CORE = os.path.join(ROOT, "gecko", "core")         # 引擎目录
RANGE_CRAWLER = os.path.join(CORE, "range_crawler.py")

# 引擎模块是平铺 import 的，得把 gecko/core 塞进 sys.path 才能 import media_download
if CORE not in sys.path:
    sys.path.insert(0, CORE)

# 优先用项目自己的虚拟环境（里面装了 curl_cffi，能伪装 TLS 指纹）
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable

# 第二次实战的目标站
DEFAULT_URL = "https://www.yhdmtv.cc/"
DEFAULT_OUT = os.path.join(ROOT, "output_video_demo")

# 四种源类型，文件大小与处理方式各不相同
KIND_DESC = {
    "hls":    "HLS 切片流（.m3u8）——视频站最常见，需拉分片再合并，要 ffmpeg",
    "dash":   "DASH 流（.mpd）——自适应码率，同样需要专门处理",
    "file":   "直链文件（.mp4/.webm/.flv）——最简单，Range 续传直接下",
    "audio":  "音频（.mp3/.m4a 等）",
    "segment": "裸分片（.ts/.m4s）——通常是 HLS/DASH 的零件，单独下没意义",
}


def run(cmd, tag):
    """跑一条子命令，输出实时打出来（不用 capture，方便看进度）。"""
    print("\n[%s] $ %s\n" % (tag, " ".join(cmd)))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print("[%s] 命令退出码 %d，后续步骤可能没有数据" % (tag, r.returncode))
    return r.returncode


def step1_scan(url, depth, max_pages, outdir):
    """第一步：用 Gecko 爬页面，发现视频源。

    --media-scan 会识别四种常见写法：<video src>、<source src>（多码率回退）、
    <audio>/<embed>、以及内联 JS 里硬编码的地址（含 https:\\/\\/ 这种转义写法）。
    """
    cmd = [
        PYTHON, RANGE_CRAWLER,
        "-u", url,
        "-d", str(depth),
        "--max-pages", str(max_pages),
        "--threads", "6",
        "--delay", "0.3",
        "--per-domain", "3",
        "--media-scan",                 # 关键：开启媒体源发现
        "--extract",
        "--rotate-ua",                  # UA 轮换
        "--referer-auto",               # 自动带站内 Referer
        "--no-verify",                  # 部分环境（如代理后）证书校验会失败，正常网络可去掉
        "-o", outdir,
    ]
    return run(cmd, "第一步 扫描")


def step2_pick(outdir, kind, limit):
    """第二步：读 media.csv，按类型过滤、按命中次数排序、限量。

    media.csv 列：类型, 媒体地址, 来源页面, 发现方式, 命中次数
    同一地址可能被多个页面、多种写法命中，Gecko 已经合并成一行并记录命中次数，
    所以这里读到的每一行都是唯一源，不会重复下载。
    """
    path = os.path.join(outdir, "media.csv")
    if not os.path.exists(path):
        print("[第二步] 没有 %s —— 这个站可能确实没有可发现的视频源" % path)
        return []

    # utf-8-sig：Gecko 写 CSV 带 BOM，Excel 打开中文才不乱码
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    if kind:
        rows = [r for r in rows if r["类型"] in kind]

    # 命中次数多的排前面：通常意味着这个源被更多页面引用，更可能是正片而不是装饰
    rows.sort(key=lambda r: -int(r["命中次数"] or 1))
    if limit:
        rows = rows[:limit]

    print("\n[第二步] 共挑出 %d 个源：" % len(rows))
    for i, r in enumerate(rows, 1):
        print("  %2d. [%-7s] 命中%s次  %s"
              % (i, r["类型"], r["命中次数"], r["媒体地址"][:88]))
    return rows


def step3_download(rows, outdir, max_mb):
    """第三步：交给 Gecko 的 media_download 模块流式落盘。

    为什么不自己写 requests.get + write？
      1) 大文件必须流式写盘，否则 1GB 视频会先把内存吃光；
      2) 中断要能续传，且服务端不支持 Range 时绝不能往残片后面追加（那会得到坏文件）；
      3) 写完要校验哈希。这些 media_download 都做好了，直接用。
    """
    try:
        import media_download
    except Exception as exc:
        print("[第三步] media_download 不可用：%s" % exc)
        return 0

    dst = os.path.join(outdir, "media")
    os.makedirs(dst, exist_ok=True)

    # HLS 合并需要 ffmpeg，提前检查，免得下完一堆分片才报错
    need_hls = any(r["类型"] == "hls" for r in rows)
    if need_hls and not media_download._find_ffmpeg():
        print("[第三步] 警告：没找到 ffmpeg，HLS 只能下载分片、无法合并成 mp4。")
        print("         装法：winget install ffmpeg  或  pip install imageio-ffmpeg")

    ok_n = 0
    for i, r in enumerate(rows, 1):
        url = r["媒体地址"]
        print("\n[第三步] (%d/%d) %s" % (i, len(rows), url[:100]))
        res = media_download.fetch(
            url, dst,
            max_mb=max_mb,
            verify=False,          # 与扫描保持一致；正常网络改成 True
            impersonate="chrome",  # 用 chrome 的 TLS/HTTP2 指纹，减少被拦
            timeout=60,
            retries=3,
        )
        if res["ok"]:
            tag = "已存在，跳过" if res.get("skipped") else ("续传完成" if res.get("resumed") else "下载完成")
            print("         %s：%s  %.2f MB"
                  % (tag, os.path.basename(res["path"]), res["bytes"] / 1048576.0))
            ok_n += 1
        else:
            print("         失败：%s" % res.get("error"))

    print("\n[第三步] 成功 %d / %d，保存在 %s" % (ok_n, len(rows), dst))
    return ok_n


def main():
    ap = argparse.ArgumentParser(
        description="Gecko 视频抓取教学脚本（默认只扫描，不下载）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="目标地址（默认第二次实战的站点）")
    ap.add_argument("--depth", type=int, default=1, help="爬取深度，默认 1")
    ap.add_argument("--max-pages", type=int, default=30, help="最多爬多少页，默认 30")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--download", action="store_true",
                    help="真的下载（默认不下载，只扫描）")
    ap.add_argument("--kind", nargs="*", default=None,
                    choices=list(KIND_DESC), help="只要这些类型，如 --kind hls file")
    ap.add_argument("--limit", type=int, default=5, help="最多下几个源，默认 5")
    ap.add_argument("--max-mb", type=int, default=300, help="单个文件体积上限 MB，默认 300")
    args = ap.parse_args()

    if not os.path.exists(RANGE_CRAWLER):
        print("找不到引擎：%s\n请在 Gecko 项目内运行本脚本。" % RANGE_CRAWLER)
        return 1

    print("=" * 66)
    print(" Gecko 视频抓取教学脚本")
    print(" 目标：%s" % args.url)
    print(" 模式：%s" % ("扫描 + 下载" if args.download else "仅扫描（不下载任何文件）"))
    print("=" * 66)
    print("\n源类型说明：")
    for k, v in KIND_DESC.items():
        print("  %-8s %s" % (k, v))

    step1_scan(args.url, args.depth, args.max_pages, args.out)
    rows = step2_pick(args.out, args.kind, args.limit)

    if not rows:
        print("\n没有发现视频源。常见原因有三个：")
        print("  1) 列表页往往只有封面和链接，真正的 <video> 在详情页——加大 --depth，")
        print("     或直接拿一个详情页 URL 当种子（示例站就是这种情况）；")
        print("  2) 视频地址是播放器 JS 动态塞进去的，静态 HTML 里压根没有，")
        print("     这种要 Gecko 的渲染模式（--render）才抓得到；")
        print("  3) 这个站确实没有可发现的媒体源。")
        return 0

    if not args.download:
        print("\n这是默认的「只扫描」模式，没有下载任何文件。")
        print("确认这些源你有权下载后，加 --download 再跑一次即可：")
        print("    python examples/video_grab_demo.py --download --limit %d" % args.limit)
        return 0

    step3_download(rows, args.out, args.max_mb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
