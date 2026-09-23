#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anticrawl.py —— 靶场反爬对抗模块（仅用于你拥有或已获书面授权的目标）

子模块：
  1. font    字体反爬：提取 @font-face 字体 → 重建「字形→真实字符」映射 → 还原正文
  2. decode  文本混淆解码：HTML实体 / Unicode转义 / URL编码 / Base64 / 全角同形字归一化
  3. water   隐形水印：LSB / 频域分析与清理（靶场验证用）
  4. auth    鉴权（访问控制）测试：匿名 / 登录 / 参数篡改 差分请求，发现越权点
  5. js      JS接口加密：加密特征扫描、硬编码密钥提取、Node 执行解密、请求捕获重放
  6. waf     WAF 检测：指纹识别 / 拦截特征 / UA差异分析（不含绕过 payload）
  7. challenge JS挑战：检测 → 提取挑战脚本 → Node 执行取令牌 → 带令牌重放
  8. captcha 验证码：检测与类型分类 + 人工接力指引（不提供自动识别）

统一入口：
  python anticrawl.py -m font    --url http://靶场/防爬页 [--text "待解码文本"] [--font-file x.ttf]
  python anticrawl.py -m decode  --text "&#49;&#50;&#51;"
  python anticrawl.py -m water   --file img.png --action detect|clean [--method auto]
  python anticrawl.py -m auth    --url http://靶场/付费页 [--cookie "session=xx"]
  python anticrawl.py -m js      --url http://靶场/ --action scan|run|replay
  python anticrawl.py -m waf     --url http://靶场/ [--cookie "session=xx"]
  python anticrawl.py -m challenge --url http://靶场/挑战页 [--cookie "challenge_ok=xx"]
  python anticrawl.py -m captcha --url http://靶场/登录页
