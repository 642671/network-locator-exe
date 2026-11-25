"""Windows adapter discovery, ARP neighbor lookup, and bounded ping sweeps."""

from __future__ import annotations

import base64
import concurrent.futures
import ctypes
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Callable, Iterable

from .core import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_HOSTS,
    CommandError,
    NetworkTarget,
    NeighborRecord,
    ScanCancelled,
    is_scannable_local_address,
    is_virtual_interface,
    network_host_count,
    network_hosts,
    normalize_device_mac,
    parse_ipv4_network,
)

try:  # pragma: no cover - unavailable outside Windows
    import winreg
except ImportError:  # pragma: no cover
    winreg = None

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _IP_ADDR_STRING(ctypes.Structure):
    pass


_IP_ADDR_STRING._fields_ = [
    ("Next", ctypes.POINTER(_IP_ADDR_STRING)),
    ("IpAddress", ctypes.c_char * 16),
    ("IpMask", ctypes.c_char * 16),
    ("Context", ctypes.c_ulong),
]


class _IP_ADAPTER_INFO(ctypes.Structure):
    pass


_IP_ADAPTER_INFO._fields_ = [
    ("Next", ctypes.POINTER(_IP_ADAPTER_INFO)),
    ("ComboIndex", ctypes.c_ulong),
    # The documented constants include four bytes reserved for a suffix.
    # Omitting them shifts every following field on 64-bit Windows.
    ("AdapterName", ctypes.c_char * 260),
    ("Description", ctypes.c_char * 132),
    ("AddressLength", ctypes.c_uint),
    ("Address", ctypes.c_ubyte * 8),
    ("Index", ctypes.c_ulong),
    ("Type", ctypes.c_uint),
    ("DhcpEnabled", ctypes.c_uint),
    ("CurrentIpAddress", ctypes.POINTER(_IP_ADDR_STRING)),
    ("IpAddressList", _IP_ADDR_STRING),
    ("GatewayList", _IP_ADDR_STRING),
    ("DhcpServer", _IP_ADDR_STRING),
    ("HaveWins", ctypes.c_int),
    ("PrimaryWinsServer", _IP_ADDR_STRING),
    ("SecondaryWinsServer", _IP_ADDR_STRING),
    # time_t is 64-bit in supported 64-bit Windows Python builds.
    ("LeaseObtained", ctypes.c_longlong),
    ("LeaseExpires", ctypes.c_longlong),
]


