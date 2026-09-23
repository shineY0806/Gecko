"""Gecko —— 实战型网页抓取工具箱。

本包不含业务逻辑，只负责把 core / ui 两个目录加入模块搜索路径，
使 `import range_crawler`、`import smart_extract` 这类既有平铺写法
在源码运行与打包后都照旧可用，不必逐个改写 import。

目录约定：
    gecko/core/   引擎层：爬取、请求后端、结构化提取、反爬对抗
    gecko/ui/     界面层：本地服务、页面模板、视觉层
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

for _sub in ("core", "ui"):
    _p = os.path.join(_HERE, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
