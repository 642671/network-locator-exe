"""HTTP contract tests for the local WebUI server."""

from __future__ import annotations

import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from network_locator import __version__
from network_locator import server as server_module
from network_locator.core import NetworkTarget, NeighborRecord
from network_locator.scanner import InventoryScanner
from network_locator.store import NetworkStore


def make_target() -> NetworkTarget:
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
        local_mac="02-42-AC-11-AA-BB",
        interface_index=7,
    )


class FakeManager:
    def __init__(self) -> None:
        self.running = False
        self.suffixes: list[str] = []
        self.network_ids = None
        self.stopped = False

    def start(self, suffixes, network_ids=None):
        if self.running:
            return False
        self.running = True
        self.suffixes = list(suffixes)
        self.network_ids = network_ids
        return True

    def stop(self):
        if not self.running:
            return False
        self.running = False
        self.stopped = True
        return True

    def status(self):
        return {
            "status": "scanning" if self.running else "idle",
            "running": self.running,
            "job_id": "job-1",
            "suffix": self.suffixes[0] if self.suffixes else "",
            "suffixes": list(self.suffixes),
            "selected_network_ids": list(self.network_ids or []),
            "results": [],
        }

    def matches_request(self, suffixes, network_ids=None):
        return list(suffixes) == self.suffixes and network_ids == self.network_ids

    def wait(self, timeout=None):
        del timeout


class ServerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = make_target()
        self._saved = {
            name: getattr(server_module, name)
            for name in (
                "discover_networks",
                "read_network_interfaces",
                "ping_statistics",
                "collect_neighbor_devices",
                "probe_network_devices",
            )
        }
        server_module.discover_networks = lambda: ([self.target], [])
        server_module.collect_neighbor_devices = lambda targets: (
            [
                {
                    "interface": "Ethernet",
                    "scan_range": "192.0.2.0/24",
                    "ip": "192.0.2.5",
                    "mac": "02-42-AC-11-AA-BB",
                    "state": "LOCAL",
                }
            ],
            [],
        )
        server_module.probe_network_devices = lambda targets: (
            [
                {
                    "interface": "Ethernet",
                    "scan_range": "192.0.2.0/24",
                    "ip": "192.0.2.10",
                    "mac": "02-42-AC-11-00-10",
                    "state": "REACHABLE",
                }
            ],
            [],
        )
        server_module.read_network_interfaces = lambda: [
            {
                "interface_id": "Ethernet|7",
                "interface_index": 7,
                "alias": "Ethernet",
                "description": "Test adapter",
                "local_ip": "192.0.2.5",
                "prefix_length": 24,
                "network": "192.0.2.0",
                "broadcast": "192.0.2.255",
                "scan_range": "192.0.2.0/24",
                "segment_id": "Ethernet|7|192.0.2.0/24",
                "gateway": "192.0.2.1",
                "dns_servers": ["192.0.2.53"],
                "local_mac": "02-42-AC-11-AA-BB",
                "host_count": 254,
                "is_virtual": False,
                "is_up": True,
            }
        ]
        server_module.ping_statistics = lambda address, count=4, timeout_ms=1000: {
            "address": address,
            "sent": count,
            "received": count,
            "packet_loss_percent": 0.0,
            "min_ms": 1.0,
            "avg_ms": 1.2,
            "max_ms": 1.5,
            "duration_ms": 120,
            "samples": [],
            "command_available": True,
            "error": "",
        }
        self.store = NetworkStore(":memory:")
        self.scanner = InventoryScanner(
            self.store,
            discover=lambda: ([self.target], []),
            neighbors=lambda current: [
                NeighborRecord("Ethernet", "192.0.2.10", "02-42-AC-11-00-10", "STALE", 7)
            ],
            sweep=lambda current, cancel, progress=None, **kwargs: (
                progress(254) if progress else None
            ) or {"192.0.2.10"},
            custom_sweep=lambda hosts, cancel, progress=None, **kwargs: set(),
            auto_schedule=False,
        )
        self.server, self.thread, self.url = server_module.start_server(
            0,
            store=self.store,
            inventory_scanner=self.scanner,
            auto_schedule=False,
        )
        self.manager = FakeManager()
        self.server.scan_manager = self.manager

    def tearDown(self) -> None:
        server_module.shutdown_server(self.server, self.thread)
        self.store.close()
        for name, value in self._saved.items():
            setattr(server_module, name, value)

    def get(self, path: str):
        return urllib.request.urlopen(self.url.rstrip("/") + path, timeout=5)

    def post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.url.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    def patch(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.url.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_route_parts_strips_app_and_api_prefixes(self) -> None:
        self.assertEqual(
            server_module.route_parts("/v2/proxy/network-locator/api/health?x=1"),
            ["health"],
        )
        self.assertEqual(server_module.route_parts("/"), [])

    def test_health_reports_app_identity(self) -> None:
        with self.get("/health") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["app"], "network-locator")
        self.assertEqual(payload["version"], __version__)
        self.assertTrue(payload["ok"])

    def test_index_serves_webui_with_version_substitution(self) -> None:
        with self.get("/") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("网络定位", body)
        self.assertNotIn("__APP_VERSION__", body)
        self.assertIn(__version__, body)

    def test_app_js_is_served(self) -> None:
        with self.get("/app.js?v=1") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("API_CANDIDATES", body)
        self.assertNotIn("/v2/proxy/", body.split("var API_CANDIDATES")[1][:400])

    def test_static_traversal_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.get("/..%2f..%2fetc%2fpasswd")
        self.assertEqual(captured.exception.code, 404)

    def test_networks_payload_uses_network_id(self) -> None:
        with self.get("/networks") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["networks"][0]["network_id"], "Ethernet|192.0.2.0/24")

    def test_devices_scan_returns_probe_results(self) -> None:
        with self.post("/devices/scan", {"network_ids": ["Ethernet|192.0.2.0/24"]}) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["devices"][0]["state"], "REACHABLE")

    def test_scan_start_and_stop(self) -> None:
        with self.post(
            "/scan/start",
            {
                "suffixes": ["AA-BB"],
                "network_ids": ["Ethernet|192.0.2.0/24"],
            },
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertTrue(payload["started"])
        self.assertEqual(payload["suffixes"], ["AABB"])

        with self.get("/scan/status") as response:
            status = json.loads(response.read().decode("utf-8"))
        self.assertTrue(status["running"])

        with self.post("/scan/stop", {}) as response:
            stopped = json.loads(response.read().decode("utf-8"))
        self.assertTrue(stopped["stopping"])
        self.assertTrue(self.manager.stopped)

    def test_scan_start_rejects_invalid_suffix(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post("/scan/start", {"suffixes": ["ZZ-ZZ"]})
        self.assertEqual(captured.exception.code, 400)

    def test_unknown_api_path_returns_static_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.get("/does-not-exist.js")
        self.assertEqual(captured.exception.code, 404)
        self.assertEqual(captured.exception.headers["Content-Type"], "application/json; charset=utf-8")

    def test_v2_bootstrap_and_network_metadata(self) -> None:
        with self.get("/api/v2/bootstrap") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app"], "network-locator")
        self.assertEqual(payload["interfaces"][0]["dns_servers"], ["192.0.2.53"])
        self.assertEqual(payload["segments"][0]["network_id"], "Ethernet|192.0.2.0/24")

    def test_v2_scan_device_profile_and_lookup(self) -> None:
        with self.post(
            "/api/v2/scan/start",
            {"network_ids": ["Ethernet|192.0.2.0/24"], "custom_ranges": []},
        ) as response:
            self.assertEqual(response.status, 202)
        deadline = time.time() + 5
        status = {}
        while time.time() < deadline:
            with self.get("/api/v2/scan/status") as response:
                status = json.loads(response.read().decode("utf-8"))["scan"]
            if status["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "completed")
        with self.get("/api/v2/devices") as response:
            devices = json.loads(response.read().decode("utf-8"))["devices"]
        self.assertEqual(len(devices), 2)
        device = next(item for item in devices if item["primary_mac"] == "02-42-AC-11-00-10")
        device_id = device["device_id"]
        with self.patch(
            f"/api/v2/devices/{device_id}",
            {"display_name": "测试主机", "note": "机柜 A", "favorite": True},
        ) as response:
            updated = json.loads(response.read().decode("utf-8"))["device"]
        self.assertEqual(updated["display_name"], "测试主机")
        self.assertTrue(updated["favorite"])
        with self.post("/api/v2/lookup", {"query": "0010"}) as response:
            lookup = json.loads(response.read().decode("utf-8"))
        self.assertEqual(lookup["queries"][0]["matches"][0]["device_id"], device_id)

    def test_v2_history_settings_analysis_and_csv(self) -> None:
        with self.post(
            "/api/v2/scan/start",
            {"network_ids": ["Ethernet|192.0.2.0/24"]},
        ) as response:
            self.assertEqual(response.status, 202)
        self.scanner.wait(5)
        with self.get("/api/v2/history") as response:
            tasks = json.loads(response.read().decode("utf-8"))["tasks"]
        self.assertTrue(tasks)
        task_id = tasks[0]["task_id"]
        with self.get(f"/api/v2/history/{task_id}") as response:
            task = json.loads(response.read().decode("utf-8"))["task"]
        self.assertIn("results", task)
        with self.patch("/api/v2/settings", {"theme": "dark", "scan_timeout_ms": 1200}) as response:
            settings = json.loads(response.read().decode("utf-8"))["settings"]
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["scan_timeout_ms"], 1200)
        with self.post("/api/v2/analysis/ping", {"address": "192.0.2.1", "count": 2}) as response:
            ping = json.loads(response.read().decode("utf-8"))["result"]
        self.assertEqual(ping["received"], 2)
        with self.get("/api/v2/export/devices.csv") as response:
            csv_body = response.read().decode("utf-8-sig")
        self.assertTrue(csv_body.startswith("设备名称"))
        with self.get(f"/api/v2/export/history/{task_id}.csv") as response:
            self.assertIn("IP 地址", response.read().decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