def _safe_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _decode_output(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16-le", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_powershell(script: str, timeout: float = 20.0) -> str:
    powershell = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if not os.path.isfile(powershell):
        raise CommandError("Windows PowerShell 5.1 is unavailable")
    preamble = (
        "$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
        "$OutputEncoding=[Console]::OutputEncoding;"
    )
    encoded = base64.b64encode((preamble + script).encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            check=False,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(f"PowerShell network query failed: {exc}") from exc
    stdout = _decode_output(result.stdout).strip()
    stderr = _decode_output(result.stderr).strip()
    if result.returncode != 0:
        raise CommandError(stderr or stdout or f"PowerShell exited {result.returncode}")
    return stdout


def _json_items(raw: str, label: str) -> list[dict]:
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(f"{label} returned invalid JSON: {exc}") from exc
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    raise CommandError(f"{label} returned an unexpected JSON value")


def _adapter_friendly_name(adapter_name: str) -> str:
    """Read the Windows connection name for the adapter GUID when available."""

    if not adapter_name or winreg is None:
        return ""
    key_path = (
        r"SYSTEM\CurrentControlSet\Control\Network"
        r"\{4D36E972-E325-11CE-BFC1-08002BE10318}"
        rf"\{adapter_name}\Connection"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "Name")
    except OSError:
        return ""
    return str(value).strip()


def read_windows_adapters() -> list[dict]:
    if os.name != "nt":
        raise CommandError("GetAdaptersInfo is only available on Windows")
    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.GetAdaptersInfo.argtypes = [
        ctypes.POINTER(_IP_ADAPTER_INFO),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    iphlpapi.GetAdaptersInfo.restype = ctypes.c_ulong
    size = ctypes.c_ulong(15 * 1024)
    error_buffer_overflow = 111
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetAdaptersInfo(
            ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_INFO)),
            ctypes.byref(size),
        )
        if result == 0:
            break
        if result != error_buffer_overflow:
            raise CommandError(f"GetAdaptersInfo failed with error {result}")
    else:
        raise CommandError("GetAdaptersInfo buffer sizing failed")

    names: dict[int, str] = {}
    try:
        names = {index: name for index, name in socket.if_nameindex()}
    except OSError:
        pass

    entries: list[dict] = []
    current = ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_INFO))
    while current:
        adapter = current.contents
        description = adapter.Description.decode("mbcs", errors="replace").strip()
        adapter_name = adapter.AdapterName.decode("ascii", errors="ignore").strip()
        alias = (
            _adapter_friendly_name(adapter_name)
            or names.get(int(adapter.Index))
            or description
            or f"Adapter {adapter.Index}"
        )
        mac = "-".join(
            f"{adapter.Address[index]:02X}"
            for index in range(min(int(adapter.AddressLength), 8))
        )
        address = ctypes.pointer(adapter.IpAddressList)
        while address:
            item = address.contents
            local_ip = item.IpAddress.decode("ascii", errors="ignore").strip()
            mask = item.IpMask.decode("ascii", errors="ignore").strip()
            if local_ip and mask:
                try:
                    prefix_length = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                    ipaddress.IPv4Address(local_ip)
                except ValueError:
                    pass
                else:
                    gateway = adapter.GatewayList.IpAddress.decode(
                        "ascii",
                        errors="ignore",
                    ).strip()
                    if gateway == "0.0.0.0":
                        gateway = ""
                    entries.append(
                        {
                            "interface_alias": alias,
                            "interface_index": int(adapter.Index),
                            "interface_description": description,
                            "local_ip": local_ip,
                            "prefix_length": prefix_length,
                            "gateway": gateway,
                            "local_mac": mac,
                        }
                    )
                    break
            address = item.Next
        current = adapter.Next
    return entries


def _powershell_adapter_entries(
    powershell_runner: Callable[[str], str],
) -> list[dict]:
    script = r"""
$rows = Get-NetIPConfiguration | Where-Object {
    $_.IPv4Address -and
    $_.IPv4DefaultGateway
} | ForEach-Object {
    $v4 = $_.IPv4Address | Select-Object -First 1
    $gateway = $_.IPv4DefaultGateway | Select-Object -First 1
    $adapter = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue
    [pscustomobject]@{
        interface_alias = [string]$_.InterfaceAlias
        interface_index = [int]$_.InterfaceIndex
        interface_description = [string]$_.InterfaceDescription
        local_ip = [string]$v4.IPAddress
        prefix_length = [int]$v4.PrefixLength
        gateway = [string]$gateway.NextHop
        local_mac = [string]$adapter.MacAddress
    }
}
ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
"""
    return _json_items(powershell_runner(script), "Get-NetIPConfiguration")


def _powershell_dns_servers(
    powershell_runner: Callable[[str], str],
) -> dict[int, list[str]]:
    script = r"""
$rows = Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.ServerAddresses.Count -gt 0 } |
    ForEach-Object {
        [pscustomobject]@{
            interface_index = [int]$_.InterfaceIndex
            servers = @($_.ServerAddresses | Where-Object { $_ })
        }
    }
ConvertTo-Json -InputObject @($rows) -Depth 3 -Compress
"""
    result: dict[int, list[str]] = {}
    try:
        rows = _json_items(powershell_runner(script), "Get-DnsClientServerAddress")
    except Exception:
        return result
    for row in rows:
        try:
            index = int(row.get("interface_index"))
        except (TypeError, ValueError):
            continue
        servers = row.get("servers")
        if isinstance(servers, str):
            servers = [servers]
        if isinstance(servers, list):
            result[index] = [str(item) for item in servers if str(item).strip()]
    return result


