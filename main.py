"""
Caddie 求职管家 - 桌面应用入口
运行：python main.py
"""
import sys
import time
import threading
import webbrowser

PORT = 8766
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"


def start_server():
    import uvicorn
    uvicorn.run("server:app", host=HOST, port=PORT, log_level="warning", reload=False)


def wait_for_server(timeout=15):
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    print("🏌️  启动 Caddie 求职管家...")
    threading.Thread(target=start_server, daemon=True).start()

    if not wait_for_server():
        print("❌ 服务器启动失败，请检查端口 8766 是否被占用")
        sys.exit(1)
    print(f"✅ 服务已就绪：{URL}")

    try:
        import webview
        print("🖥️  打开桌面窗口...")
        webview.create_window(
            title="Caddie 求职管家",
            url=URL, width=1280, height=860,
            min_size=(1000, 680), resizable=True,
        )
        webview.start(debug=False)
    except ImportError:
        print("💡 未安装 pywebview，回退到浏览器（pip install pywebview 可获桌面窗口）")
        webbrowser.open(URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 Caddie 已关闭")


if __name__ == "__main__":
    main()
