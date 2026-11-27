import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher


class NativeWindowOutcomeTests(unittest.TestCase):
    def test_loaded_is_success_even_after_close(self):
        self.assertEqual(
            launcher.native_window_outcome(True, True, False),
            (True, ""),
        )

    def test_closed_before_loaded_is_failure(self):
        ok, reason = launcher.native_window_outcome(False, True, False)
        self.assertFalse(ok)
        self.assertIn("加载完成前", reason)

    def test_timeout_is_failure(self):
        ok, reason = launcher.native_window_outcome(False, True, True)
        self.assertFalse(ok)
        self.assertIn("秒内未能完成加载", reason)

    def test_missing_signal_is_failure(self):
        ok, _reason = launcher.native_window_outcome(False, False, False)
        self.assertFalse(ok)


class WebviewProfileBackupTests(unittest.TestCase):
    def test_backup_moves_existing_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "webview"
            profile.mkdir()
            (profile / "marker.txt").write_text("x", encoding="utf-8")
            backup = launcher.backup_webview_profile(profile)
            self.assertIsNotNone(backup)
            self.assertFalse(profile.exists())
            self.assertTrue((backup / "marker.txt").is_file())

    def test_missing_profile_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                launcher.backup_webview_profile(Path(tmp) / "webview")
            )


class ConfigPortTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        patcher = mock.patch.object(launcher, "app_data_dir", lambda: self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, payload):
        (self.data_dir / "config.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def read_config(self):
        return json.loads(
            (self.data_dir / "config.json").read_text(encoding="utf-8")
        )

    def test_v2_port_is_separate_from_v1_port(self):
        self.write_config(
            {"v1_port": 60788, "v2_port": 50123, "browser": "edge.exe"}
        )
        self.assertEqual(launcher.load_config()["port"], 50123)
        launcher.save_config_port(51000)
        saved = self.read_config()
        self.assertEqual(saved["v2_port"], 51000)
        self.assertEqual(saved["v1_port"], 60788)
        self.assertEqual(saved["browser"], "edge.exe")

    def test_missing_v2_port_falls_back_to_legacy_port(self):
        self.write_config({"port": 18743})
        self.assertEqual(launcher.load_config()["port"], 18743)

    def test_invalid_v2_port_uses_os_assigned_port(self):
        self.write_config({"v2_port": "not-a-port"})
        self.assertEqual(launcher.load_config()["port"], launcher.DEFAULT_PORT)


class ProfileIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        patcher = mock.patch.object(launcher, "app_data_dir", lambda: self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        launcher.release_profile_locks()
        self.addCleanup(launcher.release_profile_locks)

    def test_first_instance_uses_shared_profile(self):
        path = launcher.select_profile_dir("webview", 50001)
        self.assertEqual(path.name, "webview")
        self.assertTrue(launcher.profile_in_use("webview"))

    def test_second_instance_uses_port_suffixed_profile(self):
        launcher.select_profile_dir("webview", 50001)
        second = launcher.select_profile_dir("webview", 50002)
        self.assertEqual(second.name, "webview-50002")
        self.assertTrue(second.is_dir())

    def test_state_file_is_per_instance(self):
        self.assertNotEqual(
            launcher.webview_state_path(50001, False),
            launcher.webview_state_path(50002, True),
        )


if __name__ == "__main__":
    unittest.main()