def read_network_interfaces(
    *,
    adapter_reader: Callable[[], list[dict]] | None = None,
    powershell_runner: Callable[[str], str] = run_powershell,
) -> list[dict]:
    """Return all usable IPv4 adapters with DNS, gateway, and segment details."""

    if adapter_reader is not None:
        entries = adapter_reader()
        dns_by_index: dict[int, list[str]] = {}
    else:
        try:
            entries = read_windows_adapters()
            if not entries:
                entries = _powershell_adapter_entries(powershell_runner)
        except Exception:
            entries = _powershell_adapter_entries(powershell_runner)
        dns_by_index = _powershell_dns_servers(powershell_runner)

    interfaces: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        alias = str(entry.get("interface_alias") or "")
        description = str(entry.get("interface_description") or "")
        local_ip = str(entry.get("local_ip") or "").strip()
        try:
            interface_index = int(entry.get("interface_index") or 0)
            prefix_length = int(entry.get("prefix_length") or 0)
            address = ipaddress.IPv4Address(local_ip)
            if address.is_loopback or address.is_unspecified or not is_scannable_local_address(local_ip):
                continue
            network = parse_ipv4_network(local_ip, prefix_length)
        except (TypeError, ValueError, CommandError):
            continue
        name = alias or f"Adapter {interface_index}"
        interface_id = str(entry.get("interface_id") or "") or f"{name}|{interface_index}"
        seen_key = (interface_id, str(network))
        if seen_key in seen:
            continue
        seen.add(seen_key)
        is_virtual = is_virtual_interface(name) or _is_virtual_description(description)
        interfaces.append(
            {
                "interface_id": interface_id,
                "interface_index": interface_index,
                "alias": name,
                "description": description,
                "local_ip": local_ip,
                "prefix_length": network.prefixlen,
                "network": str(network.network_address),
                "broadcast": str(network.broadcast_address),
                "scan_range": str(network),
                "segment_id": f"{interface_id}|{network}",
                "gateway": str(entry.get("gateway") or ""),
                "dns_servers": list(dns_by_index.get(interface_index, [])),
                "local_mac": normalize_device_mac(str(entry.get("local_mac") or "")) or "",
                "host_count": network_host_count(network),
                "is_virtual": is_virtual,
                "is_up": bool(entry.get("is_up", True)),
            }
        )
    interfaces.sort(key=lambda item: (item["is_virtual"], item["interface_index"], item["scan_range"]))
    return interfaces


VIRTUAL_DESCRIPTION_MARKERS = ("loopback", "bluetooth", "host-only", "vmnet")


def _is_virtual_description(description: str) -> bool:
    text = str(description or "").strip().lower()
    return bool(text) and any(marker in text for marker in VIRTUAL_DESCRIPTION_MARKERS)


