import sys

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from viewer.main_window import VideoDataViewer
from viewer.update_checker import get_latest_version
from viewer.version import VERSION

def check_for_updates(parent=None):
    latest = get_latest_version()

    if not latest:
        return

    if latest["version"] > VERSION:

        reply = QMessageBox.question(
            parent,
            "Update Available",
            (
                f"A new version of MotionSync is available.\n\n"
                f"Current version: {VERSION}\n"
                f"Latest version: {latest['version']}\n\n"
                f"Open the download page?"
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