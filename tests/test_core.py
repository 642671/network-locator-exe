"""Unit tests for matching, discovery adapters, and scan state."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from network_locator import __version__
from network_locator.core import (
    NetworkTarget,
    NeighborRecord,
    ScanCancelled,
    ScanManager,
    collect_neighbor_devices,
    match_neighbor_records_multi,
    network_target_key,
    network_target_payload,
    normalize_mac,
    normalize_mac_suffix,
    normalize_mac_suffixes,
    normalize_network_ids,
    probe_network_devices,
    select_network_targets,
)
from network_locator.win_network import discover_networks, parse_arp_output


def make_target(**overrides) -> NetworkTarget:
    values = {
        "interface": "Ethernet",
        "alias": "Ethernet",
        "local_ip": "192.0.2.5",
        "prefix_length": 24,
        "gateway": "192.0.2.1",
        "network": "192.0.2.0",
        "broadcast": "192.0.2.255",
        "scan_range": "192.0.2.0/24",
        "host_count": 254,
        "local_source": "192.0.2.5",
        "local_mac": "02-42-AC-11-00-05",
        "interface_index": 7,
    }
    values.update(overrides)
    return NetworkTarget(**values)


class NormalizationTests(unittest.TestCase):
    def test_version_is_reported(self) -> None:
        self.assertEqual(__version__, "1.3.10")

    def test_mac_forms_normalize_to_dashes(self) -> None:
        self.assertEqual(normalize_mac("02:42:ac:11:00:05"), "02-42-AC-11-00-05")
        self.assertIsNone(normalize_mac("not-a-mac"))

    def test_suffix_forms(self) -> None:
        self.assertEqual(normalize_mac_suffix("aa-bb"), "AABB")
        self.assertEqual(normalize_mac_suffix("AA:BB"), "AABB")
        self.assertEqual(normalize_mac_suffix("aabb"), "AABB")
        self.assertIsNone(normalize_mac_suffix("A"))
        self.assertEqual(normalize_mac_suffixes(["AA-BB", "aabb"]), ["AABB"])

    def test_network_ids_validation(self) -> None:
        self.assertIsNone(normalize_network_ids(None))
        self.assertEqual(normalize_network_ids(["a|b", "a|b "]), ["a|b"])
        with self.assertRaises(ValueError):
            normalize_network_ids([])
        with self.assertRaises(ValueError):
            normalize_network_ids("a|b")


class DiscoveryAdapterTests(unittest.TestCase):
    def test_discover_networks_uses_adapter_reader(self) -> None:
        entries = [
            {
                "interface_alias": "Ethernet",
                "interface_index": 5,
                "interface_description": "Realtek PCIe GbE Family Controller",
                "local_ip": "192.0.2.5",
                "prefix_length": 24,
                "gateway": "192.0.2.1",
                "local_mac": "02-42-AC-11-00-05",
            },
            {
                "interface_alias": "vEthernet (WSL)",
                "interface_index": 22,
                "interface_description": "Hyper-V Virtual Ethernet Adapter",
                "local_ip": "172.20.0.1",
                "prefix_length": 24,
                "gateway": "172.20.0.1",
                "local_mac": "00-15-5D-00-00-01",
            },
        ]
        targets, warnings = discover_networks(adapter_reader=lambda: entries)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].interface, "Ethernet")
        self.assertEqual(targets[0].scan_range, "192.0.2.0/24")
        self.assertEqual(targets[0].local_source, "192.0.2.5")
        self.assertTrue(any("虚拟网卡" in warning for warning in warnings))

    def test_discover_networks_rejects_oversized_network(self) -> None:
        entries = [
            {
                "interface_alias": "Ethernet",
                "interface_index": 1,
                "interface_description": "Intel Ethernet",
                "local_ip": "10.0.0.5",
                "prefix_length": 8,
                "gateway": "10.0.0.1",
                "local_mac": "",
            }
        ]
        targets, warnings = discover_networks(adapter_reader=lambda: entries, max_hosts=1022)
        self.assertEqual(targets, [])
        self.assertTrue(any("安全上限" in warning for warning in warnings))

    def test_virtual_adapter_is_used_when_it_is_the_only_path(self) -> None:
        entries = [
            {
                "interface_alias": "vEthernet (Default Switch)",
                "interface_index": 22,
                "interface_description": "Hyper-V Virtual Ethernet Adapter",
                "local_ip": "172.20.0.5",
                "prefix_length": 24,
                "gateway": "172.20.0.1",
                "local_mac": "00-15-5D-00-00-01",
            }
        ]
        targets, warnings = discover_networks(adapter_reader=lambda: entries)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].interface, "vEthernet (Default Switch)")
        self.assertTrue(any("仅检测到虚拟网卡" in warning for warning in warnings))

    def test_network_payload_includes_stable_id(self) -> None:
        target = make_target()
        payload = network_target_payload(target)
        self.assertEqual(payload["network_id"], "Ethernet|192.0.2.0/24")
        self.assertEqual(network_target_key(target), payload["network_id"])
        self.assertEqual(payload["alias"], "Ethernet")
        self.assertEqual(payload["local_source"], "192.0.2.5")
        self.assertEqual(payload["interface_index"], 7)

    def test_select_network_targets_reports_missing(self) -> None:
        first = make_target()
        second = make_target(interface="Wi-Fi", scan_range="198.51.100.0/24")
        selected, missing = select_network_targets(
            [first, second],
            ["Ethernet|192.0.2.0/24", "Nope|10.0.0.0/8"],
        )
        self.assertEqual(selected, [first])
        self.assertEqual(missing, ["Nope|10.0.0.0/8"])


class ArpParsingTests(unittest.TestCase):
    def test_parse_arp_output_handles_windows_table(self) -> None:
        raw = (
            "接口: 192.0.2.5 --- 0x7\r\n"
            "  Internet 地址         物理地址              类型\r\n"
            "  192.0.2.1             02-42-ac-11-00-01     动态\r\n"
            "  192.0.2.10            02-42-ac-11-00-10     动态\r\n"
            "  192.0.2.11            02-42-ac-22-00-11     静态\r\n"
            "  192.0.2.255           ff-ff-ff-ff-ff-ff     静态\r\n"
        )
        records = parse_arp_output(raw, make_target())
        by_ip = {record.ip: record for record in records}
        self.assertEqual(by_ip["192.0.2.10"].mac, "02-42-AC-11-00-10")
        self.assertEqual(by_ip["192.0.2.10"].state, "STALE")
        self.assertEqual(by_ip["192.0.2.11"].state, "REACHABLE")
        self.assertEqual(by_ip["192.0.2.1"].interface, "Ethernet")


class MatchingTests(unittest.TestCase):
    def test_multi_suffix_matching_annotates_each_device(self) -> None:
        records = [
            NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE"),
            NeighborRecord("Ethernet", "192.0.2.11", "02-42-AC-22-AC-11", "STALE"),
            NeighborRecord("Ethernet", "192.0.2.12", "02-42-AC-22-00-12", "STALE"),
        ]
        matches = match_neighbor_records_multi(
            records,
            ["AC11", "0010"],
            "192.0.2.0/24",
            {"192.0.2.10"},
        )
        by_ip = {item["ip"]: item for item in matches}
        self.assertEqual(set(by_ip), {"192.0.2.10", "192.0.2.11"})
        self.assertEqual(by_ip["192.0.2.10"]["matched_suffixes"], ["0010"])
        self.assertEqual(by_ip["192.0.2.10"]["state"], "REACHABLE")
        self.assertEqual(by_ip["192.0.2.11"]["matched_suffixes"], ["AC11"])
        self.assertEqual(by_ip["192.0.2.11"]["state"], "STALE")

    def test_unreachable_match_stays_stale(self) -> None:
        records = [
            NeighborRecord("Ethernet", "192.0.2.12", "02-42-AC-11-00-12", "STALE"),
        ]
        matches = match_neighbor_records_multi(
            records,
            "0012",
            "192.0.2.0/24",
            set(),
        )
        self.assertEqual(matches[0]["state"], "STALE")


class CollectAndProbeTests(unittest.TestCase):
    def test_collect_devices_marks_local_and_filters_foreign_networks(self) -> None:
        target = make_target()

        def neighbors(current):
            self.assertIs(current, target)
            return [
                NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE"),
                NeighborRecord("Ethernet", "198.51.100.9", "02-42-AC-11-00-99", "STALE"),
            ]

        devices, warnings = collect_neighbor_devices([target], neighbors)
        self.assertEqual(warnings, [])
        self.assertEqual([item["ip"] for item in devices], ["192.0.2.5", "192.0.2.10"])
        self.assertEqual(devices[0]["state"], "LOCAL")

    def test_probe_marks_ping_responders_reachable(self) -> None:
        target = make_target()

        def sweep(current, cancel, progress=None):
            if progress:
                progress(3)
            return {"192.0.2.10"}

        def neighbors(current):
            return [
                NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE"),
                NeighborRecord("Ethernet", "192.0.2.12", "02-42-AC-11-00-12", "STALE"),
            ]

        devices, warnings = probe_network_devices(
            [target],
            sweep=sweep,
            neighbors=neighbors,
        )
        self.assertEqual(warnings, [])
        states = {item["ip"]: item["state"] for item in devices}
        self.assertEqual(states["192.0.2.10"], "REACHABLE")
        self.assertEqual(states["192.0.2.12"], "STALE")


class ScanManagerTests(unittest.TestCase):
    def test_scan_completes_and_merges_results(self) -> None:
        target = make_target(local_mac="02-42-AC-11-AA-BB")
        progress_seen: list[int] = []

        def discover():
            return [target], []

        def sweep(current, cancel, progress=None):
            if progress:
                progress(2)
                progress_seen.append(2)
            return {"192.0.2.10"}

        def neighbors(current):
            return [
                NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE"),
                NeighborRecord("Ethernet", "192.0.2.11", "02-42-AC-22-00-11", "STALE"),
            ]

        manager = ScanManager(discover=discover, neighbors=neighbors, sweep=sweep)
        self.assertTrue(manager.start(["AABB", "0010"]))
        manager.wait(5)
        status = manager.status()
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["running"])
        self.assertEqual(status["progress_percent"], 100)
        self.assertEqual(progress_seen, [2])
        rows = {(item["ip"], item["mac"]): item for item in status["results"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[("192.0.2.5", "02-42-AC-11-AA-BB")]["state"], "LOCAL")
        self.assertEqual(rows[("192.0.2.10", "02-42-AC-11-00-10")]["state"], "REACHABLE")

    def test_scan_stop_raises_and_reports_stopped(self) -> None:
        target = make_target()

        def discover():
            return [target], []

        def sweep(current, cancel, progress=None):
            for _ in range(200):
                if cancel.is_set():
                    raise ScanCancelled("scan stopped")
                time.sleep(0.01)
            return set()

        manager = ScanManager(
            discover=discover,
            neighbors=lambda current: [],
            sweep=sweep,
        )
        self.assertTrue(manager.start("AA-BB"))
        time.sleep(0.1)
        self.assertTrue(manager.stop())
        manager.wait(3)
        status = manager.status()
        self.assertEqual(status["status"], "stopped")
        self.assertFalse(status["running"])
        self.assertFalse(status["stopping"])

    def test_matches_request_compares_suffixes_and_networks(self) -> None:
        manager = ScanManager(
            discover=lambda: ([make_target()], []),
            neighbors=lambda current: [],
            sweep=lambda current, cancel, progress=None: set(),
        )
        self.assertTrue(manager.start("AA-BB", ["Ethernet|192.0.2.0/24"]))
        manager.wait(3)
        self.assertTrue(manager.matches_request("aabb", ["Ethernet|192.0.2.0/24"]))
        self.assertFalse(manager.matches_request("CCDD", ["Ethernet|192.0.2.0/24"]))
        self.assertFalse(manager.matches_request("AABB"))

    def test_second_start_is_rejected_while_running(self) -> None:
        target = make_target()
        gate = threading.Event()

        def sweep(current, cancel, progress=None):
            gate.wait(2)
            return set()

        manager = ScanManager(
            discover=lambda: ([target], []),
            neighbors=lambda current: [],
            sweep=sweep,
        )
        self.assertTrue(manager.start("AA-BB"))
        self.assertFalse(manager.start("CC-DD"))
        gate.set()
        manager.wait(3)


if __name__ == "__main__":
    unittest.main()