def discover_networks(
    *,
    adapter_reader: Callable[[], list[dict]] | None = None,
    adapter_entries: list[dict] | None = None,
    powershell_runner: Callable[[str], str] = run_powershell,
    max_hosts: int | None = None,
) -> tuple[list[NetworkTarget], list[str]]:
    """Discover routed physical IPv4 networks without scanning them yet."""

    if adapter_entries is not None:
        entries = adapter_entries
    elif adapter_reader is not None:
        entries = adapter_reader()
    else:
        try:
            entries = read_windows_adapters()
            if not entries:
                entries = _powershell_adapter_entries(powershell_runner)
        except Exception:
            entries = _powershell_adapter_entries(powershell_runner)
    limit = max(
        1,
        int(max_hosts or os.environ.get("SCAN_MAX_HOSTS", DEFAULT_MAX_HOSTS)),
    )
    targets: list[NetworkTarget] = []
    virtual_targets: list[NetworkTarget] = []
    virtual_names: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[int, str]] = set()

    for entry in entries:
        alias = str(entry.get("interface_alias") or "")
        description = str(entry.get("interface_description") or "")
        is_virtual = is_virtual_interface(alias) or _is_virtual_description(description)
        try:
            interface_index = int(entry["interface_index"])
            local_ip = str(entry["local_ip"])
            prefix_length = int(entry["prefix_length"])
            if not is_scannable_local_address(local_ip):
                warnings.append(
                    f"{alias or 'unknown adapter'} 的 IPv4 地址 {local_ip} 不可扫描，已跳过"
                )
                continue
            network = parse_ipv4_network(local_ip, prefix_length)
        except (KeyError, TypeError, ValueError, CommandError) as exc:
            warnings.append(f"{alias or 'unknown adapter'}: {exc}")
            continue
        key = (interface_index, str(network))
        if key in seen:
            continue
        host_count = network_host_count(network)
        if host_count > limit:
            warnings.append(
                f"{alias} 的 {network} 包含 {host_count} 台主机，"
                f"超过安全上限 {limit}，已跳过"
            )
            continue
        seen.add(key)
        name = alias or f"Adapter {interface_index}"
        interface_id = str(entry.get("interface_id") or "") or f"{name}|{interface_index}"
        target = NetworkTarget(
            interface=name,
            alias=name,
            local_ip=local_ip,
            prefix_length=network.prefixlen,
            gateway=str(entry.get("gateway") or ""),
            network=str(network.network_address),
            broadcast=str(network.broadcast_address),
            scan_range=str(network),
            host_count=host_count,
            local_source=local_ip,
            local_mac=normalize_device_mac(str(entry.get("local_mac") or "")) or "",
            interface_index=interface_index,
            interface_id=interface_id,
        )
        if is_virtual:
            virtual_targets.append(target)
            if name not in virtual_names:
                virtual_names.append(name)
        else:
            targets.append(target)

    if not targets and virtual_targets:
        targets = virtual_targets
        for name in virtual_names:
            warnings.append(f"仅检测到虚拟网卡 {name}，已作为可用网段")
    else:
        for name in virtual_names:
            warnings.append(f"已跳过虚拟网卡 {name}")
    targets.sort(key=lambda item: (item.interface, item.scan_range))
    if not targets and not warnings:
        warnings.append("未找到带 IPv4 默认网关的物理网卡")
    if not entries:
        warnings.append("未读取到网卡信息（GetAdaptersInfo 与 Get-NetIPConfiguration 均为空）")
    return targets, warnings


ARP_LINE_PATTERN = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})\s+(\S+)",
    re.MULTILINE,
)
ARP_TYPE_LABELS = {
    "dynamic": "STALE",
    "static": "REACHABLE",
    "动态": "STALE",
    "静态": "REACHABLE",
}


def parse_arp_output(raw: str, target: NetworkTarget) -> list[NeighborRecord]:
    """Parse `arp -a` output into neighbor records.

    Windows does not expose NUD states, so dynamic entries are reported as
    STALE and static entries as REACHABLE. An active sweep upgrades entries
    that answered ping to REACHABLE.
    """

    records: list[NeighborRecord] = []
    seen: set[tuple[str, str]] = set()
    for match in ARP_LINE_PATTERN.finditer(raw):
        ip_value, raw_mac, raw_type = match.groups()
        mac = normalize_device_mac(raw_mac)
        if mac is None:
            continue
        try:
            address = ipaddress.ip_address(ip_value)
        except ValueError:
            continue
        if not isinstance(address, ipaddress.IPv4Address):
            continue
        key = (str(address), mac)
        if key in seen:
            continue
        seen.add(key)
        state = ARP_TYPE_LABELS.get(raw_type.strip().lower(), "STALE")
        records.append(
            NeighborRecord(
                interface=target.interface,
                ip=str(address),
                mac=mac,
                state=state,
                interface_index=target.interface_index,
            )
        )
    return records


def read_neighbors(target: NetworkTarget) -> list[NeighborRecord]:
    """Return the Windows ARP cache for one adapter address."""

    arp = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "arp.exe",
    )
    if not os.path.isfile(arp):
        raise CommandError("Windows arp.exe is unavailable")
    try:
        result = subprocess.run(
            [arp, "-a", "-N", target.local_ip],
            capture_output=True,
            check=False,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(f"arp.exe failed: {exc}") from exc
    if result.returncode != 0:
        detail = _decode_output(result.stderr or result.stdout).strip()
        raise CommandError(detail or f"arp.exe exited {result.returncode}")
    return parse_arp_output(_decode_output(result.stdout), target)


def resolve_ping_command() -> str | None:
    ping = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "ping.exe",
    )
    if os.path.isfile(ping):
        return ping
    return shutil.which("ping")


