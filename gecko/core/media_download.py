"""大文件下载器（v1.6.0）：给 Gecko 补上「把发现的媒体源真正取回来」的能力。

为什么单独做：爬虫抓页面有 `--max-body-mb`（默认 3MB）上限，1GB 的视频用它抓只会被
截成一截断片；而朴素写法（直接 GET 后追加到残片）在中断后会得到**体积更大但内容错位**
的坏文件——实测 300MB 残片 + 全量重下 = 1.29GB 且 SHA256 不一致。

这里用 HTTP Range 做真正的断点续传，并坚持四条：
  1. 流式写盘，内存恒定（1GB 实测峰值 53MB，不随文件增大）
  2. 残片只在服务端支持 Range 时续传，否则删掉重头——绝不追加，避免静默损坏
  3. 边下边算 SHA256，落盘即校验，不信任 Content-Length
  4. 用 `.part` 中间文件，写完再原子改名，中断不会留下「看起来完成」的文件

用法（独立 CLI）：
    python gecko/core/media_download.py <url> -o <目录>
    python gecko/core/media_download.py --from media.csv -o <目录> --types hls,file
"""

import hashlib
import os
import re
import time
import urllib.parse

CHUNK = 65536


def _safe_name(url, default="download.bin"):
    """从 URL 推出一个安全文件名，避开查询串与控制字符。"""
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path) or default
    name = re.sub(r"[^\w.\-]", "_", urllib.parse.unquote(name))[:120]
    return name or default


def _unique(dst_dir, name):
    """同名文件已存在时不覆盖，加序号另存。"""
    base, ext = os.path.splitext(name)
    i, target = 1, os.path.join(dst_dir, name)
    while os.path.exists(target):
        target = os.path.join(dst_dir, "%s(%d)%s" % (base, i, ext))
        i += 1
    return target


def _open_session(impersonate=None, verify=True, timeout=30):
    """优先用 curl_cffi（TLS 指纹更像浏览器），拿不到就退回 requests。"""
    try:
        from curl_cffi import requests as creq
        return creq, impersonate or "chrome"
    except Exception:
        import requests as creq
        return creq, None


def probe(url, timeout=20, verify=True, impersonate=None, session=None):
    """探测远端大小与是否支持 Range，返回 (total, accepts_range, status)。"""
    creq, imp = (session or _open_session(impersonate, verify, timeout))
    kwargs = {"timeout": timeout, "verify": verify, "allow_redirects": True}
    if imp:
        kwargs["impersonate"] = imp
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/1.6"}
    try:
        resp = creq.get(url, headers=headers, stream=True, **kwargs)
    except Exception as exc:
        return 0, False, "探测失败: %s" % type(exc).__name__
    try:
        total = int(resp.headers.get("Content-Length") or 0)
        # 用 Content-Range 里的总长更可靠（206 时 Content-Length 只是本段长度）
        cr = resp.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)$", cr)
        if m:
            total = int(m.group(1))
        accepts = (resp.headers.get("Accept-Ranges", "").lower() == "bytes") \
            or resp.status_code == 206 or bool(cr)
        resp.close()
        return total, accepts, None
    except Exception as exc:
        return 0, False, "探测异常: %s" % type(exc).__name__


