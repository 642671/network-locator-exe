"""Local loopback HTTP server for the Network Locator Windows app."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from . import APP_ID, APP_NAME, __version__
from .core import (
    CommandError,
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
from .win_network import ping_statistics, read_network_interfaces
from .scanner import InventoryScanner, ScanScopeError, parse_custom_ranges
from .store import NetworkStore

# 0 lets the operating system assign a free TCP port on first bind. The
# launcher remembers the assigned port so the WebView2 origin stays stable.
DEFAULT_PORT = 0
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
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(str(name), str(value))
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _store(self) -> NetworkStore:
        store = getattr(self.server, "store", None)
        if store is None:
            raise ApiError(503, "device store is unavailable")
        return store

    def _inventory_scanner(self) -> InventoryScanner:
        scanner = getattr(self.server, "inventory_scanner", None)
        if scanner is None:
            raise ApiError(503, "inventory scanner is unavailable")
        return scanner

    def _network_snapshot(self, force: bool = False) -> dict:
        server = self.server
        cache = getattr(server, "network_cache", None)
        if not force and cache is not None and time.monotonic() - cache["at"] < 12:
            return cache
        warnings: list[str] = []
        try:
            interfaces = read_network_interfaces()
        except Exception as exc:  # display a useful warning instead of hiding the app
            interfaces = []
            warnings.append(f"网卡详情读取失败：{exc}")
        try:
            targets, discovery_warnings = discover_networks()
            warnings.extend(discovery_warnings)
        except Exception as exc:
            targets = []
            warnings.append(f"网段发现失败：{exc}")
        known = {str(item.get("interface_id") or "") for item in interfaces}
        for target in targets:
            interface_id = target.interface_id or f"{target.interface}|{target.interface_index}"
            if interface_id in known:
                continue
            interfaces.append(
                {
                    "interface_id": interface_id,
                    "interface_index": target.interface_index,
                    "alias": target.interface,
                    "description": "",
                    "local_ip": target.local_ip,
                    "prefix_length": target.prefix_length,
                    "network": target.network,
                    "broadcast": target.broadcast,
                    "scan_range": target.scan_range,
                    "segment_id": f"{interface_id}|{target.scan_range}",
                    "gateway": target.gateway,
                    "dns_servers": [],
                    "local_mac": target.local_mac,
                    "host_count": target.host_count,
                    "is_virtual": False,
                    "is_up": True,
                }
            )
            known.add(interface_id)
        segments = [network_target_payload(target) for target in targets]
        try:
            self._store().sync_network_snapshot(interfaces, segments)
        except Exception:
            LOGGER.exception("failed to persist network snapshot")
        snapshot = {
            "at": time.monotonic(),
            "interfaces": interfaces,
            "segments": segments,
            "warnings": warnings,
        }
        with server.network_lock:
            server.network_cache = snapshot
        return snapshot

    def _send_csv(self, body: str, filename: str, *, head_only: bool = False) -> None:
        safe_name = Path(str(filename or "export.csv")).name.replace('"', "")
        self._send_bytes(
            200,
            "text/csv; charset=utf-8",
            body.encode("utf-8"),
            head_only=head_only,
            extra_headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
            },
        )

    def _dispatch_v2(self, method: str, parts: list[str], *, head_only: bool = False) -> None:
        store = self._store()
        scanner = self._inventory_scanner()
        if not parts:
            raise ApiError(404, "not found")
        if parts == ["bootstrap"]:
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            snapshot = self._network_snapshot()
            devices = store.list_devices()
            counts = {"total": len(devices), "online": 0, "offline": 0, "unknown": 0, "favorites": 0}
            for device in devices:
                status = str(device.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
                if device.get("favorite"):
                    counts["favorites"] += 1
            history = store.list_scan_tasks(limit=30)
            self._send_json(
                200,
                {
                    "ok": True,
                    "app": APP_ID,
                    "name": APP_NAME,
                    "version": __version__,
                    "interfaces": snapshot["interfaces"],
                    "segments": snapshot["segments"],
                    "warnings": snapshot["warnings"],
                    "devices": devices,
                    "history": history,
                    "settings": store.get_settings(),
                    "summary": {
                        "devices": counts,
                        "last_scan": history[0] if history else None,
                    },
                    "scan": scanner.status(),
                },
                head_only=head_only,
            )
            return
        if parts == ["interfaces"]:
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            query = parse_qs(urlsplit(self.path).query)
            force = str((query.get("refresh") or ["0"])[0]).lower() in {"1", "true", "yes"}
            snapshot = self._network_snapshot(force=force)
            self._send_json(
                200,
                {
                    "ok": True,
                    "app": APP_ID,
                    "interfaces": snapshot["interfaces"],
                    "segments": snapshot["segments"],
                    "warnings": snapshot["warnings"],
                },
                head_only=head_only,
            )
            return
        if parts == ["devices"]:
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            devices = store.list_devices()
            self._send_json(200, {"ok": True, "devices": devices}, head_only=head_only)
            return
        if len(parts) == 2 and parts[0] == "devices":
            device_id = parts[1]
            if method in {"GET", "HEAD"}:
                device = store.get_device(device_id)
                if device is None:
                    raise ApiError(404, "设备不存在")
                self._send_json(200, {"ok": True, "device": device}, head_only=head_only)
                return
            if method == "PATCH":
                body = self._read_json_body()
                device = store.update_device(device_id, body)
                if device is None:
                    raise ApiError(404, "设备不存在")
                self._send_json(200, {"ok": True, "device": device})
                return
            raise ApiError(405, "method not allowed")
        if parts == ["lookup"]:
            if method == "POST":
                body = self._read_json_body()
                value = body.get("query", body.get("q", body.get("tokens", "")))
            elif method in {"GET", "HEAD"}:
                query = parse_qs(urlsplit(self.path).query)
                value = (query.get("q") or query.get("query") or [""])[0]
            else:
                raise ApiError(405, "method not allowed")
            try:
                result = store.lookup_devices(value)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            self._send_json(200, {"ok": True, **result}, head_only=head_only)
            return
        if parts == ["analysis"]:
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            snapshot = self._network_snapshot()
            devices = store.list_devices()
            distribution: dict[str, int] = {}
            for device in devices:
                key = str(device.get("current_scan_range") or "未知网段")
                distribution[key] = distribution.get(key, 0) + 1
            self._send_json(
                200,
                {
                    "ok": True,
                    "interfaces": snapshot["interfaces"],
                    "segments": snapshot["segments"],
                    "distribution": [
                        {"segment": key, "count": value}
                        for key, value in sorted(distribution.items(), key=lambda item: (-item[1], item[0]))
                    ],
                    "warnings": snapshot["warnings"],
                },
                head_only=head_only,
            )
            return
        if parts == ["analysis", "ping"]:
            if method != "POST":
                raise ApiError(405, "method not allowed")
            body = self._read_json_body()
            address = str(body.get("address") or body.get("ip") or "").strip()
            if not address:
                snapshot = self._network_snapshot()
                default_id = str(store.get_settings().get("default_interface_id") or "")
                chosen = next(
                    (item for item in snapshot["interfaces"] if item.get("interface_id") == default_id),
                    None,
                ) or next((item for item in snapshot["interfaces"] if item.get("gateway")), None)
                address = str((chosen or {}).get("gateway") or "")
            if not address:
                raise ApiError(400, "请指定要探测的 IPv4 地址")
            try:
                count = max(1, min(10, int(body.get("count") or 4)))
                timeout_ms = max(200, min(5000, int(body.get("timeout_ms") or store.get_settings().get("scan_timeout_ms") or 1000)))
                result = ping_statistics(address, count, timeout_ms=timeout_ms)
            except (CommandError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
            self._send_json(200, {"ok": True, "result": result})
            return
        if parts == ["scan", "start"]:
            if method != "POST":
                raise ApiError(405, "method not allowed")
            body = self._read_json_body()
            try:
                status = scanner.start(
                    body.get("network_ids"),
                    body.get("custom_ranges"),
                    trigger=str(body.get("trigger") or "manual")[:32],
                )
            except ScanScopeError as exc:
                message = str(exc)
                raise ApiError(409 if "正在运行" in message else 400, message) from exc
            self._send_json(202, {"ok": True, "app": APP_ID, "scan": status})
            return
        if len(parts) == 2 and parts[0] == "scan" and parts[1] in {"status", "pause", "resume", "stop"}:
            action = parts[1]
            if action == "status":
                if method not in {"GET", "HEAD"}:
                    raise ApiError(405, "method not allowed")
                self._send_json(200, {"ok": True, "app": APP_ID, "scan": scanner.status()}, head_only=head_only)
                return
            if method != "POST":
                raise ApiError(405, "method not allowed")
            self._read_json_body()
            changed = getattr(scanner, action)()
            self._send_json(
                200,
                {
                    "ok": True,
                    "changed": changed,
                    "scan": scanner.status(),
                    "message": "ok" if changed else "no active scan state changed",
                },
            )
            return
        if parts == ["history"]:
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            self._send_json(200, {"ok": True, "tasks": store.list_scan_tasks(200)}, head_only=head_only)
            return
        if len(parts) == 2 and parts[0] == "history":
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            task = store.get_scan_task(parts[1])
            if task is None:
                raise ApiError(404, "扫描记录不存在")
            self._send_json(200, {"ok": True, "task": task}, head_only=head_only)
            return
        if parts == ["settings"]:
            if method in {"GET", "HEAD"}:
                self._send_json(200, {"ok": True, "settings": store.get_settings()}, head_only=head_only)
                return
            if method == "PATCH":
                body = self._read_json_body()
                try:
                    settings = store.update_settings(body)
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from exc
                self._send_json(200, {"ok": True, "settings": settings})
                return
            raise ApiError(405, "method not allowed")
        if parts == ["export", "devices.csv"]:
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            query = parse_qs(urlsplit(self.path).query)
            raw_ids = (query.get("ids") or [""])[0]
            ids = [item for item in raw_ids.split(",") if item.strip()]
            body, filename = store.export_devices_csv(ids or None)
            self._send_csv(body, filename, head_only=head_only)
            return
        if len(parts) == 3 and parts[0] == "export" and parts[1] == "history":
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method not allowed")
            task_id = parts[2][:-4] if parts[2].endswith(".csv") else parts[2]
            try:
                body, filename = store.export_scan_csv(task_id)
            except ValueError as exc:
                raise ApiError(404, str(exc)) from exc
            self._send_csv(body, filename, head_only=head_only)
            return
        raise ApiError(404, "not found")

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
        self.send_header("Allow", "GET, HEAD, POST, PATCH, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

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
            if parts and parts[0] == "v2":
                self._dispatch_v2(method, parts[1:], head_only=head_only)
                return
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
                            "v2/bootstrap",
                            "v2/devices",
                            "v2/lookup",
                            "v2/analysis",
                            "v2/history",
                            "v2/settings",
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

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        store: NetworkStore | None = None,
        inventory_scanner: InventoryScanner | None = None,
        auto_schedule: bool = True,
    ) -> None:
        super().__init__(server_address, NetworkLocatorHandler)
        self.scan_manager = ScanManager()
        self.device_scan_lock = threading.Lock()
        self._owns_store = store is None
        self.store = store or NetworkStore()
        self.inventory_scanner = inventory_scanner or InventoryScanner(
            self.store,
            auto_schedule=auto_schedule,
        )
        self.network_cache: dict[str, Any] | None = None
        self.network_lock = threading.Lock()


def start_server(
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    *,
    store: NetworkStore | None = None,
    inventory_scanner: InventoryScanner | None = None,
    auto_schedule: bool = True,
) -> tuple[LocalAppServer, threading.Thread, str]:
    server = LocalAppServer(
        (host, int(port)),
        store=store,
        inventory_scanner=inventory_scanner,
        auto_schedule=auto_schedule,
    )
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
    try:
        server.inventory_scanner.shutdown()
    except Exception:
        LOGGER.exception("failed to stop inventory scanner")
    server.scan_manager.stop()
    server.scan_manager.wait(2.0)
    server.shutdown()
    thread.join(timeout=3)
    LOGGER.info("http thread stopped")
    server.server_close()
    LOGGER.info("http server closed")
    if getattr(server, "_owns_store", False):
        try:
            server.store.close()
        except Exception:
            LOGGER.exception("failed to close device store")
        else:
            LOGGER.info("device store closed")
