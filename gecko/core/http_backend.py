# -*- coding: utf-8 -*-
"""HTTP 后端抽象层：在 requests 与 curl_cffi 之间自由切换。

为什么要这一层
--------------
requests 发出的 TLS 握手是固定的（系统 OpenSSL + 固定的扩展顺序与 Cipher 列表），
HTTP/2 也没有，因此在 JA3 / JA4 / Akamai H2 指纹检测面前一眼就被认出来是脚本。
curl_cffi 复用 curl-impersonate，能发出与真实 Chrome / Safari 一致的 TLS 与 HTTP/2 指纹，
且 API 与 requests 高度兼容 —— 所以它做成"可选后端"而不是替换整条链路。

对外接口只有两个函数，调用方（range_crawler / anticrawl）不需要写分支：

    available_backends()                 -> {"requests": bool, "curl_cffi": bool, ...}
    create_session(backend, impersonate) -> (session, actual_backend_name)

返回的 session 至少支持 requests.Session 的这些用法：
    .headers / .cookies / .proxies / .verify / .get() / .post()
    resp.status_code / .headers / .text / .content / .url / .iter_content()

差异只有一处：curl_cffi 的 session 没有 adapters，不能 mount(HTTPAdapter)。
所以连接池调优那段必须写成 `if has_adapters(session):`，别直接 mount。
"""

from __future__ import annotations

import os
import shutil
import tempfile

# 常用的浏览器指纹目标。curl_cffi 版本不同支持列表也不同，
# 因此运行时探测而非硬编码，避免传了不支持的值直接抛异常。
DEFAULT_IMPERSONATE = "chrome"

FINGERPRINT_ORDER = [
    "chrome",
    "chrome124",
    "chrome120",
    "chrome110",
    "chrome107",
    "chrome104",
    "chrome101",
    "chrome99",
    "edge101",
    "edge99",
    "safari17_0",
    "safari15_5",
    "safari",
    "firefox",
    "firefox133",
    "tor145",
]


def _ascii_ca_path(ca):
    """把含非 ASCII 字符的 CA 证书路径换成一个纯 ASCII 副本的路径。

    Windows 上这是个真会踩的坑：libcurl 用 ANSI 接口打开 CA 文件，
    一旦 certifi 装在中文用户名下（C:\\Users\\张耀阳\\...\\cacert.pem），
    就会报 curl: (77) error adding trust anchors —— 表现为所有 https 请求 SSL 失败，
    而同样的地址用 requests 完全正常。把证书复制到 ASCII 路径再传给 curl 即可。
    """
    if not ca:
        return ca
    ca = str(ca)
    if ca.isascii():
        return ca
    for base in (os.environ.get("PUBLIC") or "", os.environ.get("SystemRoot") or "",
                 tempfile.gettempdir()):
        base = str(base)
        if not base or not base.isascii():
            continue
        try:
            d = os.path.join(base, "Temp" if base.endswith("Windows") else "", "curlca")
            d = os.path.normpath(d)
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, "cacert.pem")
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(ca):
                shutil.copyfile(ca, dst)
            return dst
        except Exception:
            continue
    return ca


def _ca_for_curl(verify):
    """给 curl 用的 CA 参数：需要校验时确保路径是 ASCII；不校验时原样返回 False。"""
    if verify is False or verify in (0, "0", "false", "False"):
        return False
    if verify is True:
        try:
            import certifi
            return _ascii_ca_path(certifi.where())
        except Exception:
            return True
    return _ascii_ca_path(verify)


def _import_curl_cffi():
    try:
        from curl_cffi import requests as curl_requests  # type: ignore
        return curl_requests
    except Exception:
        return None


def available_backends():
    """探测本机可用的后端，供 CLI/GUI 展示与自动降级判断。"""
    info = {"requests": False, "curl_cffi": False, "curl_cffi_version": None,
            "impersonate_targets": []}
    try:
        import requests  # noqa: F401
        info["requests"] = True
    except Exception:
        pass

    mod = _import_curl_cffi()
    if mod is not None:
        info["curl_cffi"] = True
        info["curl_cffi_version"] = getattr(mod, "__version__", None)
        targets = []
        # 不同版本暴露方式不同：有的在 Session 上有 _impersonate_targets，
        # 有的在顶层有 BrowserTypeLiteral / __all__。一律尽力探测，探测不到就用候选列表。
        try:
            sess_cls = getattr(mod, "Session", None)
            cand = getattr(sess_cls, "_impersonate_targets", None) or \
                   getattr(mod, "BrowserTypeLiteral", None)
            if isinstance(cand, dict):
                targets = [k for k in cand if isinstance(k, str)]
            elif isinstance(cand, (list, tuple)):
                targets = [str(k) for k in cand]
        except Exception:
            targets = []
        info["impersonate_targets"] = targets or list(FINGERPRINT_ORDER)
    return info


