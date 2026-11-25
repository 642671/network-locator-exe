import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
