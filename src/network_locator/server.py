"""Local loopback HTTP server for the Network Locator Windows app."""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from . import APP_ID, APP_NAME, __version__
from .core import (
    ScanManager,
    collect_neighbor_devices,
    max_configured_hosts,
    network_target_payload,
    normalize_mac_suffixes,
    normalize_network_ids,
    probe_network_devices,
    select_network_targets,
)
from .win_network import discover_networks

DEFAULT_PORT = 18743
MAX_BODY_BYTES = 8 * 1024
LOGGER = logging.getLogger("network_locator.server")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
TEXT_SUFFIXES = {".html", ".js", ".css", ".svg", ".json", ".txt"}


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(str(sys._MEIPASS))
    return Path(__file__).resolve().parents[1]


FRONTEND_DIR = resource_root() / "frontend"


def route_parts(path: str) -> list[str]:
    raw = str(path or "").split("?", 1)[0].split("#", 1)[0]
    parts = [unquote(part) for part in raw.split("/") if part]
    while APP_ID in parts:
        index = parts.index(APP_ID)
        del parts[: index + 1]
    if parts and parts[0] == "api":
        parts = parts[1:]
    return parts


def health_payload() -> dict:
    return {
        "ok": True,
        "status": "ok",
        "app": APP_ID,
        "name": APP_NAME,
        "version": __version__,
        "python": sys.version.split()[0],
        "max_hosts": max_configured_hosts(),
    }


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_network_selection(body: Mapping[str, Any]) -> list[str] | None:
    try:
        return normalize_network_ids(body.get("network_ids"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc


def start_scan_result(
    manager: Any,
    suffixes: list[str],
    network_ids: list[str] | None = None,
) -> tuple[int, dict]:
    started = manager.start(suffixes, network_ids)
    actual = manager.status()
    if not started:
        matches_request = getattr(manager, "matches_request", None)
        same_request = actual.get("suffix") == suffixes[0]
        if callable(matches_request):
            same_request = bool(matches_request(suffixes, network_ids))
        if not same_request:
            raise ApiError(
                409,
                "another scan with different MAC suffixes or network selection "
                "is already running",
            )
    return (
        202 if started else 200,
        {
            "ok": True,
            "app": APP_ID,
            "started": started,
            "running": bool(actual.get("running")),
            "status": actual.get("status"),
            "job_id": actual.get("job_id", ""),
            "suffix": actual.get("suffix", ""),
            "suffixes": actual.get("suffixes", []),
            "selected_network_ids": actual.get("selected_network_ids", []),
            "message": "scan started" if started else "scan already running",
        },
    )


class NetworkLocatorHandler(BaseHTTPRequestHandler):
    server_version = f"NetworkLocator/{__version__}"
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _manager(self) -> ScanManager:
        manager = getattr(self.server, "scan_manager", None)
        if manager is None:
            raise ApiError(503, "scan manager is unavailable")
        return manager

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        head_only: bool = False,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        data: bytes,
        *,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _serve_static(self, path: str, *, head_only: bool = False) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        root = FRONTEND_DIR.resolve()
        requested = (root / relative).resolve()
        try:
            requested.relative_to(root)
        except ValueError:
            raise ApiError(404, "not found") from None
        if not requested.is_file():
            raise ApiError(404, "not found")
        suffix = requested.suffix.lower()
        data = requested.read_bytes()
        if suffix in TEXT_SUFFIXES:
            text = data.decode("utf-8")
            text = text.replace("__APP_VERSION__", __version__)
            data = text.encode("utf-8")
        self._send_bytes(
            200,
            CONTENT_TYPES.get(suffix, "application/octet-stream"),
            data,
            head_only=head_only,
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(413, "request body is too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"invalid JSON body: {exc}") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "JSON body must be an object")
        return value

    def _dispatch(self, method: str) -> None:
        head_only = method == "HEAD"
        try:
            parts = route_parts(self.path)
            if not parts:
                if method in {"GET", "HEAD"}:
                    self._serve_static("/", head_only=head_only)
                    return
                raise ApiError(404, "not found")
            if parts == ["info"]:
                self._send_json(
                    200,
                    {
                        **health_payload(),
                        "capabilities": [
                            "health",
                            "networks",
                            "devices",
                            "devices/scan",
                            "scan/start",
                            "scan/status",
                            "scan/stop",
                        ],
                    },
                    head_only=head_only,
                )
                return
            if parts == ["health"]:
                if method not in {"GET", "HEAD"}:
                    raise ApiError(405, "method not allowed")
                self._send_json(200, health_payload(), head_only=head_only)
                return
            if parts == ["networks"]:
                if method not in {"GET", "HEAD"}:
                    raise ApiError(405, "method not allowed")
                targets, warnings = discover_networks()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "app": APP_ID,
                        "version": __version__,
                        "max_hosts": max_configured_hosts(),
                        "networks": [
                            network_target_payload(target) for target in targets
                        ],
                        "warnings": warnings,
                    },
                    head_only=head_only,
                )
                return
            if parts == ["devices"]:
                if method not in {"GET", "HEAD"}:
                    raise ApiError(405, "method not allowed")
                targets, warnings = discover_networks()
                devices, neighbor_warnings = collect_neighbor_devices(targets)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "app": APP_ID,
                        "version": __version__,
                        "devices": devices,
                        "warnings": warnings + neighbor_warnings,
                    },
                    head_only=head_only,
                )
                return
            if parts == ["devices", "scan"]:
                if method != "POST":
                    raise ApiError(405, "method not allowed")
                body = self._read_json_body()
                network_ids = parse_network_selection(body)
                inventory_lock = getattr(self.server, "device_scan_lock", None)
                if inventory_lock is None:
                    inventory_lock = threading.Lock()
                if not inventory_lock.acquire(blocking=False):
                    raise ApiError(409, "device inventory scan is already running")
                try:
                    if self._manager().status().get("running"):
                        raise ApiError(409, "another scan is already running")
                    targets, warnings = discover_networks()
                    targets, missing = select_network_targets(targets, network_ids)
                    if missing:
                        warnings = warnings + [
                            "所选网段当前不可用：" + "、".join(missing)
                        ]
                    if network_ids is not None and not targets:
                        raise ApiError(
                            409,
                            "所选网段当前不可用，请刷新网段后重试",
                        )
                    devices, probe_warnings = probe_network_devices(targets)
                finally:
                    inventory_lock.release()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "app": APP_ID,
                        "version": __version__,
                        "devices": devices,
                        "warnings": warnings + probe_warnings,
                    },
                )
                return
            if parts == ["scan", "status"]:
                if method not in {"GET", "HEAD"}:
                    raise ApiError(405, "method not allowed")
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "app": APP_ID,
                        "version": __version__,
                        **self._manager().status(),
                    },
                    head_only=head_only,
                )
                return
            if parts == ["scan", "start"]:
                if method != "POST":
                    raise ApiError(405, "method not allowed")
                body = self._read_json_body()
                raw_suffixes = body.get("suffixes")
                if raw_suffixes is None:
                    legacy_suffix = body.get("suffix")
                    raw_suffixes = [legacy_suffix] if legacy_suffix else []
                try:
                    suffixes = normalize_mac_suffixes(raw_suffixes)
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from exc
                network_ids = parse_network_selection(body)
                inventory_lock = getattr(self.server, "device_scan_lock", None)
                if inventory_lock is None:
                    inventory_lock = threading.Lock()
                if not inventory_lock.acquire(blocking=False):
                    raise ApiError(409, "device inventory scan is already running")
                try:
                    status, payload = start_scan_result(
                        self._manager(),
                        suffixes,
                        network_ids,
                    )
                finally:
                    inventory_lock.release()
                self._send_json(status, payload)
                return
            if parts == ["scan", "stop"]:
                if method != "POST":
                    raise ApiError(405, "method not allowed")
                self._read_json_body()
                stopping = self._manager().stop()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "app": APP_ID,
                        "stopping": stopping,
                        "message": "stopping" if stopping else "scan is not running",
                    },
                )
                return
            if method in {"GET", "HEAD"}:
                self._serve_static(self.path.split("?", 1)[0], head_only=head_only)
                return
            raise ApiError(404, "not found")
        except ApiError as exc:
            self._send_json(
                exc.status,
                {"ok": False, "app": APP_ID, "error": str(exc)},
                head_only=head_only,
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            LOGGER.exception("unhandled request failure for %s", self.path)
            self._send_json(
                500,
                {"ok": False, "app": APP_ID, "error": f"internal server error: {exc}"},
                head_only=head_only,
            )


class LocalAppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, NetworkLocatorHandler)
        self.scan_manager = ScanManager()
        self.device_scan_lock = threading.Lock()


def start_server(
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
) -> tuple[LocalAppServer, threading.Thread, str]:
    server = LocalAppServer((host, int(port)))
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="network-locator-http",
        daemon=True,
    )
    thread.start()
    actual_host, actual_port = server.server_address[:2]
    return server, thread, f"http://{actual_host}:{actual_port}/"


def shutdown_server(
    server: LocalAppServer,
    thread: threading.Thread,
) -> None:
    LOGGER.info("stopping local server")
    server.scan_manager.stop()
    server.scan_manager.wait(2.0)
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()