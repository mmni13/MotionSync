from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog


_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_CHUNK_SIZE = 1024 * 1024


class UpdateCancelled(Exception):
    pass


def windows_update_assets(release: dict) -> tuple[dict, dict] | None:
    version = str(release.get("version") or "")
    installer_name = f"MotionSync-Setup-{version}-windows-x64.exe"
    checksum_name = f"{installer_name}.sha256"
    assets = {
        str(asset.get("name")): asset
        for asset in release.get("assets") or []
        if isinstance(asset, dict)
    }
    installer = assets.get(installer_name)
    checksum = assets.get(checksum_name)
    if installer is None or checksum is None:
        return None
    return installer, checksum


def parse_sha256_checksum(checksum_text: str, installer_name: str) -> str:
    checksum_parts = str(checksum_text or "").strip().split(maxsplit=1)
    expected_hash = checksum_parts[0].lower() if checksum_parts else ""
    if not _HEX_SHA256.fullmatch(expected_hash):
        raise ValueError("The release checksum file is invalid.")
    if len(checksum_parts) == 2:
        checksum_filename = checksum_parts[1].strip().lstrip("*")
        if Path(checksum_filename).name != installer_name:
            raise ValueError("The checksum file names a different installer.")
    return expected_hash


def _download_url(url: str, destination: Path, cancel_event, progress_callback=None) -> int:
    parsed_url = urlparse(str(url))
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("The update release contains an invalid download URL.")

    total_bytes = 0
    with requests.get(url, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()
        try:
            total_bytes = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total_bytes = 0

        bytes_written = 0
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_event.is_set():
                    raise UpdateCancelled()
                if not chunk:
                    continue
                output.write(chunk)
                bytes_written += len(chunk)
                if progress_callback is not None:
                    progress_callback(bytes_written, total_bytes)

    return bytes_written


def download_verified_installer(installer_asset: dict, checksum_asset: dict, cancel_event, progress_callback=None) -> Path:
    installer_name = str(installer_asset.get("name") or "")
    if Path(installer_name).name != installer_name or not installer_name.lower().endswith(".exe"):
        raise ValueError("The release installer has an invalid filename.")

    checksum_url = str(checksum_asset.get("browser_download_url") or "")
    if urlparse(checksum_url).scheme != "https":
        raise ValueError("The release checksum URL is invalid.")
    with requests.get(checksum_url, timeout=(10, 30)) as response:
        response.raise_for_status()
        checksum_text = response.text.strip()

    expected_hash = parse_sha256_checksum(checksum_text, installer_name)

    temp_dir = Path(tempfile.mkdtemp(prefix="motionsync-update-"))
    installer_path = temp_dir / installer_name
    try:
        bytes_written = _download_url(
            str(installer_asset.get("browser_download_url") or ""),
            installer_path,
            cancel_event,
            progress_callback,
        )
        expected_size = int(installer_asset.get("size") or 0)
        if expected_size and bytes_written != expected_size:
            raise ValueError("The downloaded installer size does not match the release metadata.")

        digest = hashlib.sha256()
        with installer_path.open("rb") as installer_file:
            for chunk in iter(lambda: installer_file.read(_CHUNK_SIZE), b""):
                if cancel_event.is_set():
                    raise UpdateCancelled()
                digest.update(chunk)
        if digest.hexdigest().lower() != expected_hash:
            raise ValueError("The downloaded installer failed its SHA-256 check.")
        return installer_path
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _helper_script_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "update_helper.ps1"
    return Path(__file__).resolve().parents[1] / "installer" / "update_helper.ps1"


def launch_update_helper(installer_path: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Automatic installation is currently supported on Windows only.")
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Automatic installation can only be started from the installed application.")

    helper_path = _helper_script_path()
    if not helper_path.is_file():
        raise FileNotFoundError(f"Update helper is missing: {helper_path}")

    app_path = Path(sys.executable).resolve()
    command = [
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(helper_path),
        "-ParentProcessId",
        str(os.getpid()),
        "-InstallerPath",
        str(installer_path.resolve()),
        "-ApplicationPath",
        str(app_path),
    ]
    if not Path(command[0]).is_file():
        command[0] = "powershell.exe"
    subprocess.Popen(
        command,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class _DownloadThread(QThread):
    progress = Signal(int, int)

    def __init__(self, installer_asset: dict, checksum_asset: dict, parent=None):
        super().__init__(parent)
        self.installer_asset = dict(installer_asset)
        self.checksum_asset = dict(checksum_asset)
        self.cancel_event = threading.Event()
        self.installer_path: Path | None = None
        self.error_message = ""
        self.was_cancelled = False

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            self.installer_path = download_verified_installer(
                self.installer_asset,
                self.checksum_asset,
                self.cancel_event,
                self.progress.emit,
            )
        except UpdateCancelled:
            self.was_cancelled = True
        except Exception as exc:
            self.error_message = str(exc)


class WindowsUpdateController(QObject):
    def __init__(self, parent, installer_asset: dict, checksum_asset: dict):
        super().__init__(parent)
        self.window = parent
        self.progress_dialog = QProgressDialog(
            "Downloading and verifying the update...",
            "Cancel",
            0,
            0,
            parent,
        )
        self.progress_dialog.setWindowTitle("MotionSync update")
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.canceled.connect(self.cancel)

        self.thread = _DownloadThread(installer_asset, checksum_asset, self)
        self.thread.progress.connect(self._on_progress)
        self.thread.finished.connect(self._on_download_finished)
        self.thread.start()
        self.progress_dialog.show()

    def cancel(self) -> None:
        self.thread.cancel()
        self.progress_dialog.setLabelText("Cancelling update download...")
        self.progress_dialog.setCancelButton(None)

    def _on_progress(self, received: int, total: int) -> None:
        if total > 0:
            if self.progress_dialog.maximum() != total:
                self.progress_dialog.setRange(0, total)
            self.progress_dialog.setValue(received)
            self.progress_dialog.setLabelText(
                f"Downloading update: {received / 1_000_000:.1f} / {total / 1_000_000:.1f} MB"
            )
        else:
            self.progress_dialog.setLabelText(
                f"Downloading update: {received / 1_000_000:.1f} MB"
            )

    def _on_download_finished(self) -> None:
        self.progress_dialog.close()
        if self.thread.was_cancelled:
            return
        if self.thread.error_message:
            QMessageBox.warning(
                self.window,
                "Update failed",
                f"MotionSync could not download or verify the update.\n\n{self.thread.error_message}",
            )
            return
        installer_path = self.thread.installer_path
        if installer_path is None:
            QMessageBox.warning(self.window, "Update failed", "The update download did not produce an installer.")
            return

        try:
            launch_update_helper(installer_path)
        except Exception as exc:
            shutil.rmtree(installer_path.parent, ignore_errors=True)
            QMessageBox.warning(
                self.window,
                "Could not start update",
                f"The update was downloaded and verified, but could not be started.\n\n{exc}",
            )
            return

        self.window.close()
        application = QApplication.instance()
        if application is not None:
            application.quit()


def begin_windows_update(parent, installer_asset: dict, checksum_asset: dict) -> WindowsUpdateController:
    controller = WindowsUpdateController(parent, installer_asset, checksum_asset)
    parent._windows_update_controller = controller
    return controller