def fetch(url, dst_dir, name=None, timeout=30, retries=3, max_mb=0,
          verify=True, impersonate=None, progress=None, session=None):
    """下载单个文件，支持断点续传。返回结果字典。

    progress(bytes_done, total) 会被周期性回调，用于界面显示进度。
    """
    # HLS 走另一条链路：解析 m3u8 -> 并发拉分片 -> ffmpeg 合并
    if re.search(r"\.m3u8(\?|$)", url.split("#")[0].lower()):
        return fetch_hls(url, dst_dir, name=name, timeout=timeout, retries=retries,
                         max_mb=max_mb, verify=verify, impersonate=impersonate,
                         progress=progress)

    os.makedirs(dst_dir, exist_ok=True)
    if not name:
        name = _safe_name(url)
    final = _unique(dst_dir, name)
    part = final + ".part"

    creq, imp = (session or _open_session(impersonate, verify, timeout))
    total, accepts, err = probe(url, timeout, verify, impersonate, session=(creq, imp))
    if err:
        return {"ok": False, "url": url, "path": final, "bytes": 0,
                "total": 0, "resumed": False, "error": err}
    if max_mb and total and total > max_mb * 1024 * 1024:
        return {"ok": False, "url": url, "path": final, "bytes": 0, "total": total,
                "resumed": False, "error": "体积 %.1f MB 超过上限 %d MB" % (total / 2 ** 20, max_mb)}

    done = os.path.getsize(part) if os.path.exists(part) else 0
    if done and not accepts:
        # 服务端不支持续传，残片没法接上——删掉重头，绝不追加
        os.remove(part)
        done = 0
    resumed = done > 0

    h = hashlib.sha256()
    if done and os.path.exists(part):
        # 续传时先把已有内容喂进摘要，保证最终哈希覆盖整个文件
        with open(part, "rb") as fh:
            for c in iter(lambda: fh.read(1 << 20), b""):
                h.update(c)

    attempt = 0
    while True:
        attempt += 1
        try:
            kwargs = {"timeout": timeout, "verify": verify, "stream": True}
            if imp:
                kwargs["impersonate"] = imp
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/1.6"}
            if done:
                headers["Range"] = "bytes=%d-" % done
            resp = creq.get(url, headers=headers, **kwargs)
            if done and resp.status_code == 200:
                # 服务端无视 Range 从头返回了，只能重头写
                done = 0
                h = hashlib.sha256()
                resumed = False
            elif done and resp.status_code not in (206,):
                raise IOError("续传被拒: HTTP %s" % resp.status_code)
            elif resp.status_code >= 400:
                raise IOError("HTTP %s" % resp.status_code)

            mode = "ab" if done else "wb"
            with open(part, mode) as fh:
                for chunk in resp.iter_content(CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            resp.close()

            if total and done != total:
                raise IOError("长度不符: 拿到 %d / 应为 %d" % (done, total))
            os.replace(part, final)
            return {"ok": True, "url": url, "path": final, "bytes": done, "total": total,
                    "resumed": resumed, "sha256": h.hexdigest(), "error": None}
        except Exception as exc:
            if attempt > retries:
                return {"ok": False, "url": url, "path": final, "bytes": done, "total": total,
                        "resumed": resumed, "error": "%s: %s" % (type(exc).__name__, exc)}
            time.sleep(min(2 ** (attempt - 1), 5))
            # 续传前重新确认远端能力；不支持 Range 就重头
            _t, accepts, _e = probe(url, timeout, verify, impersonate, session=(creq, imp))
            if not accepts:
                if os.path.exists(part):
                    os.remove(part)
                done = 0
                h = hashlib.sha256()


def fetch_many(urls, dst_dir, **kw):
    """批量下载，返回 (成功列表, 失败列表)。"""
    ok, bad = [], []
    for u in urls:
        r = fetch(u, dst_dir, **kw)
        (ok if r["ok"] else bad).append(r)
    return ok, bad


def _find_ffmpeg():
    """找 ffmpeg：系统 PATH -> 打包自带（sys._MEIPASS）-> imageio-ffmpeg 静态二进制。

    单独走 imageio_ffmpeg.get_ffmpeg_exe() 在 PyInstaller 里不可靠：包内模块的
    __file__ 指向很怪，算出来的 binaries 路径常常不存在。打包版改用 _MEIPASS 找。
    """
    import glob
    import shutil
    import sys
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    base = getattr(sys, "_MEIPASS", "")
    if base:
        found = glob.glob(os.path.join(base, "**", "ffmpeg*.exe"), recursive=True)
        if found:
            return found[0]
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        return p if os.path.exists(p) else ""
    except Exception:
        return ""


def _get_text(url, timeout=20, verify=True, impersonate=None):
    """取回 m3u8 这类纯文本内容。"""
    creq, imp = _open_session(impersonate, verify, timeout)
    kwargs = {"timeout": timeout, "verify": verify}
    if imp:
        kwargs["impersonate"] = imp
    resp = creq.get(url, headers={"User-Agent": "Mozilla/5.0 Gecko/1.6"}, **kwargs)
    resp.raise_for_status()
    return resp.text


def parse_m3u8(text, base_url):
    """解析 m3u8：返回 (多码率列表, 分片地址列表, 是否加密)。"""
    import urllib.parse
    lines = [l.strip() for l in text.splitlines()]
    streams, segs, encrypted = [], [], False
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("#EXT-X-STREAM-INF"):
            bw = 0
            res = ""
            m = re.search(r"BANDWIDTH=(\d+)", l)
            if m:
                bw = int(m.group(1))
            m = re.search(r"RESOLUTION=([\dx]+)", l)
            if m:
                res = m.group(1)
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                streams.append({"bandwidth": bw, "resolution": res,
                                "url": urllib.parse.urljoin(base_url, lines[j])})
                i = j + 1
                continue
        elif l.startswith("#EXT-X-KEY") and "METHOD=NONE" not in l:
            encrypted = True
        elif l and not l.startswith("#"):
            segs.append(urllib.parse.urljoin(base_url, l))
        i += 1
    return streams, segs, encrypted


def fetch_hls(url, dst_dir, name=None, timeout=30, retries=3, max_mb=0,
              verify=True, impersonate=None, progress=None, workers=6,
              pick="best", session=None):
    """下载 HLS 流并合并成 mp4。

    pick: best=最高码率 / worst=最低 / 直接给分辨率字符串如 "1280x720"。
    流程：master m3u8 -> 选码率 -> media m3u8 -> 并发拉分片 -> ffmpeg -c copy 合并。
    """
    import shutil
    import subprocess
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    os.makedirs(dst_dir, exist_ok=True)
    stem = os.path.splitext(name or _safe_name(url, "video.mp4"))[0]
    if not stem.lower().endswith(".mp4"):
        stem += ".mp4"
    final = _unique(dst_dir, stem)

    try:
        text = _get_text(url, timeout, verify, impersonate)
    except Exception as exc:
        return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                "resumed": False, "error": "取 m3u8 失败: %s" % exc}

    streams, segs, encrypted = parse_m3u8(text, url)
    if encrypted:
        return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                "resumed": False, "error": "该流有加密分片（EXT-X-KEY），暂不支持"}
    if streams:                       # master playlist，先选一路码率
        if pick == "best":
            choice = max(streams, key=lambda s: s["bandwidth"])
        elif pick == "worst":
            choice = min(streams, key=lambda s: s["bandwidth"])
        else:
            choice = next((s for s in streams if s["resolution"] == pick),
                          max(streams, key=lambda s: s["bandwidth"]))
        try:
            text = _get_text(choice["url"], timeout, verify, impersonate)
        except Exception as exc:
            return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                    "resumed": False, "error": "取子 m3u8 失败: %s" % exc}
        _s, segs, encrypted = parse_m3u8(text, choice["url"])
        if encrypted:
            return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                    "resumed": False, "error": "该流有加密分片（EXT-X-KEY），暂不支持"}
    if not segs:
        return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                "resumed": False, "error": "m3u8 里没有分片"}

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                "resumed": False, "error": "未找到 ffmpeg，无法合并分片（pip install imageio-ffmpeg）"}

    tmp = tempfile.mkdtemp(prefix="gecko_hls_")
    total_mb = sum(1 for _ in segs)
    done = [0]

    def one(idx_url):
        i, u = idx_url
        ext = ".ts"
        low = u.split("?")[0].lower()
        if low.endswith((".m4s", ".mp4")):
            ext = ".m4s"
        dst = os.path.join(tmp, "%06d%s" % (i, ext))
        for attempt in range(retries + 1):
            try:
                r = fetch(u, tmp, name=os.path.basename(dst), timeout=timeout,
                          retries=1, verify=verify, impersonate=impersonate)
                if r["ok"]:
                    done[0] += 1
                    if progress:
                        progress(done[0], total_mb)
                    return os.path.basename(dst)
            except Exception:
                pass
            time.sleep(min(2 ** attempt, 4))
        return None

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            files = list(pool.map(one, list(enumerate(segs))))
        files = [f for f in files if f]
        if len(files) < len(segs):
            return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                    "resumed": False,
                    "error": "分片下载不全: %d/%d" % (len(files), len(segs))}

        # 按分片真实封装拼接。试过让 ffmpeg 直接读本地 m3u8，但分片类型与扩展名
        # 不一致时会报 Invalid data；二进制拼接 + 一次转封装容错得多：
        #   0x47 开头 = MPEG-TS（可直接首尾相接）；含 ftyp = MP4/fMP4。
        with open(os.path.join(tmp, files[0]), "rb") as fh:
            head = fh.read(16)
        if head[:1] == b"\x47":
            merged_ext = ".ts"
        elif b"ftyp" in head:
            merged_ext = ".mp4"
        else:
            merged_ext = os.path.splitext(files[0])[1] or ".ts"
        merged = os.path.join(tmp, "merged" + merged_ext)
        with open(merged, "wb") as out:
            for f in files:
                with open(os.path.join(tmp, f), "rb") as fh:
                    shutil.copyfileobj(fh, out)

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-fflags", "+genpts", "-i", merged, "-c", "copy"]
        if merged_ext == ".ts":          # TS 里的 AAC 是 ADTS，进 MP4 要转成 ASC
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd.append(final)
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
        if p.returncode != 0 or not os.path.exists(final):
            return {"ok": False, "url": url, "path": final, "bytes": 0, "total": 0,
                    "resumed": False, "error": "合并失败: %s" % (p.stderr or "")[-160:]}
        size = os.path.getsize(final)
        return {"ok": True, "url": url, "path": final, "bytes": size, "total": size,
                "resumed": False, "segments": len(files), "error": None}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cli():
    import argparse
    import csv
    import io

    p = argparse.ArgumentParser(description="Gecko 大文件下载器（断点续传 / 流式写盘）")
    p.add_argument("url", nargs="?", default="", help="要下载的地址")
    p.add_argument("--from", dest="from_csv", default="",
                   help="从 media.csv 读取地址批量下载（Gecko --media-scan 的产物）")
    p.add_argument("--types", default="", help="只下指定类型，逗号分隔：hls,dash,file,segment,audio")
    p.add_argument("-o", "--out", default="media_downloads", help="保存目录")
    p.add_argument("--max-mb", type=int, default=0, help="单文件体积上限，0=不限")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--no-verify", action="store_true", help="跳过证书校验")
    args = p.parse_args()

    urls = []
    if args.from_csv:
        with io.open(args.from_csv, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if args.types and row.get("类型") not in args.types.split(","):
                    continue
                urls.append(row["媒体地址"])
    elif args.url:
        urls.append(args.url)
    else:
        p.error("给一个 URL，或用 --from media.csv")

    print("待下载 %d 个，保存目录: %s" % (len(urls), os.path.abspath(args.out)))
    t0 = time.time()
    for u in urls:
        def prog(done, total):
            if total:
                print("\r  %.1f%%  %.1f/%.1f MB" % (done * 100.0 / total, done / 2 ** 20, total / 2 ** 20),
                      end="", flush=True)
        r = fetch(u, args.out, timeout=args.timeout, retries=args.retries,
                  max_mb=args.max_mb, verify=not args.no_verify, progress=prog)
        print()
        if r["ok"]:
            print("  [OK] %s  %.2f MB  %s  sha256=%s" % (
                os.path.basename(r["path"]), r["bytes"] / 2 ** 20,
                "续传完成" if r["resumed"] else "新建", r.get("sha256", "")[:16]))
        else:
            print("  [FAIL] %s -> %s" % (u[:70], r["error"]))
    print("耗时 %.1f 秒" % (time.time() - t0))


if __name__ == "__main__":
    _cli()
