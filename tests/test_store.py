"""Persistence and identity tests for the V2 device inventory."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from network_locator.store import NetworkStore


def observation(ip: str, mac: str, *, state: str = "REACHABLE", interface: str = "Ethernet") -> dict:
    return {
        "ip": ip,
        "mac": mac,
        "state": state,
        "interface": interface,
        "interface_id": "Ethernet|1",
        "scan_range": "192.0.2.0/24",
    }


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NetworkStore(Path(self.temp.name) / "test.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def scan(self, rows: list[dict], *, status: str = "completed") -> str:
        task_id = self.store.create_scan_task()
        self.store.ingest_scan_results(task_id, rows)
        self.store.finish_scan_task(
            task_id,
            status=status,
            total_hosts=254,
            probed_hosts=254,
        )
        return task_id

    def test_mac_identity_updates_ip_instead_of_creating_a_device(self) -> None:
        self.scan([observation("192.0.2.10", "02-42-AC-11-00-10")])
        self.scan([observation("192.0.2.11", "02-42-AC-11-00-10")])

        devices = self.store.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["current_ip"], "192.0.2.11")
        self.assertEqual(devices[0]["address_count"], 2)
        detail = self.store.get_device(devices[0]["device_id"])
        self.assertIsNotNone(detail)
        self.assertTrue(any(event["event_type"] == "ip_changed" for event in detail["events"]))

    def test_repeated_observation_is_deduplicated(self) -> None:
        self.scan(
            [
                observation("192.0.2.10", "02-42-AC-11-00-10", state="STALE"),
                observation("192.0.2.10", "02-42-AC-11-00-10", state="REACHABLE"),
            ]
        )
        devices = self.store.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["last_state"], "REACHABLE")

    def test_invalid_arp_macs_never_create_or_update_devices(self) -> None:
        task_id = self.scan(
            [
                observation("192.0.2.10", "02-42-AC-11-00-10"),
                observation("192.0.2.255", "FF-FF-FF-FF-FF-FF"),
                observation("192.0.2.20", "01-00-5E-00-00-01"),
                observation("192.0.2.21", "00-00-00-00-00-00"),
            ]
        )
        devices = self.store.list_devices()
        self.assertEqual([item["current_ip"] for item in devices], ["192.0.2.10"])
        history = self.store.get_scan_task(task_id)
        self.assertIsNotNone(history)
        result_ips = {item["ip"] for item in history["results"]}
        self.assertEqual(result_ips, {"192.0.2.10"})

    def test_schema_migration_purges_legacy_broadcast_device(self) -> None:
        path = Path(self.temp.name) / "legacy.db"
        legacy = NetworkStore(path)
        now = "2026-01-01T00:00:00+00:00"
        with legacy._lock, legacy._conn:
            legacy._conn.execute(
                """
                INSERT INTO devices(
                    device_id, identity_key, primary_mac, current_ip,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-broadcast",
                    "mac:FF-FF-FF-FF-FF-FF",
                    "FF-FF-FF-FF-FF-FF",
                    "192.0.2.255",
                    now,
                    now,
                    now,
                    now,
                ),
            )
            legacy._conn.execute(
                "UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'"
            )
        legacy.close()

        migrated = NetworkStore(path)
        try:
            self.assertEqual(migrated.list_devices(), [])
            self.assertEqual(migrated.get_settings()["schema_version"], 2)
        finally:
            migrated.close()

    def test_manual_fields_survive_rescans(self) -> None:
        self.scan([observation("192.0.2.10", "02-42-AC-11-00-10")])
        device = self.store.list_devices()[0]
        self.store.update_device(
            device["device_id"],
            {"display_name": "财务打印机", "note": "二楼", "favorite": True},
        )
        self.scan([observation("192.0.2.12", "02-42-AC-11-00-10")])
        refreshed = self.store.get_device(device["device_id"])
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed["display_name"], "财务打印机")
        self.assertEqual(refreshed["note"], "二楼")
        self.assertTrue(refreshed["favorite"])
        self.assertEqual(refreshed["current_ip"], "192.0.2.12")

    def test_same_mac_on_two_ips_in_one_scan_keeps_both_conflict_records(self) -> None:
        self.scan(
            [
                observation("192.0.2.10", "02-42-AC-11-00-10"),
                observation("192.0.2.20", "02-42-AC-11-00-10"),
            ]
        )
        self.assertEqual(len(self.store.list_devices()), 2)

    def test_absent_device_transitions_to_offline(self) -> None:
        self.scan([observation("192.0.2.10", "02-42-AC-11-00-10")])
        self.scan([observation("192.0.2.12", "02-42-AC-11-00-12")])
        devices = {item["current_ip"]: item for item in self.store.list_devices()}
        self.assertEqual(devices["192.0.2.10"]["status"], "offline")
        self.assertEqual(devices["192.0.2.12"]["status"], "online")

    def test_lookup_supports_full_prefix_suffix_and_batch(self) -> None:
        self.scan([observation("192.0.2.10", "02-42-AC-11-00-10")])
        result = self.store.lookup_devices("02-42-AC-11-00-10\n02-42-AC\n0010\nZZZZ")
        self.assertEqual(result["query_count"], 4)
        self.assertEqual(result["queries"][0]["kind"], "full")
        self.assertEqual(result["queries"][1]["kind"], "prefix")
        self.assertEqual(result["queries"][2]["kind"], "suffix")
        self.assertEqual(result["queries"][3]["kind"], "invalid")
        self.assertEqual(len(result["queries"][0]["matches"]), 1)
        self.assertEqual(len(result["queries"][1]["matches"]), 1)
        self.assertEqual(len(result["queries"][2]["matches"]), 1)

    def test_settings_are_clamped_and_persisted(self) -> None:
        settings = self.store.update_settings(
            {
                "scan_timeout_ms": 99999,
                "scan_concurrency": 0,
                "offline_after_minutes": 45,
                "theme": "dark",
            }
        )
        self.assertEqual(settings["scan_timeout_ms"], 5000)
        self.assertEqual(settings["scan_concurrency"], 1)
        self.assertEqual(settings["offline_after_minutes"], 45)
        self.assertEqual(settings["theme"], "dark")

    def test_csv_export_has_bom_and_protects_formula_cells(self) -> None:
        self.scan([observation("192.0.2.10", "02-42-AC-11-00-10")])
        device = self.store.list_devices()[0]
        self.store.update_device(device["device_id"], {"note": "=cmd()"})
        body, filename = self.store.export_devices_csv(None)
        self.assertTrue(body.startswith("\ufeff"))
        self.assertIn("'=cmd()", body)
        self.assertTrue(filename.endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
