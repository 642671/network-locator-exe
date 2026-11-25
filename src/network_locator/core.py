"""Platform-neutral discovery results, MAC matching, and scan state."""

from __future__ import annotations

import ipaddress
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Iterable, Sequence

APP_ID = "network-locator"
DEFAULT_MAX_HOSTS = 1022
DEFAULT_CONCURRENCY = 64
MAX_SCAN_SUFFIXES = 8
MAX_NETWORK_SELECTION = 64
MAX_NETWORK_ID_LENGTH = 256
DEVICE_STATE_PRIORITY = {
    "STALE": 1,
    "PROBE": 2,
    "DELAY": 3,
    "REACHABLE": 4,
    "LOCAL": 5,
}
MAC_SUFFIX_PATTERN = re.compile(
    r"^(?:[0-9A-Fa-f]{2}[-:][0-9A-Fa-f]{2}|[0-9A-Fa-f]{4})$"
)
VIRTUAL_INTERFACE_PREFIXES = (
    "docker",
    "veth",
    "virbr",
    "vmnet",
    "tailscale",
    "tun",
    "tap",
    "wg",
    "zt",
    "cni",
    "flannel",
    "podman",
    "lxc",
    "kube",
    "dummy",
    "lo",
)


class CommandError(RuntimeError):
    """Raised when a required local network command fails."""


class ScanCancelled(RuntimeError):
    """Internal cancellation signal."""


@dataclass(frozen=True)
class NetworkTarget:
    interface: str
    alias: str
    local_ip: str
    prefix_length: int
    gateway: str
    network: str
    broadcast: str
    scan_range: str
    host_count: int
    local_source: str
    local_mac: str
    interface_index: int = 0
    interface_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NeighborRecord:
    interface: str
    ip: str
    mac: str
    state: str
    interface_index: int = 0


def network_target_key(target: NetworkTarget) -> str:
    return f"{target.interface}|{target.scan_range}"


def network_target_payload(target: NetworkTarget) -> dict:
    payload = target.as_dict()
    payload["network_id"] = network_target_key(target)
    payload["segment_id"] = network_target_key(target)
    if not payload.get("interface_id"):
        payload["interface_id"] = (
            f"{target.interface}|{target.interface_index}"
            if target.interface_index
            else target.interface
        )
    return payload


