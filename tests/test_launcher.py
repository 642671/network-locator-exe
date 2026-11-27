"""Launcher configuration tests for the Windows build."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import launcher


class LauncherConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_localappdata = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.tempdir.name
        self.config_path = Path(self.tempdir.name) / "NetworkLocator" / "config.json"

    def tearDown(self) -> None:
        if self.previous_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self.previous_localappdata
        self.tempdir.cleanup()

    def write_config(self, payload: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_default_port_is_os_assigned(self) -> None:
        self.assertEqual(launcher.load_config()["port"], 0)
        self.assertFalse(self.config_path.exists())

    def test_legacy_port_is_honoured_for_upgrades(self) -> None:
        self.write_config({"port": 18743, "browser": "edge.exe"})
        config = launcher.load_config()
        self.assertEqual(config["port"], 18743)
        self.assertEqual(config["browser"], "edge.exe")

    def test_v1_port_wins_and_does_not_clobber_v2_port(self) -> None:
        self.write_config({"port": 18743, "v1_port": 50505, "browser": ""})
        self.assertEqual(launcher.load_config()["port"], 50505)

        launcher.save_config_port(60606)
        stored = self.read_config()
        self.assertEqual(stored["v1_port"], 60606)
        self.assertEqual(stored["port"], 18743)
        self.assertEqual(launcher.load_config()["port"], 60606)

    def test_save_port_creates_config_without_a_v2_port(self) -> None:
        launcher.save_config_port(51234)
        stored = self.read_config()
        self.assertEqual(stored["v1_port"], 51234)
        self.assertNotIn("port", stored)


if __name__ == "__main__":
    unittest.main()
