import sys

from PySide6.QtWidgets import QApplication

from viewer.main_window import VideoDataViewer
def main() -> None:
    app = QApplication(sys.argv)

    viewer = VideoDataViewer()
    viewer.resize(1200, 900)
    viewer.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()