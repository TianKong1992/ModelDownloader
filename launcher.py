"""
模型下载管理工具 — 桌面版启动器
启动 Flask 后端，然后用原生窗口加载页面
"""
import sys
import threading
from pathlib import Path

if getattr(sys, 'frozen', False):
    _USER_DIR = Path(sys.executable).parent
else:
    _USER_DIR = Path(__file__).parent.absolute()

import webview
from tool_server import app

TITLE = "模型下载管理工具"
PORT = 5000


def run_flask():
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    webview.create_window(
        title=TITLE,
        url=f"http://127.0.0.1:{PORT}",
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
    )
    webview.start()
