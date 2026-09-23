"""练习靶场服务：多线程（并发抓取时不会被单线程 http.server 打死）、不打日志。

配合 启动演示靶场.bat 使用，默认监听 http://127.0.0.1:8000，
内容为 demo_site 目录下的演示页面。关掉窗口即停止。
"""
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler


class QuietHandler(SimpleHTTPRequestHandler):
    """静默处理：不在项目目录里留日志文件，也不刷屏。"""

    def log_message(self, fmt, *args):
        pass


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_site")
    os.chdir(root)
    srv = ThreadingHTTPServer(("127.0.0.1", 8000), QuietHandler)
    srv.daemon_threads = True
    sys.stderr.write("serving %s on http://127.0.0.1:8000\n" % root)
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