"""

import argparse
import base64
import hashlib
import html as html_mod
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urljoin, urlparse, unquote

import requests

# HTTP 后端：优先 curl_cffi（真实浏览器 TLS/HTTP2 指纹），缺失时退回 requests。
# WAF 常按 TLS 指纹区分爬虫与浏览器，所以这里与爬虫引擎共用同一套后端。
try:
    import http_backend
except Exception:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import http_backend
    except Exception:  # pragma: no cover
        http_backend = None


def _new_session(cookie="", verify=True, ua=None, headers=None, impersonate=None):
    """创建 requests 兼容的 session（带浏览器指纹，用于越过 TLS/HTTP2 指纹检测）。"""
    h = {"User-Agent": ua or DEFAULT_UA}
    if cookie:
        h["Cookie"] = cookie
    for k, v in (headers or {}).items():
        h[str(k)] = str(v)
    if http_backend is not None:
        session, _name = http_backend.create_session(
            backend="auto", impersonate=impersonate, verify=verify, headers=h)
        return session
    s = requests.Session()
    s.verify = verify
    s.headers.update(h)
    return s


def _http_get(url, headers=None, timeout=15, verify=True, allow_redirects=True, ua=None):
    """一次性 GET：走与引擎一致的后端；失败返回 None，由调用方按不可达处理。"""
    try:
        return _new_session(verify=verify, headers=headers, ua=ua).get(
            url, timeout=timeout, allow_redirects=allow_redirects)
    except Exception:
        return None

try:
    import numpy as np
except ImportError:
    np = None
try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:
    Image = None
try:
    from fontTools.ttLib import TTFont
except ImportError:
    TTFont = None

DEFAULT_UA = "RangeCrawler/1.0 (+authorized-testing-only)"
PUA_START, PUA_END = 0xE000, 0xF8FF
REF_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


# —— 编码探测 ——
# requests 对未声明 charset 的 text/* 会按 latin-1 解码，中文站点会整页乱码；
# 各模块统一走 response_text()，不要直接用 resp.text。
META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I)


def decode_body(body: bytes, headers) -> str:
    """按 响应头 charset → HTML meta charset → utf-8 → gb18030 的顺序探测解码。"""
    ct = (headers.get("Content-Type") or "") if headers else ""
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    enc = m.group(1).lower() if m else ""
    if not enc:
        m = META_CHARSET_RE.search(body[:4096])
        if m:
            enc = m.group(1).decode("ascii", "replace").lower()
    for cand in ([enc] if enc else []) + ["utf-8", "gb18030"]:
        try:
            return body.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def response_text(resp) -> str:
    return decode_body(resp.content, resp.headers)


def _require(cond, msg):
    if not cond:
        raise RuntimeError(msg)


# ===========================================================================
# 1. 字体反爬
# ===========================================================================


class FontAntiCrawl:
    """提取站点字体，重建字形→字符映射，解码混淆文本。"""

    def __init__(self, cookie="", verify=True, timeout=15):
        self.session = _new_session(cookie=cookie, verify=verify)
        self.verify = verify
        self.timeout = timeout
        self.mapping = {}   # codepoint(int) -> char
        self.font_source = None

    # ---- 获取 ----

    def fetch_font_from_page(self, page_url):
        """从页面内联 <style> 与外部 CSS 提取 @font-face 字体（支持 base64 内联）。"""
        _require(TTFont is not None, "缺少 fontTools，请执行 pip install fonttools")
        html_text = self._get_text(page_url)
        candidates = []
        for block in re.finditer(r"@font-face\s*\{([^}]*)\}", html_text, re.S):
            self._collect_src(block.group(1), page_url, candidates)
        for link in re.finditer(r'<link[^>]+rel=["\']stylesheet["\'][^>]*>', html_text, re.I):
            m = re.search(r'href=["\']([^"\']+)["\']', link.group(0))
            if not m:
                continue
            css = self._get_text(urljoin(page_url, m.group(1)))
            for block in re.finditer(r"@font-face\s*\{([^}]*)\}", css, re.S):
                self._collect_src(block.group(1), page_url, candidates)
        if not candidates:
            return None
        # 按出现顺序尝试（woff2/woff/ttf 均可由 fontTools 打开）
        for src in candidates:
            try:
                data = self._fetch_bytes(src)
                if data:
                    self.font_source = src[1] if src[0] == "__url__" else "data:base64(内联字体)"
                    return data
            except Exception:
                continue
        return None

    def _collect_src(self, block, base, out):
        m = re.search(r"src:\s*([^;}]+)", block, re.S)
        if not m:
            return
        for _, u in re.findall(r"url\((['\"]?)(.*?)\1\)", m.group(1)):
            u = u.strip()
            if u.startswith("data:"):
                try:
                    b64 = u.split(",", 1)[1]
                    out.append(("__b64__", b64))
                except IndexError:
                    pass
            else:
                # 相对路径（含不带前导 / 的）与绝对 URL 统一转绝对
                out.append(("__url__", urljoin(base, u)))

    def _fetch_bytes(self, src):
        kind, val = src
        if kind == "__b64__":
            return base64.b64decode(val)
        return self.session.get(val, timeout=self.timeout, verify=self.verify).content

    def _get_text(self, url):
        r = self.session.get(url, timeout=self.timeout, verify=self.verify)
        return response_text(r)

    # ---- 字形比对 ----

    @staticmethod
    def _render_char(pil_font, ch, size):
        """渲染单个字符为归一化灰度数组。"""
        img = Image.new("L", (size * 2, size * 2), 255)
        d = ImageDraw.Draw(img)
        try:
            bbox = d.textbbox((0, 0), ch, font=pil_font)
        except TypeError:
            bbox = pil_font.getbbox(ch)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= 0 or h <= 0:
            return None
        x = (size * 2 - w) / 2 - bbox[0]
        y = (size * 2 - h) / 2 - bbox[1]
        d.text((x, y), ch, font=pil_font, fill=0)
        img = img.crop((size - size // 2, size - size // 2, size + size // 2, size + size // 2))
        img = img.resize((size, size), Image.LANCZOS)
        return np.asarray(img, dtype=np.uint8)

    @staticmethod
    def _similarity(a, b):
        if a is None or b is None or a.shape != b.shape:
            return 0.0
        ab = (a > 128).astype(np.uint8)
        bb = (b > 128).astype(np.uint8)
        inter = int(np.logical_and(ab, bb).sum())
        union = int(np.logical_or(ab, bb).sum())
        return inter / max(union, 1)

    def build_mapping(self, font_bytes, ref_font_path=None, size=64, threshold=0.62):
        """重建映射：PUA 字形 vs 参考字体字形逐对比对，取相似度最高的字符。"""
        _require(np is not None, "缺少 numpy")
        _require(Image is not None, "缺少 pillow")
        ref = self._reference_glyphs(size, ref_font_path)
        if not ref:
            raise RuntimeError("找不到参考字体（尝试 --ref-font 指定，如 arial.ttf）")

        font = TTFont(io.BytesIO(font_bytes))
        cmap = {}
        for table in font["cmap"].tables:
            cmap.update(table.cmap)

        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as tf:
            tmp_name = tf.name
            font.save(tmp_name)  # with 块退出后句柄关闭，避免 Windows 文件锁
        try:
            pil_font = ImageFont.truetype(tmp_name, size)
            mapping = {}
            for cp, _glyph in sorted(cmap.items()):
                if not (PUA_START <= cp <= PUA_END):
                    continue
                try:
                    bmp = self._render_char(pil_font, chr(cp), size)
                except Exception:
                    continue
                best_char, best_score = None, 0.0
                for ch, rmp in ref.items():
                    s = self._similarity(bmp, rmp)
                    if s > best_score:
                        best_score, best_char = s, ch
                if best_char is not None and best_score >= threshold:
                    mapping[cp] = best_char
            return mapping
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _reference_glyphs(self, size, ref_font_path=None):
        candidates = [ref_font_path] if ref_font_path else []
        candidates += [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/consola.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        path = next((c for c in candidates if c and os.path.exists(c)), None)
        if not path:
            return {}
        pil_font = ImageFont.truetype(path, size)
        return {ch: self._render_char(pil_font, ch, size) for ch in REF_CHARS}

    def decode_text(self, text):
        """用重建的映射还原文本中的 PUA 字符。"""
        out = []
        for ch in text:
            cp = ord(ch)
            out.append(self.mapping[cp] if cp in self.mapping else ch)
        return "".join(out)

    def full_run(self, page_url, text="", ref_font_path=None):
        result = {"page": page_url, "mapping": {}, "font_source": None}
        data = self.fetch_font_from_page(page_url)
        if not data:
            result["error"] = "未从页面提取到 @font-face 字体"
            return result
        result["font_source"] = self.font_source
        result["font_size"] = len(data)
        self.mapping = self.build_mapping(data, ref_font_path)
        result["mapping"] = {
            f"&#x{cp:X};": ch for cp, ch in sorted(self.mapping.items())
        }
        if text:
            result["original"] = text
            # 输入可能是 HTML 实体/转义形式，先还原成真实字符再按映射解码
            decoded_in = html_mod.unescape(text)
            result["decoded"] = self.decode_text(decoded_in)
        return result


# ===========================================================================
# 2. 文本混淆解码
# ===========================================================================


class TextDecoder:
    @staticmethod
    def _unescape_unicode(s):
        def repl(m):
            try:
                return chr(int(m.group(1), 16))
            except ValueError:
                return m.group(0)
        s = re.sub(r"\\u([0-9a-fA-F]{4})", repl, s)
        s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
        return s

    @staticmethod
    def _try_b64(s):
        if len(s) < 8:
            return s
        cleaned = s.strip()
        # 标准/URL-safe base64 特征
        if not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", cleaned):
            return s
        if len(cleaned) % 4 != 0:
            return s
        try:
            dec = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))
            text = dec.decode("utf-8", errors="strict")
            if any(c.isprintable() or c in "\r\n\t" for c in text) and len(text) > 0:
                return text
        except Exception:
            pass
        return s

    @classmethod
    def decode_chain(cls, text):
        """按常见混淆顺序逐级尝试，返回每一步变化。"""
        if text is None:
            text = ""
        steps = []
        cur = text

        def step(name, new):
            nonlocal cur
            if new != cur:
                steps.append({"step": name, "result": new})
                cur = new

        step("HTML实体", html_mod.unescape(cur))
        step("Unicode转义(\\u/\\x)", cls._unescape_unicode(cur))
        step("URL编码", unquote(cur))
        step("Base64", cls._try_b64(cur))
        step("全角/同形字归一化(NFKC)", unicodedata.normalize("NFKC", cur))
        return {"original": text, "final": cur, "steps": steps}


# ===========================================================================
# 3. 隐形水印
# ===========================================================================


class WatermarkTool:
    """隐形水印检测与清理（靶场验证用，启发式，非盲水印的通用破解）。"""

    @staticmethod
    def _load_image(src):
        if isinstance(src, (bytes, bytearray)):
            return Image.open(io.BytesIO(src)).convert("RGB")
        return Image.open(src).convert("RGB")

    @staticmethod
    def _channel_entropy(plane):
        if plane.size == 0:
            return 0.0
        vals, counts = np.unique(plane, return_counts=True)
        probs = counts / counts.sum()
        return float(-(probs * np.log2(probs + 1e-12)).sum())

    @classmethod
    def detect(cls, src, name="image"):
        _require(np is not None, "缺少 numpy")
        img = cls._load_image(src)
        arr = np.asarray(img)
        h, w, _c = arr.shape
        report = {"name": name, "width": w, "height": h, "checks": {}}

        # 1) LSB 平面分析：真随机≈高熵且通道间独立；水印常导致通道相关性/低熵
        lsb = arr & 1
        ent = [cls._channel_entropy(lsb[..., k]) for k in range(3)]
        flat = [lsb[..., k].ravel() for k in range(3)]
        corr01 = float(np.corrcoef(flat[0], flat[1])[0, 1])
        corr02 = float(np.corrcoef(flat[0], flat[2])[0, 1])
        report["checks"]["lsb_entropy"] = {"r": round(ent[0], 3), "g": round(ent[1], 3), "b": round(ent[2], 3)}
        report["checks"]["lsb_channel_corr"] = {"r-g": round(corr01, 3), "r-b": round(corr02, 3)}
        lsb_flag = any(e < 0.95 for e in ent) or abs(corr01) > 0.6 or abs(corr02) > 0.6

        # 2) 频域分析：高频带出现明显周期峰 → 疑似重复水印/规律性嵌入
        gray = np.asarray(img.convert("L"), dtype=np.float64)
        f = np.fft.fft2(gray - gray.mean())
        fshift = np.fft.fftshift(f)
        mag = np.abs(fshift)
        cy, cx = h // 2, w // 2
        mag_high = mag.copy()
        mag_high[cy - h // 8:cy + h // 8, cx - w // 8:cx + w // 8] = 0  # 剔除低频区
        med = float(np.median(mag_high))
        mad = float(np.median(np.abs(mag_high - med))) + 1e-9
        outliers = int((mag_high > med + 10 * mad).sum())
        outlier_ratio = outliers / max(mag_high.size, 1)
        report["checks"]["fft_periodic_peaks"] = {"outliers": outliers,
                                                  "ratio": round(outlier_ratio, 6)}
        fft_flag = outlier_ratio > 0.01

        # 3) 块重复检测：水印常在 8x8 块中重复出现（粗略）
        bh, bw = max(8, h // 32), max(8, w // 32)
        blocks = []
        for y in range(0, h - bh + 1, bh):
            for x in range(0, w - bw + 1, bw):
                blocks.append(gray[y:y + bh, x:x + bw])
        sim_sum, cnt = 0.0, 0
        if len(blocks) > 2:
            for i in range(min(200, len(blocks) - 1)):
                a = blocks[i]
                b = blocks[i + 1]
                if a.shape == b.shape and a.std() > 1e-3 and b.std() > 1e-3:
                    sim_sum += float(np.corrcoef(a.ravel(), b.ravel())[0, 1])
                    cnt += 1
        avg_sim = sim_sum / cnt if cnt else 0.0
        report["checks"]["block_corr"] = round(avg_sim, 3)
        block_flag = avg_sim > 0.98 and cnt > 0

        flags = []
        if lsb_flag:
            flags.append("LSB层存在非随机模式（疑似水印/隐写）")
        if fft_flag:
            flags.append("频域存在明显周期峰（疑似规律性水印）")
        if block_flag:
            flags.append("分块高度相似（疑似重复水印）")
        report["verdict"] = "疑似存在隐形水印" if flags else "未发现明显水印特征"
        report["flags"] = flags
        return report

    @classmethod
    def clean(cls, src, method="auto"):
        _require(np is not None, "缺少 numpy")
        img = cls._load_image(src)
        if method in ("auto", "lsb_strip"):
            arr = np.asarray(img)
            arr = arr & 0b11111110  # 清零 LSB
            img = Image.fromarray(arr)
        if method in ("auto", "fft_lowpass"):
            arr = np.asarray(img.convert("RGB"), dtype=np.float64)
            for k in range(3):
                f = np.fft.fft2(arr[..., k] - arr[..., k].mean())
                fshift = np.fft.fftshift(f)
                h, w = fshift.shape
                # 低通：保留中心低频，清零四角高频（高频水印/噪声集中在四角）
                mask = np.zeros_like(fshift)
                mask[h // 2 - h // 16:h // 2 + h // 16, w // 2 - w // 16:w // 2 + w // 16] = 1
                fshift *= mask
                arr[..., k] = np.fft.ifft2(np.fft.ifftshift(fshift)).real + arr[..., k].mean()
            arr = np.clip(arr, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)
        if method in ("gaussian",):
            img = img.filter(ImageFilter.GaussianBlur(radius=0.8))
        if method in ("median",):
            img = img.filter(ImageFilter.MedianFilter(size=3))
        if method in ("posterize",):
            img = img.point(lambda p: (p // 32) * 32)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), method


# ===========================================================================
# 4. 鉴权（访问控制）测试
# ===========================================================================


class AuthTester:
    """以多种鉴权上下文请求目标并差分对比，定位访问控制弱点（仅限授权目标）。"""

    def __init__(self, cookie="", verify=True, timeout=15):
        self.cookie = cookie
        self.verify = verify
        self.timeout = timeout
        self.session = _new_session(cookie=cookie, verify=verify)

    def test(self, url, method="GET", body_json=None):
        host = urlparse(url).netloc
        probes = [
            ("匿名访问", {}, None),
            ("携带Cookie(登录/付费态)", {}, None),
            ("伪造VIP字段", {"vip": "1", "is_paid": "true", "role": "admin"}, None),
            ("伪造管理员字段", {"admin": "1", "is_admin": "true", "level": "9"}, None),
            ("伪造Referer来源", {}, {"Referer": f"https://admin.{host}/"}),
            ("内网来源头", {}, {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}),
        ]
        rows = []
        baseline = None
        for name, params, extra_headers in probes:
            if name == "携带Cookie(登录/付费态)" and not self.cookie:
                continue
            headers = dict(extra_headers or {})
            if self.cookie:
                headers["Cookie"] = self.cookie
            try:
                if method.upper() == "POST":
                    data = dict(params)
                    if body_json:
                        data.update(json.loads(body_json))
                    r = self.session.post(url, data=data, headers=headers,
                                          timeout=self.timeout, verify=self.verify)
                else:
                    r = self.session.get(url, params=params, headers=headers,
                                         timeout=self.timeout, verify=self.verify)
                length = len(r.content)
                _rtext = response_text(r)
                digest = hashlib.md5(r.content).hexdigest()[:8]
                row = {
                    "context": name, "status": r.status_code,
                    "length": length, "digest": digest,
                    "title": re.search(r"<title[^>]*>(.*?)</title>", _rtext[:5000], re.S | re.I)
                                .group(1).strip()[:80] if "<title" in _rtext[:5000] else "",
                }
            except requests.exceptions.RequestException as exc:
                row = {"context": name, "status": "ERR", "length": 0,
                       "digest": "", "title": str(exc)[:120], "verdict": "请求失败"}
                rows.append(row)
                continue

            if baseline is None:
                baseline = row
                row["verdict"] = "基线"
            else:
                if row["status"] == baseline["status"] and row["digest"] == baseline["digest"]:
                    row["verdict"] = "与基线相同"
                elif row["status"] == 200 and baseline["status"] in (401, 403, 302):
                    row["verdict"] = "疑似越权(匿名被拒/篡改后放行)"
                elif row["status"] != baseline["status"]:
                    row["verdict"] = "状态码变化(差异)"
                else:
                    row["verdict"] = "内容差异(需人工核对)"
            rows.append(row)
        return {"url": url, "method": method.upper(), "results": rows}


# ===========================================================================
# 5. JS 接口加密
# ===========================================================================

CRYPTO_PATTERNS = [
    (r"CryptoJS|AES|createCipheriv|createCipher", "对称加密(AES/CryptoJS)"),
    (r"JSEncrypt|RSA|setPublicKey|encryptLong|setPrivateKey", "RSA非对称加密"),
    (r"md5\(|\.MD5|createHash\(['\"]md5", "MD5/哈希"),
    (r"btoa\(|atob\(|Base64\.encode|fromBase64", "Base64"),
    (r"iv\s*[:=]\s*['\"][A-Za-z0-9+/=_\-.]{8,}['\"]", "疑似硬编码IV"),
    (r"(?:key|KEY|Key)\s*[:=]\s*['\"][A-Za-z0-9+/=_\-.]{8,}['\"]", "疑似硬编码密钥"),
    (r"sign(?:ature)?\s*[:=]", "签名参数"),
    (r"fetch\(|XMLHttpRequest|axios\.(?:get|post)|\.ajax\(", "请求封装"),
    (r"/api/[A-Za-z0-9_\-/]+", "API端点"),
    (r"window\.|document\.|location\.", "浏览器对象(可作逆向线索)"),
]


class JSAnalyzer:
    def __init__(self, cookie="", verify=True, timeout=15):
        self.session = _new_session(cookie=cookie, verify=verify)
        self.verify = verify
        self.timeout = timeout

    # ---- 扫描 ----

    def scan(self, page_url):
        html_text = self._get_text(page_url)
        scripts = []
        for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html_text, re.I):
            u = urljoin(page_url, m.group(1))
            scripts.append({"url": u, "code": self._get_text(u)})
        for m in re.finditer(r"<script[^>]*>(.*?)</script>", html_text, re.S | re.I):
            code = m.group(1).strip()
            if code:
                scripts.append({"url": page_url + " [inline]", "code": code})

        findings = []
        for sc in scripts:
            lines = sc["code"].splitlines()
            for pat, label in CRYPTO_PATTERNS:
                for i, line in enumerate(lines):
                    if re.search(pat, line):
                        ctx = " | ".join(x.strip() for x in lines[max(0, i - 1):i + 2] if x.strip())[:220]
                        findings.append({
                            "file": sc["url"], "line": i + 1, "type": label,
                            "pattern": pat, "context": ctx,
                        })
        return {"page": page_url, "script_count": len(scripts), "findings": findings}

    def extract_keys(self, page_url):
        """从 JS 中粗提取疑似硬编码密钥/IV 候选（hex/base64/普通字符串常量）。"""
        html_text = self._get_text(page_url)
        code = "\n".join(self._get_text(urljoin(page_url, m.group(1)))
                         for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html_text, re.I))
        code += "\n" + "\n".join(m.group(1) for m in re.finditer(r"<script[^>]*>(.*?)</script>", html_text, re.S))
        candidates = set()
        for pat in [r"['\"]([A-Za-z0-9+/=_\-.]{16,64})['\"]",
                    r"0x[0-9a-fA-F]{8,}",
                    r"['\"]([0-9a-f]{16,64})['\"]"]:
            for m in re.finditer(pat, code):
                candidates.add(m.group(1))
        return sorted(candidates)[:100]

    def _get_text(self, url):
        r = self.session.get(url, timeout=self.timeout, verify=self.verify)
        return response_text(r)

    # ---- Node 执行 ----

    @staticmethod
    def run_script(js_code, args=None, timeout=30):
        """用 Node 执行 JS（解密/签名函数），返回 stdout/stderr。"""
        # shutil.which 跨平台且不依赖编码；原先用 `where` + text=True，
        # 在中文 Windows 上会因 GBK 输出解码失败，把已安装的 Node 误判为不存在
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            raise RuntimeError("未找到 Node.js，无法执行 JS。可手动把脚本放到浏览器控制台运行，或安装 Node。")
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", encoding="utf-8") as tf:
            tf.write(js_code)
            tmp = tf.name
        try:
            cmd = [node, tmp] + (args or [])
            proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
            return {"exit": proc.returncode, "stdout": proc.stdout[:8000], "stderr": proc.stderr[:4000]}
        finally:
            os.unlink(tmp)

    # ---- 捕获重放 ----

    @staticmethod
    def replay(captures, cookie="", verify=True, timeout=20):
        """重放捕获的请求列表。captures: [{method,url,headers?,data?,params?}]。"""
        if isinstance(captures, str):
            captures = json.loads(captures.lstrip("\ufeff"))  # 兼容 BOM
        results = []
        s = _new_session(cookie=cookie)
        for i, cap in enumerate(captures):
            headers = dict(cap.get("headers") or {})
            method = (cap.get("method") or "GET").upper()
            url = cap.get("url")
            if not url:
                results.append({"index": i, "error": "缺少 url"})
                continue
            try:
                if method in ("POST", "PUT", "PATCH"):
                    r = s.request(method, url, data=cap.get("data"),
                                  json=cap.get("json"), params=cap.get("params"),
                                  headers=headers, timeout=timeout, verify=verify)
                else:
                    r = s.request(method, url, params=cap.get("params"),
                                  headers=headers, timeout=timeout, verify=verify)
                results.append({
                    "index": i, "method": method, "url": url,
                    "status": r.status_code, "length": len(r.content),
                    "body_head": response_text(r)[:300],
                })
            except requests.exceptions.RequestException as exc:
                results.append({"index": i, "method": method, "url": url, "error": str(exc)[:200]})
        return {"total": len(captures), "results": results}


# ===========================================================================
# 6. WAF 检测（指纹识别 + 拦截特征 + 请求差异分析，仅授权靶场）
# ===========================================================================

# 常见 WAF 指纹：[(名称, [(响应头, 特征正则), (None, 页面正文特征正则)])]
WAF_SIGNATURES = [
    ("Cloudflare", [("Server", r"cloudflare"), ("cf-ray", None), (None, r"cf_chl|challenge-platform|captcha-banner")]),
    ("ModSecurity", [("Server", r"mod[_ -]?security|modsecurity"), (None, r"mod_security|ModSecurity")]),
    ("阿里云盾WAF", [("Server", r"aliyun|Tengine"), (None, r"阿里云|Aliyun.*[Ww][Aa][Ff]|waf\.aliyun|error\.aliyun")]),
    ("腾讯云WAF", [(None, r"tencent.*waf|腾讯云.*(WAF|防火墙)")]),
    ("安全狗 Safedog", [("Server", r"safedog"), (None, r"safedog|安全狗")]),
    ("Yundun 云盾", [("Server", r"yundun"), (None, r"yundun")]),
    ("360网站卫士", [("Server", r"360wzb"), (None, r"360网站卫士|360wzb")]),
    ("网宿云WAF", [("Server", r"wangsu|WS[ /]?WAF"), (None, r"wangsu|网宿")]),
    ("OpenResty(常见WAF宿主)", [("Server", r"openresty")]),
    ("Naxsi", [("Server", r"naxsi"), (None, r"naxsi")]),
    ("F5 BIG-IP ASM", [("Server", r"BIG-IP|F5"), (None, r"F5.*ASM|Attack Signature Identified")]),
    ("Imperva / Incapsula", [("Server", r"incapsula|imperva"), (None, r"incapsula|imperva")]),
    ("AWS WAF", [("X-Amzn-RequestId", None), (None, r"awswaf|AWS WAF")]),
]

# 疑似被拦截的状态码
BLOCK_STATUS = (403, 406, 418, 429, 503)
# 拦截/验证页常见关键词
# 拦截/验证页常见关键词（中文用负向后视排除"未被/没有/不被"等否定表述）
BLOCK_KEYWORDS_RE = re.compile(
    r"blocked|blocked by|forbidden|access denied|your request|security check|"
    r"captcha|challenge|verify|"
    r"(?<!未)(?<!没)(?<!不)(?:被拦截|已拦截)|访问被拒绝|安全验证|检测到异常|恶意访问|请求被拒绝",
    re.I,
)

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class WAFDetector:
    """WAF 指纹识别与拦截检测：识别目标是否处于 WAF 之后、类型与触发迹象。"""

    def __init__(self, cookie="", verify=True, timeout=15):
        self.cookie = cookie
        self.verify = verify
        self.timeout = timeout

    def _get(self, url, ua):
        headers = {"User-Agent": ua} if ua else {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            return _http_get(url, headers=headers, timeout=self.timeout, verify=self.verify)
        except requests.exceptions.RequestException:
            return None

    @staticmethod
    def _is_blocked(r, body):
        if r.status_code in BLOCK_STATUS:
            return True
        if body and BLOCK_KEYWORDS_RE.search(body):
            return True
        return False

    def detect(self, url):
        base = self._get(url, DEFAULT_UA)
        if base is None:
            return {"error": "请求失败（连接/超时）"}
        body = response_text(base)[:20000]
        headers = {k.lower(): v for k, v in base.headers.items()}
        fingerprints = []
        for name, sigs in WAF_SIGNATURES:
            for hdr, pat in sigs:
                if hdr:
                    val = headers.get(hdr) or ""
                    if val and re.search(pat, val, re.I):
                        fingerprints.append(name)
                        break
                elif pat and body and re.search(pat, body, re.I):
                    fingerprints.append(name)
                    break
        fingerprints = list(dict.fromkeys(fingerprints))
        blocked = self._is_blocked(base, body)

        # UA 差异测试：默认爬虫UA vs 浏览器UA vs 无UA
        ua_tests = []
        for label, ua in (("默认爬虫UA", DEFAULT_UA), ("Chrome浏览器UA", CHROME_UA), ("无UA", None)):
            r = self._get(url, ua)
            if r is None:
                ua_tests.append({"ua": label, "error": "请求失败"})
                continue
            ua_tests.append({
                "ua": label, "status": r.status_code, "length": len(r.content),
                "digest": hashlib.md5(r.content).hexdigest()[:8],
                "blocked": self._is_blocked(r, response_text(r)[:20000]),
            })

        hints = []
        if fingerprints:
            hints.append(f"识别到 WAF 指纹：{', '.join(fingerprints)}")
        if blocked:
            hints.append(f"基线请求疑似被拦截（状态 {base.status_code}）")
        ua_blocked = [t for t in ua_tests if t.get("blocked")]
        if ua_blocked and len(ua_blocked) < len(ua_tests):
            hints.append("不同 UA 的拦截结果不一致 → 存在 UA 规则，换浏览器 UA 可缓解")
        if not fingerprints and not blocked:
            hints.append("未发现明显 WAF 指纹或拦截特征（可能无 WAF，或 WAF 静默放行）")

        verdict = "疑似处于 WAF 之后" if (fingerprints or blocked) else "未发现明显 WAF 迹象"
        return {
            "url": url,
            "baseline": {
                "status": base.status_code,
                "server": headers.get("server") or "",
                "content_type": headers.get("content-type") or "",
                "set_cookie": base.headers.get("Set-Cookie") or "",
                "length": len(base.content),
            },
            "fingerprints": fingerprints,
            "blocked": blocked,
            "ua_diff_tests": ua_tests,
            "hints": hints,
            "verdict": verdict,
            "note": "WAF 模块为检测/识别用途（授权靶场），不含绕过 payload 库；差异测试用于判断是否存在 UA 规则。",
        }


# ===========================================================================
# 7. JS 挑战分析（检测 → 提取脚本 → Node 执行取令牌 → 带令牌重放）
# ===========================================================================

CHALLENGE_KEYWORDS = (
    "challenge", "js challenge", "cf_chl", "challenge-platform",
    "turnstile", "javascript challenge", "安全验证", "浏览器检查",
    "校验令牌", "token", "等待", "请稍候",
)

# Node 包装：注入最小 document/window mock，捕获 document.cookie 赋值（脚本报错不阻断输出）
NODE_WRAPPER = """
var __out = {cookie: ""};
var __fakeEl = { innerHTML: "", style: {}, value: "", setAttribute: function(){}, addEventListener: function(){} };
var document = {
  cookie: "",
  set cookie(v) { __out.cookie = v; },
  get cookie() { return __out.cookie; },
  getElementById: function(){ return __fakeEl; },
  querySelector: function(){ return __fakeEl; },
  querySelectorAll: function(){ return []; },
  createElement: function(){ return __fakeEl; },
  body: { appendChild: function(){} }
};
var location = { href: "", reload: function(){} };
var navigator = { userAgent: "Mozilla/5.0" };
var window = globalThis;
try { (function(){ __scripts__ })(); } catch (e) { console.log("__ERR__=" + String(e)); }
console.log("__COOKIE__=" + document.cookie);
"""


class ChallengeBypass:
    """JS 挑战（cookie 计算型）对抗：检测挑战页 → 提取内联脚本 → Node 执行
    生成令牌 → 携带令牌重放验证。适用于靶场自建的简单挑战（如 cookie 计算）。"""

    def __init__(self, cookie="", verify=True, timeout=15):
        self.cookie = cookie
        self.verify = verify
        self.timeout = timeout

    def _get(self, url, cookie=None):
        headers = {"User-Agent": CHROME_UA}
        ck = cookie if cookie is not None else self.cookie
        if ck:
            headers["Cookie"] = ck
        try:
            return _http_get(url, headers=headers, timeout=self.timeout, verify=self.verify,
                             allow_redirects=False)
        except requests.exceptions.RequestException:
            return None

    @staticmethod
    def _extract_inline_scripts(html):
        scripts = []
        for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I):
            code = m.group(1).strip()
            if code:
                scripts.append(code)
        return scripts

    @staticmethod
    def _extract_cookie_assignments(code):
        return [m.group(1).strip()[:200]
                for m in re.finditer(r"document\.cookie\s*=\s*([^;\n]+)", code)]

    @staticmethod
    def _run_in_node(scripts):
        # shutil.which 跨平台且不依赖编码；原先用 `where` + text=True，
        # 在中文 Windows 上会因 GBK 输出解码失败，把已安装的 Node 误判为不存在
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            return {"exit": -1, "error": "未找到 Node.js，无法执行挑战脚本（需安装 Node 并加入 PATH）"}
        joined = "\n".join(scripts)
        code = NODE_WRAPPER.replace("__scripts__", joined)
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", encoding="utf-8") as tf:
            tf.write(code)
            tmp = tf.name
        try:
            proc = subprocess.run([node, tmp], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
            return {"exit": proc.returncode, "stdout": proc.stdout[:8000], "stderr": proc.stderr[:4000]}
        finally:
            os.unlink(tmp)

    @staticmethod
    def _parse_cookie(stdout):
        for line in stdout.splitlines():
            if line.startswith("__COOKIE__="):
                val = line[len("__COOKIE__="):].strip()
                if val:
                    return val
        return None

    def analyze(self, url):
        r = self._get(url)
        if r is None:
            return {"error": "请求失败（连接/超时）"}
        html = response_text(r)[:100000]
        low = html.lower()

        features = [kw for kw in CHALLENGE_KEYWORDS if kw in low]
        inline = self._extract_inline_scripts(html)
        cookie_assigns = []
        for code in inline:
            cookie_assigns.extend(self._extract_cookie_assignments(code))
        is_challenge = (r.status_code in (403, 503) or "cf_chl" in low
                        or ("challenge" in low and inline) or bool(cookie_assigns)
                        or (r.headers.get("Set-Cookie") and inline and features))

        node_res = None
        solved_cookie = None
        if inline:
            node_res = self._run_in_node(inline)
            if node_res and node_res.get("stdout"):
                solved_cookie = self._parse_cookie(node_res["stdout"])

        # 带令牌/现有 cookie 重放
        replay = None
        probe_cookie = solved_cookie or self.cookie
        if probe_cookie:
            rr = self._get(url, cookie=probe_cookie)
            if rr is not None:
                replay = {"status": rr.status_code, "length": len(rr.content),
                          "digest": hashlib.md5(rr.content).hexdigest()[:8],
                          "blocked_like": rr.status_code in BLOCK_STATUS}

        return {
            "url": url,
            "status": r.status_code,
            "set_cookie": r.headers.get("Set-Cookie") or "",
            "features": features,
            "inline_script_count": len(inline),
            "cookie_assignments": cookie_assigns[:10],
            "is_challenge": is_challenge,
            "node_exec": ({"exit": node_res["exit"], "stderr_tail": node_res.get("stderr", "")[-300:]}
                          if node_res and "error" not in node_res else node_res),
            "node_stdout_tail": (node_res or {}).get("stdout", "")[-500:],
            "solved_cookie": solved_cookie or None,
            "replay_after": replay,
            "verdict": "疑似 JS 挑战页" if is_challenge else "未发现明显 JS 挑战特征",
            "note": "靶场自建 cookie 计算型挑战可自动提取执行；商业挑战（如 Turnstile）需浏览器环境，工具提供检测与人工接力（浏览器过挑战后复制 Cookie 交给 --cookie）。",
        }


# ===========================================================================
# 8. 验证码检测（检测与分类 + 人工接力，不提供自动识别）
# ===========================================================================

CAPTCHA_KEYWORDS = (
    "captcha", "验证码", "安全验证", "图形验证", "滑块验证", "滑动验证",
    "点选验证", "短信验证", "verification code", "security code", "verify",
)
CAPTCHA_FIELD_RE = re.compile(r'name=["\']?([^"\'>]*(?:captcha|verify|verif|code|check|token)[^"\'>]*)', re.I)
CAPTCHA_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']*(?:captcha|verify|verif|code|rand|check)[^"\']*)', re.I)


class CaptchaDetector:
    """验证码检测与分类。仅检测/指引人工接力，不做自动识别（识别属灰产能力）。"""

    def __init__(self, cookie="", verify=True, timeout=15):
        self.cookie = cookie
        self.verify = verify
        self.timeout = timeout

    def detect(self, url):
        headers = {"User-Agent": CHROME_UA}
        if self.cookie:
            headers["Cookie"] = self.cookie
        r = _http_get(url, headers=headers, timeout=self.timeout, verify=self.verify)
        if r is None:
            return {"error": "请求失败: 目标不可达（检查 URL / 证书 / 网络）"}
        html = response_text(r)[:100000]
        low = html.lower()

        hits = [kw for kw in CAPTCHA_KEYWORDS if kw in low]
        fields = [m.group(0)[:60] for m in CAPTCHA_FIELD_RE.finditer(html)][:10]
        imgs = [m.group(1)[:120] for m in CAPTCHA_IMG_RE.finditer(html)][:10]
        comps = list(dict.fromkeys(
            re.findall(r"(geetest|yidun|nc_|tcaptcha|JCaptcha|kaptcha|gt4?|slider)", low)))[:6]

        if hits or fields or imgs or comps:
            if any(k in low for k in ("滑块", "slider", "geetest", "nc_", "yidun", "滑动")):
                ctype = "滑块/行为验证"
            elif any(k in low for k in ("点选", "选字", "点击")):
                ctype = "点选验证"
            elif any(k in low for k in ("短信", "sms")):
                ctype = "短信验证"
            elif imgs or any(k in low for k in ("captcha", "验证码", "图形验证")):
                ctype = "图形字符验证"
            else:
                ctype = "疑似验证码（类型待人工确认）"
        else:
            ctype = "无验证码"

        has_captcha = bool(hits or fields or imgs or comps)
        return {
            "url": url,
            "status": r.status_code,
            "keywords": hits[:10],
            "form_fields": fields,
            "captcha_images": imgs,
            "components": comps,
            "type": ctype,
            "has_captcha": has_captcha,
            "verdict": "检测到验证码（" + ctype + "）" if has_captcha else "未检测到验证码",
            "manual_steps": [
                "本工具不提供验证码自动识别（属灰产能力，无法提供）。",
                "靶场训练请走人工接力：浏览器手动完成验证 → 复制 Cookie → 用 --cookie 或界面 Cookie 字段继续。",
                "如需自动化回归，可在测试环境关闭验证码或用固定测试码绕过。",
            ],
        }


# ===========================================================================
# 命令行入口
# ===========================================================================


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="anticrawl",
        description="靶场反爬对抗模块：字体反爬/文本混淆/隐形水印/鉴权测试/JS接口加密。仅限授权目标。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-m", "--module", required=True,
                   choices=["font", "decode", "water", "auth", "js", "waf", "challenge", "captcha"],
                   help="子模块: font字体 / decode文本 / water水印 / auth鉴权 / js接口加密 / waf检测 / challenge JS挑战 / captcha验证码")
    p.add_argument("--url", help="目标页面 URL")
    p.add_argument("--cookie", default="", help='Cookie，如 "session=abc"')
    p.add_argument("--no-verify", action="store_true", help="跳过 HTTPS 证书校验")
    # font
    p.add_argument("--font-file", help="本地字体文件（.ttf/.otf/.woff/.woff2）")
    p.add_argument("--text", help="待解码的混淆文本")
    p.add_argument("--ref-font", help="参考字体路径（默认自动找 arial.ttf 等）")
    # decode
    p.add_argument("--input", help="decode 模块的输入文本（也可用 --text）")
    # water
    p.add_argument("--file", help="水印模块的图片文件")
    p.add_argument("--action", choices=["detect", "clean"], default="detect")
    p.add_argument("--method", choices=["auto", "lsb_strip", "fft_lowpass", "gaussian", "median", "posterize"],
                   default="auto")
    p.add_argument("--out", help="清理后图片输出路径")
    # auth
    p.add_argument("--http-method", choices=["GET", "POST"], default="GET")
    p.add_argument("--body-json", help="POST 时附加的 JSON 字段")
    # js
    p.add_argument("--js-action", choices=["scan", "run", "replay"], default="scan")
    p.add_argument("--script", help="js run：JS 脚本文件路径")
    p.add_argument("--captures", help="js replay：捕获请求 JSON 文件路径")
    args = p.parse_args(argv)

    verify = not args.no_verify
    out = {}

    try:
        if args.module == "font":
            f = FontAntiCrawl(cookie=args.cookie, verify=verify)
            if args.font_file:
                with open(args.font_file, "rb") as fh:
                    data = fh.read()
                f.font_source = args.font_file
                f.mapping = f.build_mapping(data, args.ref_font)
                out = {"font_source": args.font_file,
                       "mapping": {f"&#x{k:X};": v for k, v in sorted(f.mapping.items())}}
                if args.text:
                    out["original"], out["decoded"] = args.text, f.decode_text(args.text)
            elif args.url:
                out = f.full_run(args.url, text=args.text or "", ref_font_path=args.ref_font)
            else:
                p.error("font 模块需要 --url 或 --font-file")
        elif args.module == "decode":
            text = args.text if args.text is not None else args.input
            if text is None:
                p.error("decode 模块需要 --text")
            out = TextDecoder.decode_chain(text)
        elif args.module == "water":
            if not args.file:
                p.error("water 模块需要 --file")
            if args.action == "detect":
                out = WatermarkTool.detect(args.file, os.path.basename(args.file))
            else:
                cleaned, used = WatermarkTool.clean(args.file, args.method)
                out_path = args.out or (os.path.splitext(args.file)[0] + "_clean.png")
                with open(out_path, "wb") as fh:
                    fh.write(cleaned)
                out = {"method_used": used, "output": out_path, "bytes": len(cleaned)}
        elif args.module == "auth":
            if not args.url:
                p.error("auth 模块需要 --url")
            out = AuthTester(cookie=args.cookie, verify=verify).test(
                args.url, method=args.http_method, body_json=args.body_json)
        elif args.module == "js":
            js = JSAnalyzer(cookie=args.cookie, verify=verify)
            if args.js_action == "scan":
                if not args.url:
                    p.error("js scan 需要 --url")
                out = js.scan(args.url)
                out["key_candidates"] = js.extract_keys(args.url)
            elif args.js_action == "run":
                if not args.script:
                    p.error("js run 需要 --script")
                with open(args.script, "r", encoding="utf-8-sig") as fh:
                    out = JSAnalyzer.run_script(fh.read())
            elif args.js_action == "replay":
                if not args.captures:
                    p.error("js replay 需要 --captures(JSON)")
                with open(args.captures, "r", encoding="utf-8-sig") as fh:
                    out = JSAnalyzer.replay(fh.read(), cookie=args.cookie, verify=verify)
        elif args.module == "waf":
            if not args.url:
                p.error("waf 模块需要 --url")
            out = WAFDetector(cookie=args.cookie, verify=verify).detect(args.url)
        elif args.module == "challenge":
            if not args.url:
                p.error("challenge 模块需要 --url")
            out = ChallengeBypass(cookie=args.cookie, verify=verify).analyze(args.url)
        elif args.module == "captcha":
            if not args.url:
                p.error("captcha 模块需要 --url")
            out = CaptchaDetector(cookie=args.cookie, verify=verify).detect(args.url)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    except requests.exceptions.RequestException as exc:
        print(json.dumps({"error": f"请求失败: {exc}"}, ensure_ascii=False), flush=True)
        return 1

    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