def normalize_network_ids(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError("network_ids 必须是数组")
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("network_ids 只能包含字符串")
        text = item.strip()
        if not text:
            raise ValueError("network_ids 不能包含空值")
        if len(text) > MAX_NETWORK_ID_LENGTH:
            raise ValueError("网段标识过长")
        if text not in selected:
            selected.append(text)
    if not selected:
        raise ValueError("至少选择一个网段")
    if len(selected) > MAX_NETWORK_SELECTION:
        raise ValueError(f"最多选择 {MAX_NETWORK_SELECTION} 个网段")
    return selected


def select_network_targets(
    targets: Sequence[NetworkTarget],
    network_ids: Sequence[str] | None,
) -> tuple[list[NetworkTarget], list[str]]:
    available = list(targets)
    if network_ids is None:
        return available, []
    wanted = set(network_ids)
    available_ids = {network_target_key(target) for target in available}
    selected = [
        target for target in available if network_target_key(target) in wanted
    ]
    missing = [network_id for network_id in network_ids if network_id not in available_ids]
    return selected, missing


def normalize_mac_suffix(value: str) -> str | None:
    text = str(value or "").strip()
    if MAC_SUFFIX_PATTERN.fullmatch(text) is None:
        return None
    return re.sub(r"[-:]", "", text).upper()


def normalize_mac_suffixes(value: object) -> list[str]:
    """Normalize one suffix or a list of suffixes for a multi-target scan."""

    if isinstance(value, str):
        values: list[object] = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("MAC suffixes must be an array")

    suffixes: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("MAC suffixes must be strings")
        normalized = normalize_mac_suffix(item)
        if normalized is None:
            raise ValueError("MAC suffix must use AA-BB, AA:BB, or AABB format")
        if normalized not in suffixes:
            suffixes.append(normalized)

    if not suffixes:
        raise ValueError("at least one MAC suffix is required")
    if len(suffixes) > MAX_SCAN_SUFFIXES:
        raise ValueError(f"at most {MAX_SCAN_SUFFIXES} MAC suffixes are allowed")
    return suffixes


def normalize_mac(value: str) -> str | None:
    text = str(value or "").strip().replace("-", "").replace(":", "")
    if re.fullmatch(r"[0-9A-Fa-f]{12}", text) is None:
        return None
    upper = text.upper()
    return "-".join(upper[index : index + 2] for index in range(0, 12, 2))


def normalize_device_mac(value: str) -> str | None:
    """Normalize a MAC that can identify a real unicast device.

    ARP tables can contain the Ethernet broadcast address and IPv4/IPv6
    multicast mappings. Those values are not devices and must never become
    inventory records.
    """

    normalized = normalize_mac(value)
    if normalized is None:
        return None
    octets = bytes.fromhex(normalized.replace("-", ""))
    if octets == b"\x00" * 6 or octets == b"\xff" * 6:
        return None
    if octets[0] & 0x01:
        return None
    return normalized


def parse_ipv4_network(address: str, prefix_length: int) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(f"{address}/{int(prefix_length)}", strict=False)
    except ValueError as exc:
        raise CommandError(
            f"invalid IPv4 network {address}/{prefix_length}: {exc}"
        ) from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise CommandError(f"not an IPv4 network: {address}/{prefix_length}")
    return network


def network_host_count(network: ipaddress.IPv4Network) -> int:
    if network.prefixlen <= 30:
        return max(0, network.num_addresses - 2)
    if network.prefixlen == 31:
        return 2
    return 1


def network_hosts(network: ipaddress.IPv4Network) -> list[ipaddress.IPv4Address]:
    if network.prefixlen <= 30:
        return list(network.hosts())
    if network.prefixlen == 31:
        return list(network)
    return [network.network_address]


def is_scannable_local_address(address: str) -> bool:
    """Reject addresses that cannot identify a real LAN interface."""

    try:
        parsed = ipaddress.IPv4Address(str(address or "").strip())
    except ValueError:
        return False
    return not (
        parsed.is_unspecified
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
    )


EXACT_VIRTUAL_NAMES = {"lo", "wg", "zt"}


def is_virtual_interface(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return True
    if lowered in EXACT_VIRTUAL_NAMES:
        return True
    return any(
        lowered.startswith(prefix)
        for prefix in VIRTUAL_INTERFACE_PREFIXES
        if prefix not in EXACT_VIRTUAL_NAMES
    )


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def match_neighbor_records(
    records: Iterable[NeighborRecord],
    suffix: str,
    scan_range: str,
    reachable: set[str] | None = None,
) -> list[dict]:
    return match_neighbor_records_multi(records, [suffix], scan_range, reachable)


def match_neighbor_records_multi(
    records: Iterable[NeighborRecord],
    suffixes: Sequence[str] | str,
    scan_range: str,
    reachable: set[str] | None = None,
) -> list[dict]:
    """Return neighbor matches annotated with every requested suffix they hit."""

    normalized_suffixes = normalize_mac_suffixes(suffixes)
    matches: list[dict] = []
    for record in records:
        compact = record.mac.replace("-", "")
        matched = [
            suffix for suffix in normalized_suffixes if compact.endswith(suffix)
        ]
        if not matched:
            continue
        state = record.state
        if reachable is not None:
            state = "REACHABLE" if record.ip in reachable else "STALE"
        matches.append(
            {
                "interface": record.interface,
                "scan_range": scan_range,
                "ip": record.ip,
                "mac": record.mac,
                "state": state,
                "matched_suffixes": matched,
            }
        )
    return matches


def _sort_results(results: Iterable[dict]) -> list[dict]:
    def key(item: dict) -> tuple:
        try:
            ip_value = int(ipaddress.ip_address(item.get("ip", "")))
        except ValueError:
            ip_value = 0
        return (item.get("interface", ""), ip_value, item.get("mac", ""))

    return sorted(results, key=key)


def _merge_device(unique: dict[tuple[str, str, str], dict], item: dict) -> None:
    key = (
        str(item.get("scan_range", "")),
        str(item.get("ip", "")),
        str(item.get("mac", "")),
    )
    existing = unique.get(key)
    if existing is None:
        unique[key] = dict(item)
        return
    suffixes = list(existing.get("matched_suffixes") or [])
    for suffix in item.get("matched_suffixes") or []:
        if suffix not in suffixes:
            suffixes.append(suffix)
    item_priority = DEVICE_STATE_PRIORITY.get(str(item.get("state", "")), 0)
    existing_priority = DEVICE_STATE_PRIORITY.get(str(existing.get("state", "")), 0)
    merged = dict(item) if item_priority > existing_priority else dict(existing)
    if suffixes:
        merged["matched_suffixes"] = sorted(suffixes)
    unique[key] = merged


def _merge_devices(results: Iterable[dict]) -> dict[tuple[str, str, str], dict]:
    unique: dict[tuple[str, str, str], dict] = {}
    for item in results:
        _merge_device(unique, item)
    return unique


def _sweep_groups(targets: Iterable[NetworkTarget]) -> list[list[NetworkTarget]]:
    grouped: dict[str, list[NetworkTarget]] = {}
    for target in targets:
        grouped.setdefault(target.scan_range, []).append(target)
    return list(grouped.values())


def _default_neighbors() -> Callable[[NetworkTarget], list[NeighborRecord]]:
    from .win_network import read_neighbors

    return read_neighbors


def _default_sweep() -> Callable[..., set[str]]:
    from .win_network import scan_hosts

    return scan_hosts


def _default_discover() -> Callable[..., tuple[list[NetworkTarget], list[str]]]:
    from .win_network import discover_networks

    return discover_networks


def collect_neighbor_devices(
    targets: Iterable[NetworkTarget],
    neighbors: Callable[[NetworkTarget], list[NeighborRecord]] | None = None,
    reachable: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Return every known neighbor that belongs to a scannable routed network."""

    neighbor_reader = neighbors or _default_neighbors()
    warnings: list[str] = []
    devices: dict[tuple[str, str, str], dict] = {}

    for target in targets:
        local_mac = normalize_mac(target.local_mac)
        if local_mac:
            _merge_device(
                devices,
                {
                    "interface": target.interface,
                    "scan_range": target.scan_range,
                    "ip": target.local_ip,
                    "mac": local_mac,
                    "state": "LOCAL",
                },
            )
        try:
            records = neighbor_reader(target)
        except (CommandError, OSError) as exc:
            records = []
            warnings.append(f"{target.interface} 邻居表读取失败：{exc}")

        network = parse_ipv4_network(target.local_ip, target.prefix_length)
        for record in records:
            try:
                address = ipaddress.ip_address(record.ip)
            except ValueError:
                continue
            if not isinstance(address, ipaddress.IPv4Address) or address not in network:
                continue
            state = record.state
            if reachable is not None:
                state = "REACHABLE" if record.ip in reachable else "STALE"
            _merge_device(
                devices,
                {
                    "interface": record.interface,
                    "scan_range": target.scan_range,
                    "ip": record.ip,
                    "mac": record.mac,
                    "state": state,
                },
            )

    return _sort_results(devices.values()), warnings


def probe_network_devices(
    targets: Iterable[NetworkTarget],
    *,
    sweep: Callable[..., set[str]] | None = None,
    neighbors: Callable[[NetworkTarget], list[NeighborRecord]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Actively probe each routed network, then return every known neighbor."""

    sweep_hosts = sweep or _default_sweep()
    target_list = list(targets)
    warnings: list[str] = []
    reachable: set[str] = set()
    cancel_event = threading.Event()
    for group in _sweep_groups(target_list):
        target = group[0]
        try:
            reachable.update(sweep_hosts(target, cancel_event))
        except (CommandError, OSError) as exc:
            warnings.append(f"{target.interface} 探测失败：{exc}")

    devices, neighbor_warnings = collect_neighbor_devices(
        target_list,
        neighbors,
        reachable,
    )
    return devices, warnings + neighbor_warnings


def match_local_target(target: NetworkTarget, suffix: str) -> list[dict]:
    return match_local_target_multi(target, [suffix])


def match_local_target_multi(
    target: NetworkTarget,
    suffixes: Sequence[str] | str,
) -> list[dict]:
    normalized_suffixes = normalize_mac_suffixes(suffixes)
    local_mac = normalize_mac(target.local_mac)
    if local_mac is None:
        return []
    compact = local_mac.replace("-", "")
    matched = [
        suffix for suffix in normalized_suffixes if compact.endswith(suffix)
    ]
    if not matched:
        return []
    return [
        {
            "interface": target.interface,
            "scan_range": target.scan_range,
            "ip": target.local_ip,
            "mac": local_mac,
            "state": "LOCAL",
            "matched_suffixes": matched,
        }
    ]


class ScanManager:
    """Run one scan at a time and expose polling-friendly state."""

    def __init__(
        self,
        *,
        discover: Callable[..., tuple[list[NetworkTarget], list[str]]] | None = None,
        neighbors: Callable[[NetworkTarget], list[NeighborRecord]] | None = None,
        sweep: Callable[..., set[str]] | None = None,
    ) -> None:
        self._discover = discover or _default_discover()
        self._neighbors = neighbors or _default_neighbors()
        self._sweep = sweep or _default_sweep()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._requested_network_ids: list[str] | None = None
        self._state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict:
        return {
            "status": "idle",
            "running": False,
            "stopping": False,
            "job_id": "",
            "suffix": "",
            "suffixes": [],
            "selected_network_ids": [],
            "started_at": None,
            "finished_at": None,
            "networks": [],
            "warnings": [],
            "total_hosts": 0,
            "probed_hosts": 0,
            "progress_percent": 0,
            "current_interface": "",
            "current_range": "",
            "results": [],
            "error": "",
        }

    def status(self) -> dict:
        with self._lock:
            snapshot = dict(self._state)
            snapshot["suffixes"] = list(self._state["suffixes"])
            snapshot["selected_network_ids"] = list(
                self._state["selected_network_ids"]
            )
            snapshot["networks"] = [dict(item) for item in self._state["networks"]]
            snapshot["warnings"] = list(self._state["warnings"])
            snapshot["results"] = [dict(item) for item in self._state["results"]]
            return snapshot

    def start(
        self,
        suffixes: Sequence[str] | str,
        network_ids: Sequence[str] | None = None,
    ) -> bool:
        normalized = normalize_mac_suffixes(suffixes)
        selected_network_ids = normalize_network_ids(network_ids)
        with self._lock:
            if self._state["running"]:
                return False
            self._requested_network_ids = (
                list(selected_network_ids)
                if selected_network_ids is not None
                else None
            )
            self._cancel = threading.Event()
            self._state = self._idle_state()
            self._state.update(
                {
                    "status": "discovering",
                    "running": True,
                    "job_id": uuid.uuid4().hex,
                    "suffix": normalized[0],
                    "suffixes": normalized,
                    "selected_network_ids": selected_network_ids or [],
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    self._state["job_id"],
                    normalized,
                    self._cancel,
                    selected_network_ids,
                ),
                name="network-locator-scan",
                daemon=True,
            )
            self._thread.start()
        return True

    def matches_request(
        self,
        suffixes: Sequence[str] | str,
        network_ids: Sequence[str] | None = None,
    ) -> bool:
        normalized = normalize_mac_suffixes(suffixes)
        requested = normalize_network_ids(network_ids)
        with self._lock:
            active_suffixes = self._state["suffixes"] or [self._state["suffix"]]
            if set(active_suffixes) != set(normalized):
                return False
            active = self._requested_network_ids
            if active is None or requested is None:
                return active is None and requested is None
            return set(active) == set(requested)

    def stop(self) -> bool:
        with self._lock:
            if not self._state["running"]:
                return False
            self._state["stopping"] = True
            self._cancel.set()
            return True

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _update(self, **values) -> None:
        with self._lock:
            self._state.update(values)

    def _append_results(self, values: Iterable[dict]) -> None:
        with self._lock:
            combined = list(self._state["results"]) + list(values)
            unique = _merge_devices(combined)
            self._state["results"] = _sort_results(unique.values())

    def _run(
        self,
        job_id: str,
        suffixes: list[str],
        cancel_event: threading.Event,
        selected_network_ids: list[str] | None,
    ) -> None:
        try:
            targets, warnings = self._discover()
            targets, missing = select_network_targets(targets, selected_network_ids)
            if missing:
                warnings = warnings + ["所选网段当前不可用：" + "、".join(missing)]
            if cancel_event.is_set():
                raise ScanCancelled("scan stopped")
            if not targets:
                message = (
                    "所选网段当前不可用，请刷新网段后重试"
                    if selected_network_ids is not None
                    else (warnings[0] if warnings else "no scannable physical networks")
                )
                self._update(
                    status="error",
                    running=False,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    warnings=warnings,
                    error=message,
                )
                return

            sweep_groups = _sweep_groups(targets)
            total = sum(group[0].host_count for group in sweep_groups)
            self._update(
                status="scanning",
                networks=[network_target_payload(target) for target in targets],
                selected_network_ids=[
                    network_target_key(target) for target in targets
                ],
                warnings=warnings,
                total_hosts=total,
            )
            probed_before = 0
            for group in sweep_groups:
                if cancel_event.is_set():
                    raise ScanCancelled("scan stopped")
                target = group[0]
                self._update(
                    current_interface=target.interface,
                    current_range=target.scan_range,
                )

                def on_progress(completed: int, offset: int = probed_before) -> None:
                    done = min(total, offset + completed)
                    percent = 0 if total <= 0 else int(done * 100 / total)
                    self._update(
                        probed_hosts=done,
                        progress_percent=percent,
                    )

                reachable = self._sweep(
                    target,
                    cancel_event,
                    on_progress,
                )
                for member in group:
                    self._append_results(match_local_target_multi(member, suffixes))
                    try:
                        records = self._neighbors(member)
                    except (CommandError, OSError) as exc:
                        records = []
                        with self._lock:
                            self._state["warnings"] = list(
                                self._state["warnings"]
                            ) + [f"{member.interface} 邻居表读取失败：{exc}"]
                    self._append_results(
                        match_neighbor_records_multi(
                            records,
                            suffixes,
                            member.scan_range,
                            reachable,
                        )
                    )
                probed_before += target.host_count

            if cancel_event.is_set():
                raise ScanCancelled("scan stopped")
            self._update(
                status="completed",
                running=False,
                stopping=False,
                probed_hosts=total,
                progress_percent=100,
                current_interface="",
                current_range="",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        except ScanCancelled:
            self._update(
                status="stopped",
                running=False,
                stopping=False,
                current_interface="",
                current_range="",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:
            self._update(
                status="error",
                running=False,
                stopping=False,
                current_interface="",
                current_range="",
                error=str(exc),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        finally:
            with self._lock:
                if self._state["job_id"] == job_id:
                    self._state["running"] = False


def max_configured_hosts() -> int:
    return max(1, _safe_int(os.environ.get("SCAN_MAX_HOSTS"), DEFAULT_MAX_HOSTS))