def prime_neighbor_cache(hosts: Iterable[ipaddress.IPv4Address]) -> None:
    """Send empty UDP datagrams so Windows resolves local ARP entries."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for host in hosts:
            try:
                sock.sendto(b"", (str(host), 9))
            except OSError:
                continue
    finally:
        sock.close()


def _ping_host(
    ping_command: str,
    address: ipaddress.IPv4Address,
    timeout_ms: int,
) -> bool:
    wait_seconds = max(1, int(math.ceil(timeout_ms / 1000)))
    try:
        result = subprocess.run(
            [ping_command, "-n", "1", "-w", str(timeout_ms), str(address)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=wait_seconds + 1.5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _wait_while_paused(
    pause_event: threading.Event | None,
    cancel_event: threading.Event,
) -> None:
    while pause_event is not None and pause_event.is_set():
        if cancel_event.is_set():
            raise ScanCancelled("scan stopped")
        time.sleep(0.15)


def _scan_ipv4_addresses(
    hosts: list[ipaddress.IPv4Address],
    cancel_event: threading.Event,
    progress: Callable[[int], None] | None,
    *,
    timeout_ms: int,
    batch_size: int,
    ping_command: str | None,
    pause_event: threading.Event | None,
    primer: Callable[[Iterable[ipaddress.IPv4Address]], None],
) -> set[str]:
    reachable: set[str] = set()
    completed = 0
    for start in range(0, len(hosts), batch_size):
        if cancel_event.is_set():
            raise ScanCancelled("scan stopped")
        _wait_while_paused(pause_event, cancel_event)
        if cancel_event.is_set():
            raise ScanCancelled("scan stopped")
        batch = hosts[start : start + batch_size]
        try:
            primer(batch)
        except OSError:
            pass
        if ping_command:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(batch),
                thread_name_prefix="network-locator-ping",
            ) as executor:
                futures = {
                    executor.submit(
                        _ping_host,
                        ping_command,
                        address,
                        timeout_ms,
                    ): address
                    for address in batch
                }
                for future in concurrent.futures.as_completed(futures):
                    if cancel_event.is_set():
                        for pending in futures:
                            pending.cancel()
                        raise ScanCancelled("scan stopped")
                    try:
                        if future.result():
                            reachable.add(str(futures[future]))
                    except Exception:
                        continue
        completed += len(batch)
        if progress:
            progress(completed)
    return reachable


def scan_hosts(
    target: NetworkTarget,
    cancel_event: threading.Event,
    progress: Callable[[int], None] | None = None,
    *,
    timeout_ms: int | None = None,
    concurrency: int | None = None,
    ping_command: str | None = None,
    pause_event: threading.Event | None = None,
    primer: Callable[[Iterable[ipaddress.IPv4Address]], None] = prime_neighbor_cache,
) -> set[str]:
    """Ping every host in a bounded network and return the responders."""

    network = parse_ipv4_network(target.local_ip, target.prefix_length)
    hosts = [host for host in network_hosts(network) if str(host) != target.local_ip]
    configured_timeout = (
        _safe_int(os.environ.get("SCAN_PING_TIMEOUT_MS"), 900)
        if timeout_ms is None
        else int(timeout_ms)
    )
    batch_size = max(
        1,
        min(
            128,
            _safe_int(os.environ.get("SCAN_CONCURRENCY"), DEFAULT_CONCURRENCY)
            if concurrency is None
            else int(concurrency),
        ),
    )
    ping_path = resolve_ping_command() if ping_command is None else ping_command
    return _scan_ipv4_addresses(
        hosts,
        cancel_event,
        progress,
        timeout_ms=configured_timeout,
        batch_size=batch_size,
        ping_command=ping_path,
        pause_event=pause_event,
        primer=primer,
    )


def scan_ip_addresses(
    addresses: Iterable[str | ipaddress.IPv4Address],
    cancel_event: threading.Event,
    progress: Callable[[int], None] | None = None,
    *,
    timeout_ms: int | None = None,
    concurrency: int | None = None,
    ping_command: str | None = None,
    pause_event: threading.Event | None = None,
    primer: Callable[[Iterable[ipaddress.IPv4Address]], None] = prime_neighbor_cache,
) -> set[str]:
    """Ping an explicit list of IPv4 addresses (used by custom ranges)."""

    hosts: list[ipaddress.IPv4Address] = []
    for value in addresses:
        try:
            address = ipaddress.IPv4Address(str(value))
        except ValueError:
            continue
        if address.is_loopback or address.is_multicast or address.is_unspecified:
            continue
        hosts.append(address)
    configured_timeout = (
        _safe_int(os.environ.get("SCAN_PING_TIMEOUT_MS"), 900)
        if timeout_ms is None
        else int(timeout_ms)
    )
    batch_size = max(
        1,
        min(
            128,
            _safe_int(os.environ.get("SCAN_CONCURRENCY"), DEFAULT_CONCURRENCY)
            if concurrency is None
            else int(concurrency),
        ),
    )
    ping_path = resolve_ping_command() if ping_command is None else ping_command
    return _scan_ipv4_addresses(
        hosts,
        cancel_event,
        progress,
        timeout_ms=configured_timeout,
        batch_size=batch_size,
        ping_command=ping_path,
        pause_event=pause_event,
        primer=primer,
    )


PING_LATENCY_PATTERN = re.compile(
    r"(?:time|时间|時間)\s*[=<]\s*(\d+(?:\.\d+)?)\s*ms",
    re.IGNORECASE,
)


def ping_statistics(
    address: str,
    count: int = 4,
    *,
    timeout_ms: int = 1000,
    ping_command: str | None = None,
) -> dict:
    """Return real ICMP latency and loss statistics using the system ping tool."""

    try:
        target = str(ipaddress.IPv4Address(str(address).strip()))
    except ValueError as exc:
        raise CommandError(f"无效的 IPv4 地址：{address}") from exc
    attempts = max(1, min(10, int(count)))
    command = resolve_ping_command() if ping_command is None else ping_command
    result = {
        "address": target,
        "sent": attempts,
        "received": 0,
        "packet_loss_percent": 100.0,
        "min_ms": None,
        "avg_ms": None,
        "max_ms": None,
        "duration_ms": 0,
        "samples": [],
        "command_available": bool(command),
        "error": "",
    }
    if not command:
        result["error"] = "系统 ping.exe 不可用"
        return result
    latencies: list[float] = []
    started = time.perf_counter()
    for sequence in range(1, attempts + 1):
        sample = {"sequence": sequence, "success": False, "latency_ms": None}
        try:
            completed = subprocess.run(
                [command, "-n", "1", "-w", str(max(100, int(timeout_ms))), target],
                capture_output=True,
                check=False,
                timeout=max(2.0, timeout_ms / 1000 + 2.0),
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            sample["error"] = str(exc)
            result["samples"].append(sample)
            continue
        output = _decode_output(completed.stdout)
        matches = [float(value) for value in PING_LATENCY_PATTERN.findall(output)]
        if completed.returncode == 0 and matches:
            latency = min(matches)
            sample["success"] = True
            sample["latency_ms"] = round(latency, 3)
            latencies.append(latency)
        result["samples"].append(sample)
        time.sleep(0.08)
    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    result["received"] = len(latencies)
    result["packet_loss_percent"] = round((attempts - len(latencies)) * 100 / attempts, 1)
    if latencies:
        result["min_ms"] = round(min(latencies), 3)
        result["avg_ms"] = round(sum(latencies) / len(latencies), 3)
        result["max_ms"] = round(max(latencies), 3)
    else:
        result["error"] = "未收到 ICMP 响应，主机可能离线或防火墙阻止 Ping"
    return result
