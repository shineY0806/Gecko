"""Gecko 引擎层。

    range_crawler   爬取主引擎（CLI + 库两种用法）
    http_backend    HTTP 请求后端（requests / curl_cffi 指纹 / 浏览器渲染）
    smart_extract   结构化提取与多格式导出
    anticrawl       反爬对抗与防护对抗检测
    _compat_css     视觉层老内核兜底样式生成器
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

__all__ = ["range_crawler", "http_backend", "smart_extract", "anticrawl"]
