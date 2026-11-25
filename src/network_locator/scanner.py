"""Persistent inventory discovery with pause, resume, and custom ranges."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .core import (
    CommandError,
    NetworkTarget,
    NeighborRecord,
    ScanCancelled,
    network_target_key,
    normalize_device_mac,
    normalize_network_ids,
    parse_ipv4_network,
    select_network_targets,
)
from .store import NetworkStore, utc_now

LOGGER = logging.getLogger("network_locator.scanner")
MAX_CUSTOM_HOSTS_PER_RANGE = 1022
MAX_CUSTOM_HOSTS_TOTAL = 4096
STATE_PRIORITY = {"STALE": 1, "PROBE": 2, "DELAY": 3, "REACHABLE": 4, "LOCAL": 5}


class ScanScopeError(ValueError):
    """Raised when a requested interface/range scope is invalid."""


def _split_values(values: object) -> list[str]:
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, (list, tuple)):
        raw = [str(value) for value in values]
    else:
        return []
    result: list[str] = []
    for value in raw:
        for item in re.split(r"[\r\n,;，；]+", value):
            token = item.strip()
            if token and token not in result:
                result.append(token)
    return result


def parse_custom_ranges(
    values: object,
    *,
    max_hosts: int = MAX_CUSTOM_HOSTS_PER_RANGE,
) -> list[dict]:
    """Parse CIDR, ``start-end``, and single-IP scopes without inventing hosts."""

    definitions: list[dict] = []
    total = 0
    for token in _split_values(values):
        try:
            if "/" in token:
                network = ipaddress.ip_network(token, strict=False)
                if not isinstance(network, ipaddress.IPv4Network):
                    raise ValueError("仅支持 IPv4 网段")
                if network.prefixlen >= 31:
                    start = int(network.network_address)
                    end = int(network.broadcast_address)
                    host_count = network.num_addresses
                else:
                    start = int(network.network_address) + 1
                    end = int(network.broadcast_address) - 1
                    host_count = max(0, network.num_addresses - 2)
                label = str(network)
                kind = "cidr"
            elif "-" in token:
                parts = [part.strip() for part in token.split("-", 1)]
                if len(parts) != 2:
                    raise ValueError("IP 范围格式应为 起始IP-结束IP")
                start_address = ipaddress.IPv4Address(parts[0])
                end_address = ipaddress.IPv4Address(parts[1])
                start = int(start_address)
                end = int(end_address)
                if start > end:
                    raise ValueError("范围起点不能大于终点")
                host_count = end - start + 1
                label = f"{start_address}-{end_address}"
                kind = "range"
            else:
                address = ipaddress.IPv4Address(token)
                start = end = int(address)
                host_count = 1
                label = str(address)
                kind = "single"
        except ValueError as exc:
            raise ScanScopeError(f"自定义范围“{token}”无效：{exc}") from exc
        if host_count <= 0:
            raise ScanScopeError(f"自定义范围“{token}”不包含可扫描主机")
        if host_count > max_hosts:
            raise ScanScopeError(
                f"自定义范围“{token}”包含 {host_count} 台主机，超过安全上限 {max_hosts}"
            )
        total += host_count
        if total > MAX_CUSTOM_HOSTS_TOTAL:
            raise ScanScopeError(f"自定义范围总计超过 {MAX_CUSTOM_HOSTS_TOTAL} 台主机")
        definitions.append(
            {
                "range_id": f"custom|{label}",
                "label": label,
                "kind": kind,
                "start": start,
                "end": end,
                "host_count": host_count,
            }
        )
    return definitions


def expand_custom_range(definition: Mapping) -> list[ipaddress.IPv4Address]:
    return [
        ipaddress.IPv4Address(value)
        for value in range(int(definition["start"]), int(definition["end"]) + 1)
    ]


def _group_targets(targets: Iterable[NetworkTarget]) -> list[list[NetworkTarget]]:
    grouped: dict[str, list[NetworkTarget]] = {}
    for target in targets:
        grouped.setdefault(target.scan_range, []).append(target)
    return list(grouped.values())


def _is_in_definition(value: str, definition: Mapping[str, Any]) -> bool:
    try:
        number = int(ipaddress.IPv4Address(value))
    except ValueError:
        return False
    return int(definition["start"]) <= number <= int(definition["end"])


def _is_usable_ipv4(value: object) -> bool:
    try:
        address = ipaddress.IPv4Address(str(value))
    except ValueError:
        return False
    return not (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or int(address) == 0xFFFFFFFF
    )


class InventoryScanner:
    """Run one durable inventory scan at a time, independent of page state."""

    def __init__(
        self,
        store: NetworkStore,
        *,
        discover: Callable[..., tuple[list[NetworkTarget], list[str]]] | None = None,
        neighbors: Callable[[NetworkTarget], list[NeighborRecord]] | None = None,
        sweep: Callable[..., set[str]] | None = None,
        custom_sweep: Callable[..., set[str]] | None = None,
        auto_schedule: bool = True,
    ) -> None:
        self.store = store
        if discover is None or neighbors is None or sweep is None or custom_sweep is None:
            from .win_network import (
                discover_networks,
                read_neighbors,
                scan_hosts,
                scan_ip_addresses,
            )

            discover = discover or discover_networks
            neighbors = neighbors or read_neighbors
            sweep = sweep or scan_hosts
            custom_sweep = custom_sweep or scan_ip_addresses
        self._discover = discover
        self._neighbors = neighbors
        self._sweep = sweep
        self._custom_sweep = custom_sweep
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._pause: threading.Event | None = None
        self._state = self._idle_state()
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        if auto_schedule:
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                name="network-locator-scheduler",
                daemon=True,
            )
            self._scheduler_thread.start()

    @staticmethod
    def _idle_state() -> dict:
        return {
            "task_id": "",
            "status": "idle",
            "running": False,
            "paused": False,
            "stopping": False,
            "started_at": "",
            "finished_at": "",
            "total_hosts": 0,
            "probed_hosts": 0,
            "progress_percent": 0,
            "current_interface": "",
            "current_range": "",
            "warnings": [],
            "results": [],
            "error": "",
            "trigger": "",
        }

    def status(self) -> dict:
        with self._lock:
            snapshot = dict(self._state)
            snapshot["warnings"] = list(self._state.get("warnings") or [])
            snapshot["results"] = [dict(item) for item in self._state.get("results") or []]
            return snapshot

    def start(
        self,
        network_ids: Sequence[str] | None = None,
        custom_ranges: object = None,
        *,
        trigger: str = "manual",
    ) -> dict:
        if network_ids is None:
            normalized_ids = None
        elif isinstance(network_ids, (list, tuple)) and not network_ids:
            normalized_ids = []
        else:
            normalized_ids = normalize_network_ids(network_ids)
        settings = self.store.get_settings()
        max_hosts = max(1, int(settings.get("scan_max_hosts") or MAX_CUSTOM_HOSTS_PER_RANGE))
        definitions = parse_custom_ranges(custom_ranges, max_hosts=max_hosts)
        if normalized_ids is not None and not normalized_ids and not definitions:
            raise ScanScopeError("请选择至少一个网段或输入自定义范围")
        with self._lock:
            if self._state.get("running"):
                raise ScanScopeError("已有扫描任务正在运行")
            task_id = self.store.create_scan_task(
                trigger=trigger,
                network_ids=normalized_ids or [],
                custom_ranges=[item["label"] for item in definitions],
                config={
                    "timeout_ms": settings.get("scan_timeout_ms"),
                    "concurrency": settings.get("scan_concurrency"),
                },
            )
            self._cancel = threading.Event()
            self._pause = threading.Event()
            self._state = {
                **self._idle_state(),
                "task_id": task_id,
                "status": "discovering",
                "running": True,
                "started_at": utc_now(),
                "trigger": trigger,
                "total_hosts": sum(item["host_count"] for item in definitions),
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(task_id, normalized_ids, definitions, settings, self._cancel, self._pause),
                name="network-locator-inventory-scan",
                daemon=True,
            )
            self._thread.start()
            return self.status()

    def pause(self) -> bool:
        with self._lock:
            if not self._state.get("running") or self._state.get("paused"):
                return False
            if self._pause is not None:
                self._pause.set()
            self._state["paused"] = True
            self._state["status"] = "paused"
            task_id = str(self._state.get("task_id") or "")
        if task_id:
            self.store.update_scan_progress(task_id, status="paused")
        return True

    def resume(self) -> bool:
        with self._lock:
            if not self._state.get("running") or not self._state.get("paused"):
                return False
            if self._pause is not None:
                self._pause.clear()
            self._state["paused"] = False
            self._state["status"] = "scanning"
            task_id = str(self._state.get("task_id") or "")
        if task_id:
            self.store.update_scan_progress(task_id, status="scanning")
        return True

    def stop(self) -> bool:
        with self._lock:
            if not self._state.get("running"):
                return False
            self._state["stopping"] = True
            if self._pause is not None:
                self._pause.clear()
            if self._cancel is not None:
                self._cancel.set()
            return True

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def shutdown(self) -> None:
        self._scheduler_stop.set()
        scheduler = self._scheduler_thread
        if scheduler is not None:
            scheduler.join(timeout=2)
        self.stop()
        self.wait(3)

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def _wait_if_paused(self) -> None:
        while self._pause is not None and self._pause.is_set():
            if self._cancel is not None and self._cancel.is_set():
                raise ScanCancelled("scan stopped")
            time.sleep(0.15)

    def _run(
        self,
        task_id: str,
        network_ids: list[str] | None,
        custom_definitions: list[dict],
        settings: Mapping[str, Any],
        cancel: threading.Event,
        pause: threading.Event,
    ) -> None:
        observed: list[dict] = []
        warnings: list[str] = []
        total_hosts = sum(item["host_count"] for item in custom_definitions)
        probed_hosts = 0
        try:
            targets, discovery_warnings = self._discover()
            warnings.extend(discovery_warnings)
            if network_ids is None:
                default_interface = str(settings.get("default_interface_id") or "")
                preferred = [
                    target
                    for target in targets
                    if target.interface_id == default_interface
                ]
                selected, missing = (preferred or list(targets)), []
            else:
                selected, missing = select_network_targets(targets, network_ids)
            if missing:
                warnings.append("所选网段当前不可用：" + "、".join(missing))
            if not selected and not custom_definitions:
                raise ScanScopeError(
                    warnings[0] if warnings else "没有可扫描的网段或自定义范围"
                )
            groups = _group_targets(selected)
            all_network_ids = {network_target_key(target) for target in targets}
            online_scope = (
                not custom_definitions
                and bool(selected)
                and {network_target_key(target) for target in selected} == all_network_ids
            )
            total_hosts += sum(group[0].host_count for group in groups)
            self.store.sync_network_snapshot(
                self._interface_rows(targets),
                [self._segment_row(target) for target in targets],
            )
            self.store.update_scan_progress(
                task_id,
                total_hosts=total_hosts,
                probed_hosts=0,
                status="scanning",
                warnings=warnings,
            )
            self._update(
                status="scanning",
                total_hosts=total_hosts,
                probed_hosts=0,
                progress_percent=0,
                warnings=list(warnings),
            )
            timeout_ms = int(settings.get("scan_timeout_ms") or 900)
            concurrency = int(settings.get("scan_concurrency") or 64)
            for group in groups:
                self._wait_if_paused()
                if cancel.is_set():
                    raise ScanCancelled("scan stopped")
                target = group[0]
                base = probed_hosts
                self._update(
                    current_interface=target.interface,
                    current_range=target.scan_range,
                    status="scanning",
                )

                def on_progress(completed: int, offset: int = base) -> None:
                    done = min(total_hosts, offset + completed)
                    percent = 0 if total_hosts <= 0 else int(done * 100 / total_hosts)
                    self._update(probed_hosts=done, progress_percent=percent)
                    self.store.update_scan_progress(
                        task_id, probed_hosts=done, total_hosts=total_hosts
                    )

                reachable = self._sweep(
                    target,
                    cancel,
                    on_progress,
                    timeout_ms=timeout_ms,
                    concurrency=concurrency,
                    pause_event=pause,
                )
                for member in group:
                    local_mac = normalize_device_mac(member.local_mac)
                    if local_mac:
                        observed.append(
                            {
                                "ip": member.local_ip,
                                "mac": local_mac,
                                "state": "LOCAL",
                                "interface": member.interface,
                                "interface_id": member.interface_id,
                                "scan_range": member.scan_range,
                            }
                        )
                    try:
                        records = self._neighbors(member)
                    except (CommandError, OSError) as exc:
                        records = []
                        warnings.append(f"{member.interface} 邻居表读取失败：{exc}")
                    observed.extend(
                        self._records_for_network(member, records, reachable)
                    )
                probed_hosts += target.host_count
                self._merge_and_publish(observed, warnings, probed_hosts, total_hosts)

            custom_arp = self._custom_arp_map(selected, warnings)
            interface_id_by_index = {
                str(target.interface_index): (
                    target.interface_id or f"{target.interface}|{target.interface_index}"
                )
                for target in selected
            }
            for definition in custom_definitions:
                self._wait_if_paused()
                if cancel.is_set():
                    raise ScanCancelled("scan stopped")
                base = probed_hosts
                label = str(definition["label"])
                self._update(
                    current_interface="自定义范围",
                    current_range=label,
                    status="scanning",
                )

                def on_custom_progress(completed: int, offset: int = base) -> None:
                    done = min(total_hosts, offset + completed)
                    percent = 0 if total_hosts <= 0 else int(done * 100 / total_hosts)
                    self._update(probed_hosts=done, progress_percent=percent)
                    self.store.update_scan_progress(
                        task_id, probed_hosts=done, total_hosts=total_hosts
                    )

                hosts = expand_custom_range(definition)
                reachable = self._custom_sweep(
                    hosts,
                    cancel,
                    on_custom_progress,
                    timeout_ms=timeout_ms,
                    concurrency=concurrency,
                    pause_event=pause,
                )
                for ip in sorted(reachable, key=_ip_sort_value):
                    if not _is_usable_ipv4(ip):
                        continue
                    record = custom_arp.get(ip)
                    mac = normalize_device_mac(record.mac) if record else None
                    observed.append(
                        {
                            "ip": ip,
                            "mac": mac or "",
                            "state": "REACHABLE",
                            "interface": record.interface if record else "自定义范围",
                            "interface_id": (
                                interface_id_by_index.get(str(record.interface_index), "")
                                if record
                                else ""
                            ),
                            "scan_range": label,
                        }
                    )
                for record in custom_arp.values():
                    mac = normalize_device_mac(record.mac)
                    if (
                        mac
                        and _is_usable_ipv4(record.ip)
                        and _is_in_definition(record.ip, definition)
                    ):
                        observed.append(
                            {
                                "ip": record.ip,
                                "mac": mac,
                                "state": "REACHABLE" if record.ip in reachable else "STALE",
                                "interface": record.interface,
                                "interface_id": interface_id_by_index.get(
                                    str(record.interface_index), ""
                                ),
                                "scan_range": label,
                            }
                        )
                probed_hosts += int(definition["host_count"])
                self._merge_and_publish(observed, warnings, probed_hosts, total_hosts)

            if cancel.is_set():
                raise ScanCancelled("scan stopped")
            self.store.ingest_scan_results(task_id, observed, warnings=warnings)
            counts = self.store.finish_scan_task(
                task_id,
                status="completed",
                total_hosts=total_hosts,
                probed_hosts=total_hosts,
                warnings=warnings,
                mark_offline=online_scope,
            )
            self._merge_and_publish(observed, warnings, total_hosts, total_hosts)
            with self._lock:
                self._state.update(
                    status="completed",
                    running=False,
                    paused=False,
                    stopping=False,
                    finished_at=utc_now(),
                    progress_percent=100,
                    probed_hosts=total_hosts,
                    current_interface="",
                    current_range="",
                    summary=counts,
                )
        except ScanCancelled:
            if observed:
                try:
                    self.store.ingest_scan_results(task_id, observed, warnings=warnings)
                except Exception:
                    LOGGER.exception("failed to persist partial scan results")
            self.store.finish_scan_task(
                task_id,
                status="stopped",
                total_hosts=total_hosts,
                probed_hosts=probed_hosts,
                warnings=warnings,
                error="扫描已停止",
            )
            self._merge_and_publish(observed, warnings, probed_hosts, total_hosts)
            with self._lock:
                self._state.update(
                    status="stopped",
                    running=False,
                    paused=False,
                    stopping=False,
                    finished_at=utc_now(),
                    current_interface="",
                    current_range="",
                )
        except Exception as exc:
            LOGGER.exception("inventory scan failed")
            self.store.finish_scan_task(
                task_id,
                status="error",
                total_hosts=total_hosts,
                probed_hosts=probed_hosts,
                warnings=warnings,
                error=str(exc),
            )
            with self._lock:
                self._state.update(
                    status="error",
                    running=False,
                    paused=False,
                    stopping=False,
                    finished_at=utc_now(),
                    current_interface="",
                    current_range="",
                    error=str(exc),
                    warnings=list(warnings),
                )

    def _merge_and_publish(
        self,
        observed: list[dict],
        warnings: list[str],
        probed_hosts: int,
        total_hosts: int,
    ) -> None:
        merged: dict[tuple[str, str, str], dict] = {}
        for item in observed:
            key = (
                str(item.get("mac") or "no-mac"),
                str(item.get("ip") or ""),
                str(item.get("interface") or ""),
            )
            existing = merged.get(key)
            if existing is None or STATE_PRIORITY.get(str(item.get("state") or ""), 0) > STATE_PRIORITY.get(
                str(existing.get("state") or ""), 0
            ):
                merged[key] = dict(item)
        rows = sorted(
            merged.values(),
            key=lambda item: (_ip_sort_value(str(item.get("ip") or "")), str(item.get("mac") or "")),
        )
        percent = 0 if total_hosts <= 0 else min(100, int(probed_hosts * 100 / total_hosts))
        self._update(
            results=rows,
            warnings=list(warnings),
            probed_hosts=min(probed_hosts, total_hosts),
            progress_percent=percent,
        )

    def _records_for_network(
        self,
        target: NetworkTarget,
        records: Iterable[NeighborRecord],
        reachable: set[str],
    ) -> list[dict]:
        network = parse_ipv4_network(target.local_ip, target.prefix_length)
        result: list[dict] = []
        for record in records:
            try:
                address = ipaddress.ip_address(record.ip)
            except ValueError:
                continue
            if not isinstance(address, ipaddress.IPv4Address) or address not in network:
                continue
            if network.prefixlen <= 30 and address in {
                network.network_address,
                network.broadcast_address,
            }:
                continue
            mac = normalize_device_mac(record.mac)
            if mac is None:
                continue
            result.append(
                {
                    "ip": record.ip,
                    "mac": mac,
                    "state": "REACHABLE" if record.ip in reachable else "STALE",
                    "interface": record.interface,
                    "interface_id": target.interface_id,
                    "scan_range": target.scan_range,
                }
            )
        return result

    def _custom_arp_map(
        self,
        targets: Sequence[NetworkTarget],
        warnings: list[str],
    ) -> dict[str, NeighborRecord]:
        records: dict[str, NeighborRecord] = {}
        for target in targets:
            try:
                for record in self._neighbors(target):
                    records.setdefault(record.ip, record)
            except (CommandError, OSError) as exc:
                warnings.append(f"{target.interface} 自定义范围邻居表读取失败：{exc}")
        return records

    @staticmethod
    def _interface_rows(targets: Sequence[NetworkTarget]) -> list[dict]:
        rows: dict[str, dict] = {}
        for target in targets:
            interface_id = target.interface_id or f"{target.interface}|{target.interface_index}"
            rows[interface_id] = {
                "interface_id": interface_id,
                "interface_index": target.interface_index,
                "alias": target.alias or target.interface,
                "description": "",
                "local_ip": target.local_ip,
                "prefix_length": target.prefix_length,
                "gateway": target.gateway,
                "dns_servers": [],
                "is_virtual": False,
                "is_up": True,
            }
        return list(rows.values())

    @staticmethod
    def _segment_row(target: NetworkTarget) -> dict:
        interface_id = target.interface_id or f"{target.interface}|{target.interface_index}"
        return {
            **target.as_dict(),
            "segment_id": network_target_key(target),
            "interface_id": interface_id,
        }

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.wait(15):
            try:
                settings = self.store.get_settings()
                if not settings.get("auto_scan_enabled"):
                    continue
                with self._lock:
                    if self._state.get("running"):
                        continue
                tasks = self.store.list_scan_tasks(limit=1)
                interval = max(5, int(settings.get("auto_scan_interval_minutes") or 30)) * 60
                if tasks:
                    started = datetime.fromisoformat(
                        str(tasks[0].get("started_at") or "").replace("Z", "+00:00")
                    )
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    if elapsed < interval:
                        continue
                self.start(None, [], trigger="auto")
            except Exception:
                LOGGER.exception("automatic scan scheduler failed")


def _ip_sort_value(value: str) -> int:
    try:
        return int(ipaddress.ip_address(value))
    except ValueError:
        return 0
