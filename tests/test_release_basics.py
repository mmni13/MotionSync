from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from viewer import app_paths
from viewer.contemplas_catalog import default_recording_root
from viewer.motion_api import Motion1080Api
from viewer.run_library import RunLibrary
from viewer.update_checker import is_newer_version


class ReleaseBasicsTests(unittest.TestCase):
    def test_versions_use_semantic_ordering(self):
        self.assertTrue(is_newer_version("1.10.0", "1.9.0"))
        self.assertFalse(is_newer_version("1.9.0", "1.10.0"))
        self.assertFalse(is_newer_version("not-a-version", "1.0.0"))

    def test_windows_data_paths_remain_compatible(self):
        local_app_data = r"C:\Users\Test\AppData\Local"
        with patch.dict(os.environ, {"LOCALAPPDATA": local_app_data}):
            with patch.object(sys, "platform", "win32"):
                root = Path(local_app_data) / "1080SyncViewer"
                self.assertEqual(app_paths.user_data_dir(), root)
                self.assertEqual(RunLibrary.default_root_dir(), root)
                self.assertEqual(
                    Motion1080Api.default_config_path(),
                    root / "config.json",
                )
                self.assertEqual(
                    app_paths.user_cache_dir(),
                    Path(local_app_data) / "1080_sync" / "cache",
                )
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            with patch.object(sys, "platform", "win32"):
                self.assertEqual(
                    app_paths.user_data_dir(),
                    Path.home() / ".1080SyncViewer",
                )

    def test_macos_uses_application_support_and_caches(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                app_paths.user_data_dir(),
                Path.home() / "Library" / "Application Support" / "MotionSync",
            )
            self.assertEqual(
                app_paths.user_cache_dir(),
                Path.home() / "Library" / "Caches" / "MotionSync",
            )
            self.assertIsNone(default_recording_root())


if __name__ == "__main__":
    unittest.main()
