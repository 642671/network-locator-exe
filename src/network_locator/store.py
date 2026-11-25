"""SQLite persistence for device inventory, scan history, and settings.

The Windows application used to keep only a handful of MAC aliases in browser
storage.  Version 2 keeps an append-only scan history and a durable device
archive instead.  A single connection guarded by an RLock keeps the module
portable (including ``:memory:`` databases used by tests) while still allowing
the local HTTP server's worker threads to call it safely.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .core import normalize_device_mac, normalize_mac
from .oui import describe_mac

SCHEMA_VERSION = 2
MAC_QUERY_PATTERN = re.compile(r"^[0-9A-F]{4,12}$")
MAX_DEVICE_NAME = 80
MAX_DEVICE_NOTE = 500

DEFAULT_SETTINGS: dict[str, Any] = {
    "scan_timeout_ms": 900,
    "scan_concurrency": 64,
    "default_interface_id": "",
    "auto_scan_enabled": False,
    "auto_scan_interval_minutes": 30,
    "offline_after_minutes": 30,
    "csv_bom": True,
    "csv_include_header": True,
    "csv_include_notes": True,
    "theme": "light",
}

SETTING_RULES: dict[str, tuple[type, int, int] | type] = {
    "scan_timeout_ms": (int, 200, 5000),
    "scan_concurrency": (int, 1, 128),
    "default_interface_id": str,
    "auto_scan_enabled": bool,
    "auto_scan_interval_minutes": (int, 5, 1440),
    "offline_after_minutes": (int, 1, 1440),
    "csv_bom": bool,
    "csv_include_header": bool,
    "csv_include_notes": bool,
    "theme": str,
}

STATE_PRIORITY = {
    "OFFLINE": 0,
    "STALE": 1,
    "PROBE": 2,
    "DELAY": 3,
    "REACHABLE": 4,
    "LOCAL": 5,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_data_dir() -> Path:
    override = os.environ.get("NETWORK_LOCATOR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NetworkLocator" / "data"
    return Path.home() / ".network-locator" / "data"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_online_state(value: str | None) -> bool:
    return str(value or "").upper() in {"LOCAL", "REACHABLE"}


def _normalize_observed_mac(value: Any) -> tuple[bool, str]:
    raw = str(value or "").strip()
    if not raw:
        return True, ""
    normalized = normalize_device_mac(raw)
    return normalized is not None, normalized or ""


def _normalize_observed_ip(value: Any) -> str | None:
    try:
        address = ipaddress.IPv4Address(str(value or "").strip())
    except ValueError:
        return None
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or int(address) == 0xFFFFFFFF
    ):
        return None
    return str(address)


def _device_status(row: Mapping[str, Any], latest_scan_id: str, offline_after_minutes: int) -> str:
    raw_state = str(row.get("last_state") or "").upper()
    seen_in_latest = bool(latest_scan_id) and row.get("last_seen_scan_id") == latest_scan_id
    if seen_in_latest:
        if raw_state in {"LOCAL", "REACHABLE"}:
            return "online"
        return "unknown"
    if raw_state == "OFFLINE":
        return "offline"
    last_seen = parse_time(str(row.get("last_seen_at") or ""))
    if last_seen is None:
        return "offline"
    age = datetime.now(timezone.utc) - last_seen
    if age <= timedelta(minutes=max(1, offline_after_minutes)):
        return "unknown"
    return "offline"


def _status_label(status: str) -> str:
    return {"online": "在线", "offline": "离线", "unknown": "未知"}.get(status, "未知")


def _effective_name(row: Mapping[str, Any]) -> str:
    manual = str(row.get("display_name") or "").strip()
    if manual:
        return manual
    vendor = str(row.get("vendor") or "").strip()
    mac = str(row.get("primary_mac") or "").strip()
    if vendor:
        return f"{vendor} 设备"
    if mac:
        return "设备 " + mac.replace("-", "")[-4:]
    ip = str(row.get("current_ip") or "").strip()
    return f"设备 {ip}" if ip else "未命名设备"


def sanitize_csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if re.match(r"^[=+@]", text) or re.match(r"^-(?!\d)", text):
        return "'" + text
    return text


def normalize_mac_query(value: str) -> tuple[str, str] | None:
    compact = re.sub(r"[-:\s]", "", str(value or "")).upper()
    if not MAC_QUERY_PATTERN.fullmatch(compact):
        return None
    if len(compact) == 4:
        return "suffix", compact
    if len(compact) == 6:
        return "prefix", compact
    if len(compact) == 12:
        return "full", compact
    return None


def split_mac_tokens(value: object) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = []
    tokens: list[str] = []
    for part in raw:
        for token in re.split(r"[\s,;，；]+", part):
            item = token.strip()
            if item and item not in tokens:
                tokens.append(item)
    return tokens


class NetworkStore:
    """Small, dependency-free repository for the Network Locator data model."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.data_dir = default_data_dir()
        if path is None:
            path = self.data_dir / "network-locator.db"
        self.path = ":memory:" if str(path) == ":memory:" else str(Path(path))
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=15,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 8000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()
        self.mark_interrupted_tasks()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    identity_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    vendor TEXT NOT NULL DEFAULT '',
                    primary_mac TEXT NOT NULL DEFAULT '',
                    is_random_mac INTEGER NOT NULL DEFAULT 0,
                    current_ip TEXT NOT NULL DEFAULT '',
                    current_interface TEXT NOT NULL DEFAULT '',
                    current_scan_range TEXT NOT NULL DEFAULT '',
                    last_state TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_seen_scan_id TEXT NOT NULL DEFAULT '',
                    first_seen_task_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_devices_mac ON devices(primary_mac);
                CREATE INDEX IF NOT EXISTS idx_devices_current_ip ON devices(current_ip);
                CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS device_addresses (
                    address_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    ip TEXT NOT NULL,
                    interface TEXT NOT NULL DEFAULT '',
                    interface_id TEXT NOT NULL DEFAULT '',
                    scan_range TEXT NOT NULL DEFAULT '',
                    mac TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_seen_scan_id TEXT NOT NULL DEFAULT '',
                    is_current INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(device_id, ip, interface)
                );
                CREATE INDEX IF NOT EXISTS idx_addresses_device ON device_addresses(device_id, last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_addresses_ip ON device_addresses(ip);
                CREATE TABLE IF NOT EXISTS network_interfaces (
                    interface_id TEXT PRIMARY KEY,
                    interface_index INTEGER NOT NULL DEFAULT 0,
                    alias TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    local_ip TEXT NOT NULL DEFAULT '',
                    prefix_length INTEGER NOT NULL DEFAULT 0,
                    gateway TEXT NOT NULL DEFAULT '',
                    dns_servers TEXT NOT NULL DEFAULT '[]',
                    is_virtual INTEGER NOT NULL DEFAULT 0,
                    is_up INTEGER NOT NULL DEFAULT 1,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_segments (
                    segment_id TEXT PRIMARY KEY,
                    interface_id TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    network TEXT NOT NULL,
                    prefix_length INTEGER NOT NULL,
                    scan_range TEXT NOT NULL,
                    broadcast TEXT NOT NULL DEFAULT '',
                    gateway TEXT NOT NULL DEFAULT '',
                    host_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_tasks (
                    task_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'manual',
                    scope TEXT NOT NULL DEFAULT 'inventory',
                    network_ids TEXT NOT NULL DEFAULT '[]',
                    custom_ranges TEXT NOT NULL DEFAULT '[]',
                    total_hosts INTEGER NOT NULL DEFAULT 0,
                    probed_hosts INTEGER NOT NULL DEFAULT 0,
                    found_devices INTEGER NOT NULL DEFAULT 0,
                    new_devices INTEGER NOT NULL DEFAULT 0,
                    offline_devices INTEGER NOT NULL DEFAULT 0,
                    ip_changes INTEGER NOT NULL DEFAULT 0,
                    warnings TEXT NOT NULL DEFAULT '[]',
                    config TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_scan_tasks_started ON scan_tasks(started_at DESC);
                CREATE TABLE IF NOT EXISTS scan_results (
                    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES scan_tasks(task_id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    ip TEXT NOT NULL,
                    mac TEXT NOT NULL DEFAULT '',
                    interface TEXT NOT NULL DEFAULT '',
                    interface_id TEXT NOT NULL DEFAULT '',
                    scan_range TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    vendor TEXT NOT NULL DEFAULT '',
                    is_new INTEGER NOT NULL DEFAULT 0,
                    ip_changed INTEGER NOT NULL DEFAULT 0,
                    seen_at TEXT NOT NULL,
                    UNIQUE(task_id, device_id, ip, mac)
                );
                CREATE INDEX IF NOT EXISTS idx_scan_results_task ON scan_results(task_id, ip);
                CREATE TABLE IF NOT EXISTS device_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_device ON device_events(device_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            current = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                current_version = SCHEMA_VERSION
            else:
                try:
                    current_version = int(str(current["value"]))
                except (TypeError, ValueError):
                    current_version = 0
            if current_version < SCHEMA_VERSION:
                self._purge_invalid_device_macs()
                self._conn.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            now = utc_now()
            for key, value in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
                    (key, _json(value), now),
                )

    def _purge_invalid_device_macs(self) -> int:
        rows = self._conn.execute(
            "SELECT device_id, primary_mac FROM devices WHERE primary_mac <> ''"
        ).fetchall()
        invalid = [
            (str(row["device_id"]),)
            for row in rows
            if normalize_device_mac(str(row["primary_mac"])) is None
        ]
        if invalid:
            self._conn.executemany(
                "DELETE FROM devices WHERE device_id = ?",
                invalid,
            )
        return len(invalid)

    def mark_interrupted_tasks(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE scan_tasks
                SET status = 'interrupted', finished_at = ?, error = ?
                WHERE status IN ('running', 'paused', 'discovering')
                """,
                (utc_now(), "应用在扫描完成前退出"),
            )
            return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        values = dict(DEFAULT_SETTINGS)
        for row in rows:
            values[str(row["key"])] = _loads(str(row["value"]), row["value"])
        values["data_dir"] = str(self.data_dir)
        values["database_path"] = self.path
        values["schema_version"] = SCHEMA_VERSION
        return values

    def update_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for key, raw_value in payload.items():
            if key not in SETTING_RULES:
                continue
            rule = SETTING_RULES[key]
            if isinstance(rule, tuple):
                kind, minimum, maximum = rule
                try:
                    value = kind(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} 必须是整数") from exc
                value = max(minimum, min(maximum, value))
            elif rule is bool:
                value = bool(raw_value)
            else:
                value = str(raw_value or "").strip()
            if key == "theme" and value not in {"light", "dark"}:
                value = "light"
            if key == "default_interface_id" and len(value) > 256:
                value = value[:256]
            updates[key] = value
        if not updates:
            return self.get_settings()
        now = utc_now()
        with self._lock, self._conn:
            for key, value in updates.items():
                self._conn.execute(
                    """
                    INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, _json(value), now),
                )
        return self.get_settings()

    # ------------------------------------------------------------------
    # Network snapshot
    # ------------------------------------------------------------------
    def sync_network_snapshot(
        self,
        interfaces: Sequence[Mapping[str, Any]],
        segments: Sequence[Mapping[str, Any]],
    ) -> None:
        now = utc_now()
        with self._lock, self._conn:
            for item in interfaces:
                interface_id = str(item.get("interface_id") or "").strip()
                if not interface_id:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO network_interfaces(
                        interface_id, interface_index, alias, description, local_ip,
                        prefix_length, gateway, dns_servers, is_virtual, is_up, last_seen_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(interface_id) DO UPDATE SET
                        interface_index = excluded.interface_index,
                        alias = excluded.alias,
                        description = excluded.description,
                        local_ip = excluded.local_ip,
                        prefix_length = excluded.prefix_length,
                        gateway = excluded.gateway,
                        dns_servers = excluded.dns_servers,
                        is_virtual = excluded.is_virtual,
                        is_up = excluded.is_up,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        interface_id,
                        _as_int(item.get("interface_index")),
                        str(item.get("alias") or interface_id),
                        str(item.get("description") or ""),
                        str(item.get("local_ip") or ""),
                        _as_int(item.get("prefix_length")),
                        str(item.get("gateway") or ""),
                        _json(list(item.get("dns_servers") or [])),
                        1 if item.get("is_virtual") else 0,
                        0 if item.get("is_up") is False else 1,
                        now,
                    ),
                )
            for item in segments:
                segment_id = str(item.get("segment_id") or item.get("network_id") or "").strip()
                if not segment_id:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO network_segments(
                        segment_id, interface_id, interface, network, prefix_length,
                        scan_range, broadcast, gateway, host_count, last_seen_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(segment_id) DO UPDATE SET
                        interface_id = excluded.interface_id,
                        interface = excluded.interface,
                        network = excluded.network,
                        prefix_length = excluded.prefix_length,
                        scan_range = excluded.scan_range,
                        broadcast = excluded.broadcast,
                        gateway = excluded.gateway,
                        host_count = excluded.host_count,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        segment_id,
                        str(item.get("interface_id") or ""),
                        str(item.get("interface") or ""),
                        str(item.get("network") or ""),
                        _as_int(item.get("prefix_length")),
                        str(item.get("scan_range") or ""),
                        str(item.get("broadcast") or ""),
                        str(item.get("gateway") or ""),
                        _as_int(item.get("host_count")),
                        now,
                    ),
                )

    def list_interfaces(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM network_interfaces ORDER BY is_virtual, interface_index, alias"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["dns_servers"] = _loads(str(item.get("dns_servers") or "[]"), [])
            item["is_virtual"] = bool(item.get("is_virtual"))
            item["is_up"] = bool(item.get("is_up"))
            result.append(item)
        return result

    def list_segments(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM network_segments ORDER BY interface, network"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Scans
    # ------------------------------------------------------------------
    def create_scan_task(
        self,
        *,
        trigger: str = "manual",
        scope: str = "inventory",
        network_ids: Sequence[str] | None = None,
        custom_ranges: Sequence[str] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO scan_tasks(
                    task_id, started_at, status, trigger, scope, network_ids,
                    custom_ranges, config
                ) VALUES(?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    utc_now(),
                    str(trigger or "manual")[:32],
                    str(scope or "inventory")[:32],
                    _json(list(network_ids or [])),
                    _json(list(custom_ranges or [])),
                    _json(dict(config or {})),
                ),
            )
        return task_id

    def update_scan_progress(
        self,
        task_id: str,
        *,
        total_hosts: int | None = None,
        probed_hosts: int | None = None,
        status: str | None = None,
        warnings: Sequence[str] | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        if total_hosts is not None:
            fields.append("total_hosts = ?")
            values.append(max(0, int(total_hosts)))
        if probed_hosts is not None:
            fields.append("probed_hosts = ?")
            values.append(max(0, int(probed_hosts)))
        if status is not None:
            fields.append("status = ?")
            values.append(str(status))
        if warnings is not None:
            fields.append("warnings = ?")
            values.append(_json(list(warnings)))
        if not fields:
            return
        values.append(task_id)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE scan_tasks SET {', '.join(fields)} WHERE task_id = ?",
                values,
            )

    def ingest_scan_results(
        self,
        task_id: str,
        observed: Iterable[Mapping[str, Any]],
        *,
        warnings: Sequence[str] | None = None,
    ) -> dict[str, int]:
        results = self._dedupe_observed(observed)
        mac_ips: dict[str, set[str]] = {}
        for item in results:
            valid_mac, mac = _normalize_observed_mac(item.get("mac"))
            ip = _normalize_observed_ip(item.get("ip"))
            if not valid_mac or ip is None:
                continue
            if mac and ip:
                mac_ips.setdefault(mac, set()).add(ip)

        now = utc_now()
        assigned_ips_by_mac: dict[str, set[str]] = {}
        touched_devices: set[str] = set()
        new_count = 0
        ip_changes = 0
        with self._lock, self._conn:
            task = self._conn.execute(
                "SELECT * FROM scan_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ValueError("扫描任务不存在")
            started_at = str(task["started_at"])
            for item in results:
                valid_mac, mac = _normalize_observed_mac(item.get("mac"))
                ip = _normalize_observed_ip(item.get("ip"))
                if not valid_mac or ip is None:
                    continue
                state = str(item.get("state") or "").upper()
                interface = str(item.get("interface") or "")
                interface_id = str(item.get("interface_id") or "")
                scan_range = str(item.get("scan_range") or "")
                vendor = describe_mac(mac)[0] if mac else ""
                is_random = 1 if mac and describe_mac(mac)[1] else 0
                row, is_new = self._resolve_device(
                    mac=mac,
                    ip=ip,
                    interface=interface,
                    interface_id=interface_id,
                    scan_range=scan_range,
                    state=state,
                    vendor=vendor,
                    is_random=is_random,
                    task_id=task_id,
                    started_at=started_at,
                    conflict=bool(mac and len(mac_ips.get(mac, set())) > 1),
                    assigned=assigned_ips_by_mac,
                    now=now,
                )
                device_id = str(row["device_id"])
                touched_devices.add(device_id)
                previous_ip = str(row.get("current_ip") or "")
                previous_state = str(row.get("last_state") or "")
                changed = bool(previous_ip and previous_ip != ip)
                if is_new:
                    new_count += 1
                    self._event(device_id, task_id, "discovered", {"ip": ip, "mac": mac}, now)
                elif changed:
                    ip_changes += 1
                    self._event(
                        device_id,
                        task_id,
                        "ip_changed",
                        {"old_ip": previous_ip, "new_ip": ip, "mac": mac},
                        now,
                    )
                if _is_online_state(state) and not _is_online_state(previous_state):
                    self._event(device_id, task_id, "came_online", {"ip": ip, "state": state}, now)
                if mac:
                    assigned_ips_by_mac.setdefault(mac, set()).add(ip)

                self._conn.execute(
                    """
                    INSERT INTO scan_results(
                        task_id, device_id, ip, mac, interface, interface_id,
                        scan_range, state, vendor, is_new, ip_changed, seen_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, device_id, ip, mac) DO UPDATE SET
                        state = CASE
                            WHEN (CASE excluded.state
                                WHEN 'LOCAL' THEN 5 WHEN 'REACHABLE' THEN 4
                                WHEN 'DELAY' THEN 3 WHEN 'PROBE' THEN 2 WHEN 'STALE' THEN 1
                                ELSE 0 END) > (CASE scan_results.state
                                WHEN 'LOCAL' THEN 5 WHEN 'REACHABLE' THEN 4
                                WHEN 'DELAY' THEN 3 WHEN 'PROBE' THEN 2 WHEN 'STALE' THEN 1
                                ELSE 0 END)
                            THEN excluded.state ELSE scan_results.state END,
                        vendor = excluded.vendor,
                        seen_at = excluded.seen_at
                    """,
                    (
                        task_id,
                        device_id,
                        ip,
                        mac,
                        interface,
                        interface_id,
                        scan_range,
                        state,
                        vendor,
                        1 if is_new else 0,
                        1 if changed else 0,
                        now,
                    ),
                )
            if warnings is not None:
                self._conn.execute(
                    "UPDATE scan_tasks SET warnings = ? WHERE task_id = ?",
                    (_json(list(warnings)), task_id),
                )
        return {
            "devices": len(touched_devices),
            "new_devices": new_count,
            "ip_changes": ip_changes,
        }

    def _resolve_device(
        self,
        *,
        mac: str,
        ip: str,
        interface: str,
        interface_id: str,
        scan_range: str,
        state: str,
        vendor: str,
        is_random: int,
        task_id: str,
        started_at: str,
        conflict: bool,
        assigned: Mapping[str, set[str]],
        now: str,
    ) -> tuple[Mapping[str, Any], bool]:
        row: Mapping[str, Any] | None = None
        if mac:
            candidates = self._conn.execute(
                "SELECT * FROM devices WHERE primary_mac = ? ORDER BY last_seen_at DESC",
                (mac,),
            ).fetchall()
            for candidate in candidates:
                if str(candidate["current_ip"]) == ip:
                    row = candidate
                    break
            if row is None and len(candidates) == 1 and not conflict:
                candidate_ip = str(candidates[0]["current_ip"] or "")
                if candidate_ip not in assigned.get(mac, set()):
                    row = candidates[0]
            if row is None and candidates and not conflict:
                for candidate in candidates:
                    if str(candidate["device_id"]) not in self._task_device_ids(task_id):
                        row = candidate
                        break
        if row is None:
            row = self._conn.execute(
                """
                SELECT * FROM devices
                WHERE current_ip = ? AND (primary_mac = '' OR primary_mac = ?)
                ORDER BY CASE WHEN primary_mac = ? THEN 0 ELSE 1 END, last_seen_at DESC
                LIMIT 1
                """,
                (ip, mac, mac),
            ).fetchone()
        if row is not None and mac and str(row["primary_mac"] or "") and str(row["primary_mac"]) != mac:
            row = None

        if row is None:
            device_id = uuid.uuid4().hex
            identity_key = f"mac:{mac}" if mac and not conflict else f"ip:{ip}:{uuid.uuid4().hex[:8]}"
            self._conn.execute(
                """
                INSERT INTO devices(
                    device_id, identity_key, primary_mac, is_random_mac, current_ip,
                    current_interface, current_scan_range, last_state, first_seen_at,
                    last_seen_at, last_seen_scan_id, first_seen_task_id, created_at, updated_at,
                    vendor
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    identity_key,
                    mac,
                    is_random,
                    ip,
                    interface,
                    scan_range,
                    state,
                    now,
                    now,
                    task_id,
                    task_id,
                    now,
                    now,
                    vendor,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            is_new = True
        else:
            device_id = str(row["device_id"])
            identity_key = str(row["identity_key"])
            if mac and not str(row["primary_mac"] or ""):
                identity_key = f"mac:{mac}"
            self._conn.execute(
                """
                UPDATE devices SET
                    identity_key = ?,
                    primary_mac = CASE WHEN ? <> '' THEN ? ELSE primary_mac END,
                    vendor = CASE WHEN ? <> '' THEN ? ELSE vendor END,
                    is_random_mac = CASE WHEN ? <> '' THEN ? ELSE is_random_mac END,
                    current_ip = ?,
                    current_interface = ?,
                    current_scan_range = ?,
                    last_state = ?,
                    last_seen_at = ?,
                    last_seen_scan_id = ?,
                    updated_at = ?
                WHERE device_id = ?
                """,
                (
                    identity_key,
                    mac,
                    mac,
                    vendor,
                    vendor,
                    mac,
                    is_random,
                    ip,
                    interface,
                    scan_range,
                    state,
                    now,
                    task_id,
                    now,
                    device_id,
                ),
            )
            is_new = False

        self._conn.execute(
            "UPDATE device_addresses SET is_current = 0 WHERE device_id = ?",
            (device_id,),
        )
        existing_address = self._conn.execute(
            """
            SELECT * FROM device_addresses
            WHERE device_id = ? AND ip = ? AND interface = ?
            """,
            (device_id, ip, interface),
        ).fetchone()
        if existing_address is None:
            self._conn.execute(
                """
                INSERT INTO device_addresses(
                    device_id, ip, interface, interface_id, scan_range, mac,
                    first_seen_at, last_seen_at, last_seen_scan_id, is_current
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (device_id, ip, interface, interface_id, scan_range, mac, now, now, task_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE device_addresses SET
                    interface_id = ?, scan_range = ?, mac = CASE WHEN ? <> '' THEN ? ELSE mac END,
                    last_seen_at = ?, last_seen_scan_id = ?, is_current = 1
                WHERE address_id = ?
                """,
                (
                    interface_id,
                    scan_range,
                    mac,
                    mac,
                    now,
                    task_id,
                    existing_address["address_id"],
                ),
            )
        return dict(row), is_new

    def _task_device_ids(self, task_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT device_id FROM scan_results WHERE task_id = ?", (task_id,)
        ).fetchall()
        return {str(row["device_id"]) for row in rows}

    def _event(
        self,
        device_id: str,
        task_id: str,
        event_type: str,
        detail: Mapping[str, Any],
        created_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO device_events(device_id, task_id, event_type, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            (device_id, task_id, event_type, _json(dict(detail)), created_at),
        )

    def finish_scan_task(
        self,
        task_id: str,
        *,
        status: str,
        total_hosts: int,
        probed_hosts: int,
        warnings: Sequence[str] | None = None,
        error: str = "",
        mark_offline: bool = True,
    ) -> dict[str, int]:
        now = utc_now()
        with self._lock, self._conn:
            task = self._conn.execute(
                "SELECT * FROM scan_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ValueError("扫描任务不存在")
            counts = self._conn.execute(
                """
                SELECT COUNT(DISTINCT device_id) AS found,
                       COALESCE(SUM(ip_changed), 0) AS ip_changes,
                       COALESCE(SUM(is_new), 0) AS new_devices
                FROM scan_results WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            absent = []
            if status == "completed" and mark_offline:
                absent = self._conn.execute(
                    """
                    SELECT * FROM devices
                    WHERE last_seen_scan_id <> ?
                      AND first_seen_task_id <> ?
                      AND last_state IN ('LOCAL', 'REACHABLE')
                    """,
                    (task_id, task_id),
                ).fetchall()
            for row in absent:
                self._event(
                    str(row["device_id"]),
                    task_id,
                    "offline",
                    {"last_ip": str(row["current_ip"] or "")},
                    now,
                )
            if absent:
                self._conn.executemany(
                    "UPDATE devices SET last_state = 'OFFLINE', updated_at = ? WHERE device_id = ?",
                    [(now, str(row["device_id"])) for row in absent],
                )
            found = _as_int(counts["found"] if counts else 0)
            new_devices = _as_int(counts["new_devices"] if counts else 0)
            ip_changes = _as_int(counts["ip_changes"] if counts else 0)
            offline_devices = len(absent)
            self._conn.execute(
                """
                UPDATE scan_tasks SET
                    finished_at = ?, status = ?, total_hosts = ?, probed_hosts = ?,
                    found_devices = ?, new_devices = ?, offline_devices = ?,
                    ip_changes = ?, warnings = ?, error = ?
                WHERE task_id = ?
                """,
                (
                    now,
                    str(status),
                    max(0, int(total_hosts)),
                    max(0, int(probed_hosts)),
                    found,
                    new_devices,
                    offline_devices,
                    ip_changes,
                    _json(list(warnings or [])),
                    str(error or ""),
                    task_id,
                ),
            )
        return {
            "found_devices": found,
            "new_devices": new_devices,
            "offline_devices": offline_devices,
            "ip_changes": ip_changes,
        }

    @staticmethod
    def _dedupe_observed(observed: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for raw in observed:
            item = dict(raw)
            valid_mac, mac = _normalize_observed_mac(item.get("mac"))
            ip = _normalize_observed_ip(item.get("ip"))
            if not valid_mac or ip is None:
                continue
            key = (mac or "no-mac", ip, str(item.get("interface") or ""))
            item["mac"] = mac
            existing = unique.get(key)
            if existing is None:
                unique[key] = item
                continue
            if STATE_PRIORITY.get(str(item.get("state") or "").upper(), 0) > STATE_PRIORITY.get(
                str(existing.get("state") or "").upper(), 0
            ):
                unique[key] = item
        return sorted(
            unique.values(),
            key=lambda item: (
                str(item.get("interface") or ""),
                _ip_value(str(item.get("ip") or "")),
                str(item.get("mac") or ""),
            ),
        )

    def list_scan_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scan_tasks ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["network_ids"] = _loads(str(item.get("network_ids") or "[]"), [])
            item["custom_ranges"] = _loads(str(item.get("custom_ranges") or "[]"), [])
            item["warnings"] = _loads(str(item.get("warnings") or "[]"), [])
            item["config"] = _loads(str(item.get("config") or "{}"), {})
            item["progress_percent"] = (
                100
                if item["status"] == "completed"
                else (0 if not item["total_hosts"] else int(item["probed_hosts"] * 100 / item["total_hosts"]))
            )
            tasks.append(item)
        return tasks

    def get_scan_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scan_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            results = self._conn.execute(
                """
                SELECT sr.*, d.display_name, d.note, d.favorite,
                       d.last_seen_at AS device_last_seen_at
                FROM scan_results sr
                JOIN devices d ON d.device_id = sr.device_id
                WHERE sr.task_id = ?
                ORDER BY sr.ip, sr.mac
                """,
                (task_id,),
            ).fetchall()
        item = dict(row)
        item["network_ids"] = _loads(str(item.get("network_ids") or "[]"), [])
        item["custom_ranges"] = _loads(str(item.get("custom_ranges") or "[]"), [])
        item["warnings"] = _loads(str(item.get("warnings") or "[]"), [])
        item["config"] = _loads(str(item.get("config") or "{}"), {})
        item["results"] = [dict(entry) for entry in results]
        return item

    # ------------------------------------------------------------------
    # Device inventory
    # ------------------------------------------------------------------
    def list_devices(self) -> list[dict[str, Any]]:
        settings = self.get_settings()
        offline_after = _as_int(settings.get("offline_after_minutes"), 30)
        with self._lock:
            latest = self._conn.execute(
                "SELECT task_id FROM scan_tasks WHERE status = 'completed' ORDER BY finished_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            latest_scan_id = str(latest["task_id"]) if latest else ""
            rows = self._conn.execute(
                """
                SELECT d.*,
                       (SELECT COUNT(*) FROM device_addresses a WHERE a.device_id = d.device_id) AS address_count
                FROM devices d
                ORDER BY d.last_seen_at DESC, d.current_ip
                """
            ).fetchall()
        devices: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            status = _device_status(item, latest_scan_id, offline_after)
            item["favorite"] = bool(item.get("favorite"))
            item["is_random_mac"] = bool(item.get("is_random_mac"))
            item["status"] = status
            item["status_label"] = _status_label(status)
            item["effective_name"] = _effective_name(item)
            item["address_count"] = _as_int(item.get("address_count"))
            devices.append(item)
        return devices

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        settings = self.get_settings()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                return None
            addresses = self._conn.execute(
                "SELECT * FROM device_addresses WHERE device_id = ? ORDER BY is_current DESC, last_seen_at DESC",
                (device_id,),
            ).fetchall()
            events = self._conn.execute(
                "SELECT * FROM device_events WHERE device_id = ? ORDER BY created_at DESC LIMIT 100",
                (device_id,),
            ).fetchall()
            results = self._conn.execute(
                """
                SELECT sr.*, st.started_at AS scan_started_at
                FROM scan_results sr JOIN scan_tasks st ON st.task_id = sr.task_id
                WHERE sr.device_id = ? ORDER BY sr.seen_at DESC LIMIT 50
                """,
                (device_id,),
            ).fetchall()
            latest = self._conn.execute(
                "SELECT task_id FROM scan_tasks WHERE status = 'completed' ORDER BY finished_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        latest_scan_id = str(latest["task_id"]) if latest else ""
        item = dict(row)
        item["favorite"] = bool(item.get("favorite"))
        item["is_random_mac"] = bool(item.get("is_random_mac"))
        status = _device_status(item, latest_scan_id, _as_int(settings.get("offline_after_minutes"), 30))
        item["status"] = status
        item["status_label"] = _status_label(status)
        item["effective_name"] = _effective_name(item)
        item["addresses"] = [dict(entry) for entry in addresses]
        item["events"] = []
        for event in events:
            entry = dict(event)
            entry["detail"] = _loads(str(entry.get("detail") or "{}"), {})
            item["events"].append(entry)
        item["recent_results"] = [dict(entry) for entry in results]
        return item

    def update_device(self, device_id: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                return None
            display_name = str(payload.get("display_name", row["display_name"]) or "").strip()[:MAX_DEVICE_NAME]
            note = str(payload.get("note", row["note"]) or "").strip()[:MAX_DEVICE_NOTE]
            favorite = 1 if bool(payload.get("favorite", row["favorite"])) else 0
            self._conn.execute(
                "UPDATE devices SET display_name = ?, note = ?, favorite = ?, updated_at = ? WHERE device_id = ?",
                (display_name, note, favorite, utc_now(), device_id),
            )
        return self.get_device(device_id)

    def lookup_devices(self, value: object) -> dict[str, Any]:
        tokens = split_mac_tokens(value)
        if not tokens:
            raise ValueError("请输入至少一个 MAC 地址、后缀或前缀")
        if len(tokens) > 64:
            raise ValueError("一次最多查询 64 个 MAC")
        devices = self.list_devices()
        batches: list[dict[str, Any]] = []
        for token in tokens:
            parsed = normalize_mac_query(token)
            if parsed is None:
                batches.append(
                    {
                        "query": token,
                        "kind": "invalid",
                        "normalized": "",
                        "vendor": "无法识别",
                        "matches": [],
                        "error": "支持 4 位后缀、6 位前缀或完整 12 位 MAC",
                    }
                )
                continue
            kind, compact = parsed
            matches: list[dict[str, Any]] = []
            for device in devices:
                mac = normalize_mac(str(device.get("primary_mac") or ""))
                compact_mac = (mac or "").replace("-", "")
                if not compact_mac:
                    continue
                matched = {
                    "full": compact_mac == compact,
                    "prefix": compact_mac.startswith(compact),
                    "suffix": compact_mac.endswith(compact),
                }[kind]
                if matched:
                    matches.append(device)
            vendor = describe_mac(compact)
            batches.append(
                {
                    "query": token,
                    "kind": kind,
                    "normalized": compact,
                    "vendor": vendor[0],
                    "is_random": vendor[1],
                    "matches": matches,
                    "error": "",
                }
            )
        return {"queries": batches, "query_count": len(tokens)}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_devices_csv(self, device_ids: Sequence[str] | None = None) -> tuple[str, str]:
        settings = self.get_settings()
        devices = self.list_devices()
        if device_ids:
            wanted = set(device_ids)
            devices = [item for item in devices if str(item.get("device_id")) in wanted]
        include_notes = bool(settings.get("csv_include_notes", True))
        columns = [
            ("设备名称", lambda item: item.get("effective_name", "")),
            ("状态", lambda item: item.get("status_label", "")),
            ("IP 地址", lambda item: item.get("current_ip", "")),
            ("MAC 地址", lambda item: item.get("primary_mac", "")),
            ("厂商", lambda item: item.get("vendor", "")),
            ("网卡", lambda item: item.get("current_interface", "")),
            ("网段", lambda item: item.get("current_scan_range", "")),
            ("地址记录数", lambda item: item.get("address_count", 0)),
            ("首次发现", lambda item: item.get("first_seen_at", "")),
            ("最后发现", lambda item: item.get("last_seen_at", "")),
            ("收藏", lambda item: "是" if item.get("favorite") else "否"),
        ]
        if include_notes:
            columns.append(("备注", lambda item: item.get("note", "")))
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        if settings.get("csv_include_header", True):
            writer.writerow([label for label, _ in columns])
        for item in devices:
            writer.writerow([sanitize_csv_cell(getter(item)) for _, getter in columns])
        body = buffer.getvalue()
        if settings.get("csv_bom", True):
            body = "\ufeff" + body
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return body, f"network-locator-devices-{stamp}.csv"

    def export_scan_csv(self, task_id: str) -> tuple[str, str]:
        task = self.get_scan_task(task_id)
        if task is None:
            raise ValueError("扫描任务不存在")
        settings = self.get_settings()
        columns = ["IP 地址", "MAC 地址", "设备名称", "厂商", "网卡", "网段", "状态", "本次新增", "IP 变更"]
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        if settings.get("csv_include_header", True):
            writer.writerow(columns)
        for row in task.get("results", []):
            writer.writerow(
                [
                    sanitize_csv_cell(row.get("ip", "")),
                    sanitize_csv_cell(row.get("mac", "")),
                    sanitize_csv_cell(row.get("display_name", "")),
                    sanitize_csv_cell(row.get("vendor", "")),
                    sanitize_csv_cell(row.get("interface", "")),
                    sanitize_csv_cell(row.get("scan_range", "")),
                    sanitize_csv_cell(row.get("state", "")),
                    "是" if row.get("is_new") else "否",
                    "是" if row.get("ip_changed") else "否",
                ]
            )
        body = buffer.getvalue()
        if settings.get("csv_bom", True):
            body = "\ufeff" + body
        return body, f"network-locator-scan-{task_id[:12]}.csv"

    def summary(self) -> dict[str, Any]:
        devices = self.list_devices()
        counts = {"total": len(devices), "online": 0, "offline": 0, "unknown": 0, "favorites": 0}
        for device in devices:
            counts[str(device.get("status") or "unknown")] = counts.get(str(device.get("status") or "unknown"), 0) + 1
            if device.get("favorite"):
                counts["favorites"] += 1
        tasks = self.list_scan_tasks(limit=1)
        return {"devices": counts, "last_scan": tasks[0] if tasks else None}


def _ip_value(value: str) -> int:
    try:
        return int(ipaddress.ip_address(value))
    except ValueError:
        return 0