def resolve_impersonate(wanted, targets=None):
    """把用户想用的指纹名映射到当前版本真正支持的取值。

    版本升级后旧名字可能被移除（例如 chrome100），直接传会抛异常；
    这里做一次"精确匹配 -> 前缀回退 -> 默认"，保证永远传得进去。
    """
    info = available_backends()
    targets = targets or info.get("impersonate_targets") or FINGERPRINT_ORDER
    wanted = (wanted or "").strip()
    if not wanted:
        wanted = DEFAULT_IMPERSONATE
    if wanted in targets:
        return wanted
    base = wanted.rstrip("0123456789._-")
    for t in targets:
        if t == base or t.startswith(base):
            return t
    return DEFAULT_IMPERSONATE if DEFAULT_IMPERSONATE in targets else (targets[0] if targets else None)


def has_adapters(session):
    """该 session 是否支持 mount(HTTPAdapter)。curl_cffi 返回 False。"""
    return hasattr(session, "get_adapter") and hasattr(session, "mount")


def create_session(backend="auto", impersonate=None, verify=True, user_agent=None,
                   headers=None, cookie=None, proxies=None, pool_size=None,
                   log=None):
    """创建一个 requests 兼容的 session。

    backend: "requests" | "curl_cffi" | "auto"
    impersonate: 浏览器指纹名，如 chrome / safari / firefox；None 表示不伪装
    返回 (session, actual_backend)；失败自动降级到 requests，绝不抛异常。
    """
    def _emit(msg, level="INFO"):
        if log:
            try:
                log(msg, level)
            except TypeError:
                log(msg)

    use_curl = backend in ("auto", "curl_cffi")
    curl_mod = _import_curl_cffi() if use_curl else None

    if backend == "curl_cffi" and curl_mod is None:
        _emit("未安装 curl_cffi，回退到 requests（pip install curl_cffi 可启用浏览器指纹）", "WARN")
    if curl_mod is not None and not impersonate and backend in ("auto", "curl_cffi"):
        # 未指定指纹时默认 chrome —— 这是"伪装成正常浏览器"的最常见选择，
        # 也是 curl_cffi 能起作用的必要条件（不传 impersonate 就等于普通 curl）
        impersonate = DEFAULT_IMPERSONATE

    if curl_mod is not None and impersonate:
        target = resolve_impersonate(impersonate)
        try:
            s = curl_mod.Session(impersonate=target, verify=_ca_for_curl(verify))
            _apply_common(s, user_agent, headers, cookie, proxies)
            _emit(f"HTTP 后端: curl_cffi（浏览器指纹 {target}）")
            return s, f"curl_cffi:{target}"
        except Exception as exc:
            _emit(f"curl_cffi 初始化失败({exc})，回退到 requests", "WARN")

    import requests as _rq
    s = _rq.Session()
    s.verify = verify
    _apply_common(s, user_agent, headers, cookie, proxies)
    if pool_size and has_adapters(s):
        try:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size,
                                  max_retries=0)
            s.mount("http://", adapter)
            s.mount("https://", adapter)
        except Exception:
            pass
    return s, "requests"


def _apply_common(s, user_agent=None, headers=None, cookie=None, proxies=None):
    """把 UA / 自定义头 / Cookie / 代理写到 session 上。"""
    try:
        if user_agent:
            s.headers["User-Agent"] = user_agent
        for k, v in (headers or {}).items():
            s.headers[str(k)] = str(v)
        if cookie:
            s.headers["Cookie"] = cookie
        if proxies:
            s.proxies = dict(proxies)
    except Exception:
        pass


def response_bytes(resp, limit=None):
    """从响应里读最多 limit 字节，返回 (body, truncated)。

    统一处理两个后端：优先 iter_content（省内存、可截断），不支持时退回 .content。
    """
    truncated = False
    try:
        body = b""
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            body += chunk
            if limit is not None and len(body) > limit:
                truncated = True
                break
        return body, truncated
    except Exception:
        try:
            data = resp.content or b""
        except Exception:
            data = b""
        if limit is not None and len(data) > limit:
            return data[:limit], True
        return data, False


def close_response(resp):
    try:
        resp.close()
    except Exception:
        pass


def exception_types():
    """返回 (RequestException, SSLError, Timeout, ConnectionError) 元组。

    curl_cffi 有自己的异常体系，但都继承自 requests 的异常别名吗？不保证，
    所以统一在调用侧按"requests 异常 + 通用 Exception"两层捕获，这里只提供
    requests 的一组，供 isinstance 判断使用。
    """
    import requests
    return (requests.exceptions.RequestException,
            requests.exceptions.SSLError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError)
