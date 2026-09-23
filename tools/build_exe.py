"""把 Gecko 打包成一个独立的 exe（onedir 目录模式，双击秒开）。

用法：  python tools/build_exe.py
产物：  dist\\Gecko\\Gecko.exe

打包后的 exe 不需要电脑装 Python，双击即可打开界面。
改完代码后重新执行一次本脚本即可。

注意：本脚本从任意目录运行都能定位到项目根目录，不要再 cd 到 tools/ 下跑。
"""

import os
import sys

import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))          # tools/
ROOT = os.path.dirname(HERE)                                # 项目根
CORE = os.path.join(ROOT, "gecko", "core")
UI = os.path.join(ROOT, "gecko", "ui")
ASSETS = os.path.join(ROOT, "assets")

# 换到项目根执行：PyInstaller 的相对路径参数都以它为基准
os.chdir(ROOT)

# 随 exe 一起打包的非代码文件（界面 + 视觉层 + 壁虎图标）
DATA_FILES = [
    (os.path.join(UI, "webui.html"), "."),
    (os.path.join(UI, "apple.css"), "."),
    (os.path.join(ASSETS, "gecko_favicon.png"), "."),
    (os.path.join(ASSETS, "gecko.ico"), "."),
]

# PyInstaller 的静态分析抓不到的动态导入，手动补上
HIDDEN_IMPORTS = [
    "w3lib",          # URL 规范化，range_crawler 里是 try import
    "lxml",           # HTML 解析器，同样是 try import
    "bs4",
    "webview",        # pywebview 桌面窗口
    "smart_extract",  # 结构化提取与多格式导出（range_crawler 里是 try import）
    "adaptive",       # v1.5.0 自愈选择器（同样是 try import）
    "openpyxl",       # XLSX 导出；缺失时导出器自动降级跳过 XLSX
]

args = [
    "--noconfirm",      # 覆盖已有产物，不询问
    "--clean",          # 清缓存重新打包
    "--onedir",         # 目录模式：免去 onefile 每次启动解压几十 MB 的等待，双击秒开
    "--noconsole",      # 不弹命令行黑窗口
    "--name=Gecko",
    "--specpath=tools",     # spec 文件收进 tools/，不散落在项目根目录
    # v1.4.3 性能：patchright / playwright 驱动体积巨大，且需另装浏览器内核才可用，
    # 打进 exe 只会拖慢启动。排除后界面能力探测会自动禁用渲染选项并提示安装
    # （源码 / bat 方式运行不受影响，装了照常可用）
    "--exclude-module=patchright",
    "--exclude-module=playwright",
]

# 引擎模块在 gecko/core 下，PyInstaller 分析入口时不会自动带上
for p in (CORE, UI, ROOT):
    args.append("--paths=%s" % p)

icon = os.path.join(ASSETS, "gecko.ico")
if os.path.exists(icon):
    args.append("--icon=%s" % icon)

for src, dst in DATA_FILES:
    if os.path.exists(src):
        args.append("--add-data=%s;%s" % (src, dst))
    else:
        print("[WARN] 缺少资源文件，跳过打包: %s" % src)

for m in HIDDEN_IMPORTS:
    args.append("--hidden-import=%s" % m)

args.append(os.path.join(UI, "webui.py"))

print("开始打包，需要几分钟，请耐心等待 ...")
print("项目根:", ROOT)
PyInstaller.__main__.run(args)

print()
print("打包完成：%s" % os.path.abspath(os.path.join("dist", "Gecko", "Gecko.exe")))
