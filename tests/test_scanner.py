"""Inventory scan lifecycle tests with deterministic network adapters."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from network_locator.core import NetworkTarget, NeighborRecord, ScanCancelled
from network_locator.scanner import InventoryScanner, ScanScopeError, parse_custom_ranges
from network_locator.store import NetworkStore


def target() -> NetworkTarget:
    return NetworkTarget(
        interface="Ethernet",
        alias="Ethernet",
        local_ip="192.0.2.5",
        prefix_length=24,
        gateway="192.0.2.1",
        network="192.0.2.0",
        broadcast="192.0.2.255",
        scan_range="192.0.2.0/24",
        host_count=254,
        local_source="192.0.2.5",
        local_mac="02-42-AC-11-00-05",
        interface_index=1,
        interface_id="Ethernet|1",
    )


def second_target() -> NetworkTarget:
    return NetworkTarget(
        interface="Ethernet 2",
        alias="Ethernet 2",
        local_ip="198.51.100.5",
        prefix_length=24,
        gateway="198.51.100.1",
        network="198.51.100.0",
        broadcast="198.51.100.255",
        scan_range="198.51.100.0/24",
        host_count=254,
        local_source="198.51.100.5",
        local_mac="02-42-AC-11-00-55",
        interface_index=2,
        interface_id="Ethernet 2|2",
    )


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = NetworkStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_custom_ranges_parse_and_reject_unsafe_scopes(self) -> None:
        parsed = parse_custom_ranges(["192.0.2.0/30", "198.51.100.10-198.51.100.12", "203.0.113.9"])
        self.assertEqual([item["host_count"] for item in parsed], [2, 3, 1])
        with self.assertRaises(ScanScopeError):
            parse_custom_ranges(["10.0.0.0/8"], max_hosts=1022)
        with self.assertRaises(ScanScopeError):
            parse_custom_ranges(["not-an-ip"])

    def test_scan_persists_devices_and_results(self) -> None:
        def sweep(current, cancel, progress=None, **kwargs):
            if progress:
                progress(254)
            return {"192.0.2.10"}

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([target()], []),
            neighbors=lambda current: [
                NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE", 1),
                NeighborRecord("Ethernet", "192.0.2.11", "02-42-AC-11-00-11", "STALE", 1),
            ],
            sweep=sweep,
            auto_schedule=False,
        )
        scanner.start(None, [])
        scanner.wait(5)
        status = scanner.status()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["progress_percent"], 100)
        devices = {item["current_ip"]: item for item in self.store.list_devices()}
        self.assertEqual(devices["192.0.2.10"]["last_state"], "REACHABLE")
        self.assertEqual(devices["192.0.2.11"]["last_state"], "STALE")
        self.assertEqual(devices["192.0.2.5"]["last_state"], "LOCAL")

    def test_broadcast_and_multicast_arp_rows_do_not_enter_inventory(self) -> None:
        def sweep(current, cancel, progress=None, **kwargs):
            if progress:
                progress(254)
            return {"192.0.2.10", "192.0.2.255"}

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([target()], []),
            neighbors=lambda current: [
                NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE", 1),
                NeighborRecord("Ethernet", "192.0.2.255", "FF-FF-FF-FF-FF-FF", "STALE", 1),
                NeighborRecord("Ethernet", "192.0.2.20", "01-00-5E-00-00-01", "STALE", 1),
                NeighborRecord("Ethernet", "192.0.2.21", "00-00-00-00-00-00", "STALE", 1),
            ],
            sweep=sweep,
            auto_schedule=False,
        )
        scanner.start(None, [])
        scanner.wait(5)
        status = scanner.status()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(
            {item["ip"] for item in status["results"]},
            {"192.0.2.5", "192.0.2.10"},
        )
        self.assertEqual(
            {item["current_ip"] for item in self.store.list_devices()},
            {"192.0.2.5", "192.0.2.10"},
        )

    def test_pause_resume_and_stop_are_visible_in_status(self) -> None:
        gate = threading.Event()

        def sweep(current, cancel, progress=None, **kwargs):
            gate.wait(3)
            return set()

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([target()], []),
            neighbors=lambda current: [],
            sweep=sweep,
            auto_schedule=False,
        )
        scanner.start(None, [])
        time.sleep(0.05)
        self.assertTrue(scanner.pause())
        self.assertTrue(scanner.status()["paused"])
        self.assertTrue(scanner.resume())
        self.assertFalse(scanner.status()["paused"])
        self.assertTrue(scanner.stop())
        gate.set()
        scanner.wait(4)
        self.assertEqual(scanner.status()["status"], "stopped")

    def test_custom_range_creates_reachable_device_without_arp(self) -> None:
        def custom_sweep(hosts, cancel, progress=None, **kwargs):
            if progress:
                progress(len(hosts))
            return {"198.51.100.10"}

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([], []),
            neighbors=lambda current: [],
            sweep=lambda *args, **kwargs: set(),
            custom_sweep=custom_sweep,
            auto_schedule=False,
        )
        scanner.start([], ["198.51.100.10-198.51.100.12"])
        scanner.wait(5)
        status = scanner.status()
        self.assertEqual(status["status"], "completed")
        devices = self.store.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["current_ip"], "198.51.100.10")
        self.assertEqual(devices[0]["current_interface"], "自定义范围")

    def test_partial_segment_scan_does_not_mark_uncovered_devices_offline(self) -> None:
        first = target()
        second = second_target()
        self.store.create_scan_task()
        task_id = self.store.list_scan_tasks(1)[0]["task_id"]
        self.store.ingest_scan_results(
            task_id,
            [
                {
                    "ip": "192.0.2.10",
                    "mac": "02-42-AC-11-00-10",
                    "state": "REACHABLE",
                    "interface": "Ethernet",
                    "interface_id": "Ethernet|1",
                    "scan_range": "192.0.2.0/24",
                }
            ],
        )
        self.store.finish_scan_task(task_id, status="completed", total_hosts=254, probed_hosts=254)

        def sweep(current, cancel, progress=None, **kwargs):
            del cancel, progress, kwargs
            return {"198.51.100.10"} if current.scan_range == second.scan_range else set()

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([first, second], []),
            neighbors=lambda current: [],
            sweep=sweep,
            auto_schedule=False,
        )
        scanner.start(["Ethernet 2|198.51.100.0/24"], [])
        scanner.wait(5)
        self.assertEqual(scanner.status()["status"], "completed")
        devices = {item["current_ip"]: item for item in self.store.list_devices()}
        self.assertIn("192.0.2.10", devices)
        self.assertNotEqual(devices["192.0.2.10"]["status"], "offline")

    def test_multi_segment_scan_covers_every_selected_network(self) -> None:
        first = target()
        second = second_target()
        swept = []

        def sweep(current, cancel, progress=None, **kwargs):
            del cancel, kwargs
            swept.append(current.scan_range)
            if progress:
                progress(current.host_count)
            return {current.local_ip}

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([first, second], []),
            neighbors=lambda current: [],
            sweep=sweep,
            auto_schedule=False,
        )
        scanner.start([first.interface + "|" + first.scan_range, second.interface + "|" + second.scan_range], [])
        scanner.wait(5)
        self.assertEqual(scanner.status()["status"], "completed")
        self.assertEqual(set(swept), {first.scan_range, second.scan_range})
        ranges = {item["current_scan_range"] for item in self.store.list_devices()}
        self.assertEqual(ranges, {first.scan_range, second.scan_range})

    def test_empty_scan_scope_is_rejected_before_start(self) -> None:
        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([target()], []),
            neighbors=lambda current: [],
            sweep=lambda *args, **kwargs: set(),
            auto_schedule=False,
        )
        with self.assertRaises(ScanScopeError):
            scanner.start([], [])

    def test_second_start_is_rejected_while_running(self) -> None:
        gate = threading.Event()

        def sweep(current, cancel, progress=None, **kwargs):
            gate.wait(2)
            return set()

        scanner = InventoryScanner(
            self.store,
            discover=lambda: ([target()], []),
            neighbors=lambda current: [],
            sweep=sweep,
            auto_schedule=False,
        )
        scanner.start(None, [])
        with self.assertRaises(ScanScopeError):
            scanner.start(None, [])
        scanner.stop()
        gate.set()
        scanner.wait(3)


if __name__ == "__main__":
    unittest.main()
