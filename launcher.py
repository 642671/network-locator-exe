"""Windows executable entry point for Network Locator."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import multiprocessing
import os
import subprocess
import sys
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


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "NetworkLocator"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def run_selftest(result_file: str) -> int:
    result_path = Path(result_file).resolve()
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


def run_application(port: int) -> int:
    config = load_config()
    browser = find_browser(
        os.environ.get("NETWORK_LOCATOR_BROWSER") or str(config.get("browser") or "")
    )
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
    try:
        logging.info("serving url=%s", url)
        if browser:
            browser_process = launch_app_window(url, browser)
            wait_for_browser(browser_process)
        else:
            logging.warning("no Edge/Chrome found; falling back to default browser")
            webbrowser.open(url)
            show_info(
                "未找到 Microsoft Edge 或 Google Chrome，已使用默认浏览器打开。\n\n"
                "关闭此提示即可退出 Network Locator。"
            )
    finally:
        if browser_process is not None and browser_process.poll() is None:
            try:
                browser_process.terminate()
                browser_process.wait(timeout=5)
            except Exception:
                pass
        shutdown_server(server, thread)
    return 0


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
    try:
        return run_application(port)
    except Exception as exc:
        show_error(f"应用启动失败：{exc}")
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())