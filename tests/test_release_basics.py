from __future__ import annotations

import os
import hashlib
import sys
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from viewer import app_paths
from viewer.contemplas_catalog import default_recording_root
from viewer.motion_api import Motion1080Api
from viewer.run_library import RunLibrary
from viewer.updater import (
    download_verified_installer,
    parse_sha256_checksum,
    windows_update_assets,
)
from viewer.update_checker import is_newer_version


class ReleaseBasicsTests(unittest.TestCase):
    def test_versions_use_semantic_ordering(self):
        self.assertTrue(is_newer_version("1.10.0", "1.9.0"))
        self.assertFalse(is_newer_version("1.9.0", "1.10.0"))
        self.assertFalse(is_newer_version("not-a-version", "1.0.0"))

    def test_selects_windows_installer_and_checksum_assets(self):
        version = "1.0.2"
        installer_name = f"MotionSync-Setup-{version}-windows-x64.exe"
        assets = [
            {"name": "MotionSync-1.0.2-macos-arm64.dmg"},
            {"name": installer_name},
            {"name": f"{installer_name}.sha256"},
        ]
        selected = windows_update_assets({"version": version, "assets": assets})
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0]["name"], installer_name)
        self.assertEqual(selected[1]["name"], f"{installer_name}.sha256")
        self.assertIsNone(windows_update_assets({"version": version, "assets": assets[:1]}))

    def test_checksum_must_be_valid_and_name_this_installer(self):
        installer_name = "MotionSync-Setup-1.0.2-windows-x64.exe"
        digest = "a" * 64
        self.assertEqual(
            parse_sha256_checksum(f"{digest}  {installer_name}", installer_name),
            digest,
        )
        with self.assertRaises(ValueError):
            parse_sha256_checksum(f"{digest}  different.exe", installer_name)
        with self.assertRaises(ValueError):
            parse_sha256_checksum("not-a-checksum", installer_name)

    def test_installer_download_checks_size_and_sha256(self):
        class FakeResponse:
            def __init__(self, *, content=b"", text=""):
                self.content = content
                self.text = text
                self.headers = {"Content-Length": str(len(content))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield self.content

        installer_name = "MotionSync-Setup-1.0.2-windows-x64.exe"
        installer_bytes = b"test-installer-payload"
        digest = hashlib.sha256(installer_bytes).hexdigest()
        responses = [
            FakeResponse(text=f"{digest}  {installer_name}"),
            FakeResponse(content=installer_bytes),
        ]
        with patch("viewer.updater.requests.get", side_effect=responses):
            installer_path = download_verified_installer(
                {
                    "name": installer_name,
                    "browser_download_url": "https://release.example/installer.exe",
                    "size": len(installer_bytes),
                },
                {
                    "browser_download_url": "https://release.example/installer.exe.sha256",
                },
                threading.Event(),
            )
        try:
            self.assertEqual(installer_path.read_bytes(), installer_bytes)
        finally:
            shutil.rmtree(installer_path.parent, ignore_errors=True)

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
