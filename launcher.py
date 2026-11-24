"""Windows executable entry point for Network Locator."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from network_locator import APP_ID, APP_NAME, __version__
from network_locator.server import (
    DEFAULT_PORT,
    FRONTEND_DIR,
    shutdown_server,
    start_server,
)
from network_locator.win_network import discover_networks

CONFIG_FILENAME = "config.json"
ICON_FILENAME = "network-locator.ico"
NATIVE_WINDOW_TITLE = f"{APP_NAME} / Network Locator"
NATIVE_WINDOW_TIMEOUT = 15.0
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
APP_USER_MODEL_ID = "642671.NetworkLocator.Windows"
GUI_MODES = {"auto", "native", "app-window", "browser"}


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "NetworkLocator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_icon_path() -> Path | None:
    candidates = [
        FRONTEND_DIR.parent / "assets" / ICON_FILENAME,
        SOURCE_ROOT / "assets" / ICON_FILENAME,
    ]
    return next((path for path in candidates if path.is_file()), None)


def load_config() -> dict:
    path = app_data_dir() / CONFIG_FILENAME
    defaults = {"port": DEFAULT_PORT, "browser": ""}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if isinstance(value, dict):
        defaults.update(
            {key: value[key] for key in defaults if key in value}
        )
    elif not path.exists():
        try:
            path.write_text(
                json.dumps(defaults, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return defaults


def configure_logging() -> Path:
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    return log_file


def show_error(message: str) -> None:
    logging.error(message)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def show_info(message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
            return
        except Exception:
            pass
    print(message)


def set_app_user_model_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        logging.debug("failed to set AppUserModelID", exc_info=True)


def webview2_runtime_version() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    locations = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        ),
    ]
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def find_browser(configured: str = "") -> str | None:
    if configured and os.path.isfile(configured):
        return configured
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates = [
        os.path.join(program_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(program_files, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(program_files, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(program_files_x86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
    ]
    return next((path for path in candidates if os.path.isfile(path)), None)


def browser_profile_dir() -> Path:
    path = app_data_dir() / "browser-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_native_window(url: str) -> tuple[bool, str]:
    runtime = webview2_runtime_version()
    if not runtime:
        return False, "未检测到 Microsoft Edge WebView2 运行时"
    try:
        import webview
    except Exception as exc:
        logging.exception("pywebview is unavailable")
        return False, f"pywebview 不可用（{exc}）"

    storage_path = app_data_dir() / "webview"
    icon = app_icon_path()
    shown = threading.Event()
    loaded = threading.Event()
    closed = threading.Event()
    finished = threading.Event()
    try:
        window = webview.create_window(
            title=NATIVE_WINDOW_TITLE,
            url=url,
            width=1120,
            height=720,
            resizable=True,
            min_size=(900, 600),
            background_color="#f4f7fb",
            text_select=False,
        )
        window.events.shown += shown.set
        window.events.loaded += loaded.set
        window.events.closed += closed.set
    except Exception as exc:
        logging.exception("native window creation failed")
        return False, f"原生窗口创建失败（{exc}）"

    def watchdog() -> None:
        deadline = time.monotonic() + NATIVE_WINDOW_TIMEOUT
        while not finished.is_set() and time.monotonic() < deadline:
            if loaded.wait(0.5):
                return
        if finished.is_set() or loaded.is_set():
            return
        logging.error(
            "native window did not finish loading within %.0f seconds",
            NATIVE_WINDOW_TIMEOUT,
        )
        try:
            window.destroy()
        except Exception:
            logging.exception("failed to close the unresponsive native window")

    threading.Thread(
        target=watchdog,
        name="native-window-watchdog",
        daemon=True,
    ).start()

    started = time.monotonic()
    logging.info("native window starting webview2=%s url=%s", runtime, url)
    try:
        webview.start(
            gui="edgechromium",
            icon=str(icon) if icon else None,
            private_mode=False,
            storage_path=str(storage_path),
        )
    except Exception as exc:
        logging.exception("native window failed")
        return False, f"WebView2 窗口启动失败（{exc}）"
    finally:
        finished.set()

    elapsed = time.monotonic() - started
    if loaded.is_set() or closed.is_set():
        logging.info(
            "native window closed after %.1fs loaded=%s shown=%s",
            elapsed,
            loaded.is_set(),
            shown.is_set(),
        )
        return True, ""
    logging.error("native window closed before the page finished loading")
    return False, "原生窗口已打开但页面未能加载"


def launch_app_window(
    url: str,
    browser: str,
) -> subprocess.Popen:
    profile_dir = browser_profile_dir()
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            # Edge/Chrome throttles page timers when the app window is
            # minimized. Keep the local UI responsive instead.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-component-update",
            "--window-size=1280,820",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    logging.info("browser=%s pid=%s url=%s", browser, process.pid, url)
    return process


def notify_fallback(message: str) -> None:
    logging.warning("fallback: %s", message)

    def _show() -> None:
        time.sleep(1.0)
        show_info(message)

    threading.Thread(target=_show, name="fallback-notice", daemon=True).start()


def run_selftest(result_file: str) -> int:
    result_path = Path(result_file).resolve()
    icon = app_icon_path()
    frontend = {
        "dir": str(FRONTEND_DIR),
        "index_html": (FRONTEND_DIR / "index.html").is_file(),
        "app_js": (FRONTEND_DIR / "app.js").is_file(),
        "styles_css": (FRONTEND_DIR / "styles.css").is_file(),
        "csv_js": (FRONTEND_DIR / "csv.js").is_file(),
    }
    result: dict = {
        "app": APP_ID,
        "name": APP_NAME,
        "version": __version__,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "status": "error",
        "frontend": frontend,
        "icon": str(icon) if icon else "",
        "webview_module": importlib.util.find_spec("webview") is not None,
        "webview2_runtime": webview2_runtime_version(),
    }
    try:
        targets, warnings = discover_networks()
        result.update(
            {
                "status": "ok",
                "network_count": len(targets),
                "networks": [target.as_dict() for target in targets],
                "warnings": warnings,
            }
        )
        if not all(value for key, value in frontend.items() if key != "dir"):
            result["status"] = "error"
            result["error"] = "bundled WebUI files are incomplete"
        exit_code = 0 if result["status"] == "ok" else 1
    except Exception as exc:
        result["error"] = str(exc)
        logging.exception("selftest failed")
        exit_code = 1
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return exit_code


def run_server_only(port: int, ready_file: str, duration: float) -> int:
    server, thread, url = start_server(port)
    ready_path = Path(ready_file).resolve()
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_text(
        json.dumps(
            {
                "url": url,
                "port": server.server_address[1],
                "pid": os.getpid(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logging.info("serve-only url=%s duration=%s", url, duration)
    try:
        if duration > 0:
            time.sleep(duration)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_server(server, thread)
    return 0


def wait_for_browser(process: subprocess.Popen) -> None:
    while True:
        if process.poll() is not None:
            logging.info("browser closed; exit_code=%s", process.returncode)
            return
        time.sleep(0.75)


def gui_mode() -> str:
    mode = (os.environ.get("NETWORK_LOCATOR_GUI") or "auto").strip().lower()
    if mode in GUI_MODES:
        return mode
    logging.warning("unknown NETWORK_LOCATOR_GUI=%s; using auto", mode)
    return "auto"


def run_application(port: int) -> int:
    config = load_config()
    mode = gui_mode()
    try:
        server, thread, url = start_server(port)
    except OSError as exc:
        show_error(
            "无法启动 Network Locator 本地服务。\n\n"
            f"端口 {port} 可能已被占用（{exc}）。\n"
            "如果应用已在运行，请使用已打开的窗口；"
            "否则请关闭占用该端口的程序后重试。"
        )
        return 2

    browser_process: subprocess.Popen | None = None
    native_reason = ""
    try:
        logging.info("serving url=%s gui_mode=%s", url, mode)
        if mode in {"auto", "native"}:
            ok, reason = run_native_window(url)
            if ok:
                return 0
            native_reason = reason
            logging.warning("native window unavailable: %s", reason)
            if mode == "native":
                show_error(f"无法启动原生窗口：{reason}")
                return 3

        if mode in {"auto", "app-window"}:
            browser = find_browser(
                os.environ.get("NETWORK_LOCATOR_BROWSER")
                or str(config.get("browser") or "")
            )
            if browser:
                browser_process = launch_app_window(url, browser)
                if native_reason:
                    notify_fallback(f"{native_reason}，已改用浏览器应用窗口打开。")
                wait_for_browser(browser_process)
                return 0
            logging.warning("no Edge/Chrome found; falling back to default browser")

        webbrowser.open(url)
        if native_reason:
            show_info(
                f"{native_reason}，且未找到 Microsoft Edge 或 Google Chrome，"
                "已使用默认浏览器打开。\n\n"
                "关闭此提示即退出 Network Locator。"
            )
        else:
            show_info(
                "已使用默认浏览器打开 Network Locator。\n\n"
                "关闭此提示即退出 Network Locator。"
            )
        return 0
    finally:
        if browser_process is not None and browser_process.poll() is None:
            try:
                browser_process.terminate()
                browser_process.wait(timeout=5)
            except Exception:
                pass
        shutdown_server(server, thread)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--result-file")
    parser.add_argument("--serve-only", action="store_true")
    parser.add_argument("--ready-file")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    log_file = configure_logging()
    logging.info(
        "starting version=%s frozen=%s log=%s",
        __version__,
        bool(getattr(sys, "frozen", False)),
        log_file,
    )
    if args.selftest:
        if not args.result_file:
            show_error("--selftest requires --result-file")
            return 2
        return run_selftest(args.result_file)
    if args.serve_only:
        if not args.ready_file:
            show_error("--serve-only requires --ready-file")
            return 2
        port = 0 if args.port is None else args.port
        return run_server_only(port, args.ready_file, args.duration)

    configured = load_config()
    port = args.port
    if port is None:
        port = int(os.environ.get("NETWORK_LOCATOR_PORT") or configured["port"])
    set_app_user_model_id()
    try:
        return run_application(port)
    except Exception as exc:
        show_error(f"应用启动失败：{exc}")
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())