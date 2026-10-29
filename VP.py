import sys

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from viewer.main_window import VideoDataViewer
from viewer.update_checker import get_latest_release, is_newer_version
from viewer.updater import begin_windows_update, windows_update_assets
from viewer.version import VERSION

def check_for_updates(parent=None):
    latest = get_latest_release()

    if not latest:
        return

    if is_newer_version(latest["version"], VERSION):
        windows_assets = (
            windows_update_assets(latest)
            if sys.platform == "win32" and getattr(sys, "frozen", False)
            else None
        )
        if windows_assets is not None:
            box = QMessageBox(parent)
            box.setWindowTitle("Update Available")
            box.setText(
                f"MotionSync {latest['version']} is available.\n\n"
                "Download, verify, and install it now? MotionSync will close "
                "and reopen when the update finishes."
            )
            update_button = box.addButton("Update", QMessageBox.AcceptRole)
            box.addButton("Later", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == update_button:
                begin_windows_update(parent, *windows_assets)
            return

        reply = QMessageBox.question(
            parent,
            "Update Available",
            (
                f"A new version of MotionSync is available.\n\n"
                f"Current version: {VERSION}\n"
                f"Latest version: {latest['version']}\n\n"
                "The automatic installer is not available for this platform yet.\n\n"
                "Open the release page to download the update?"
            ),
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl(latest["url"]))


def main() -> None:
    app = QApplication(sys.argv)

    viewer = VideoDataViewer()
    viewer.resize(1200, 900)
    viewer.show()

    check_for_updates(viewer)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()