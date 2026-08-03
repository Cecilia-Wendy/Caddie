"""
Caddie 求职管家 - 桌面应用入口
运行：python main.py
"""
import os
import subprocess
import sys
import time
import threading
import traceback
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("CADDIE_PORT", "8766"))
HOST = os.environ.get("CADDIE_HOST", "127.0.0.1")
URL = f"http://{HOST}:{PORT}"


def desktop_log_path():
    path = Path.home() / ".caddie" / "logs" / "desktop.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def console_write(message: str, *, error: bool = False):
    """Write diagnostics without crashing a consoleless or GBK Windows build."""
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    text = str(message)
    try:
        stream.write(text + "\n")
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stream.write(safe + "\n")
        stream.flush()


def start_server():
    try:
        import uvicorn
        from server import app
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            log_level="warning",
            reload=False,
            log_config=None if getattr(sys, "frozen", False) else uvicorn.config.LOGGING_CONFIG,
        )
    except Exception:
        details = traceback.format_exc()
        console_write(details, error=True)
        diagnostic_path = os.environ.get("CADDIE_STARTUP_LOG") or desktop_log_path()
        try:
            with open(diagnostic_path, "a", encoding="utf-8") as handle:
                handle.write(details + "\n")
        except OSError:
            pass


def start_server_process():
    """Run the packaged Windows server outside the WebView GUI process."""
    log_path = desktop_log_path()
    env = os.environ.copy()
    env["CADDIE_STARTUP_LOG"] = str(log_path)
    command = [sys.executable, "--server"]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_handle = open(log_path, "a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            creationflags=creationflags,
        )
    except Exception:
        log_handle.close()
        raise
    return process, log_handle


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
    if "--server" in sys.argv:
        start_server()
        return
    if "--mcp" in sys.argv:
        from caddie_mcp import main as run_mcp
        run_mcp()
        return
    if "--agent-cli" in sys.argv:
        marker = sys.argv.index("--agent-cli")
        sys.argv = [sys.argv[0], *sys.argv[marker + 1:]]
        from caddie_agent_cli import main as run_agent_cli
        run_agent_cli()
        return

    if getattr(sys, "frozen", False):
        try:
            from agent_launchers import ensure_packaged_launchers
            ensure_packaged_launchers()
        except OSError as exc:
            console_write(f"Agent 启动器初始化失败：{exc}", error=True)

    console_write("启动 Caddie 求职管家...")
    # The optional launch agent may already own the local server. Reusing it
    # avoids a noisy bind failure and makes reopening the desktop shell cheap.
    server_process = None
    server_log = None
    if not wait_for_server(timeout=0.8):
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            server_process, server_log = start_server_process()
        else:
            threading.Thread(target=start_server, daemon=True).start()

    if not wait_for_server():
        if server_process is not None:
            server_process.terminate()
        console_write(
            f"服务器启动失败，请检查 {desktop_log_path()}",
            error=True,
        )
        sys.exit(1)
    console_write(f"服务已就绪：{URL}")

    if os.environ.get("CADDIE_HEADLESS") == "1":
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return

    try:
        import webview
        console_write("打开桌面窗口...")
        webview.create_window(
            title="Caddie",
            url=URL, width=1280, height=860,
            min_size=(1000, 680), resizable=True,
        )
        def after_gui_started():
            if sys.platform != "darwin":
                return
            try:
                from AppKit import NSApplication, NSApplicationActivationPolicyRegular
                app = NSApplication.sharedApplication()
                app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
                app.activateIgnoringOtherApps_(True)
            except Exception:
                pass
            try:
                subprocess.run(
                    [
                        "/usr/bin/osascript", "-e",
                        'tell application id "app.caddie.alpha" to activate',
                    ],
                    check=False, timeout=3,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        # pywebview runs this callback after its GUI loop is ready. On newer
        # macOS releases AppKit activation from a background Timer can be
        # ignored, leaving a visible Caddie window behind the current app.
        webview.start(after_gui_started, debug=False)
    except ImportError:
        console_write("未安装 pywebview，回退到浏览器（pip install pywebview 可获桌面窗口）")
        webbrowser.open(URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            console_write("Caddie 已关闭")
    finally:
        if server_process is not None and server_process.poll() is None:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()
        if server_log is not None:
            server_log.close()


if __name__ == "__main__":
    main()
