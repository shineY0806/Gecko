"""Gecko 界面层。

    webui.py     本地服务 + 接口（启动入口）
    webui.html   页面模板
    apple.css    视觉层（素白工业主题）
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
