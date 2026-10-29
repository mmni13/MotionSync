from __future__ import annotations

import os
import sys
from pathlib import Path


APP_FOLDER_NAME = "1080SyncViewer"


def user_data_dir() -> Path:
    """Return the persistent per-user application data directory."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        return Path(root) / APP_FOLDER_NAME if root else Path.home() / f".{APP_FOLDER_NAME}"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MotionSync"

    root = os.environ.get("XDG_DATA_HOME")
    return Path(root) / "motionsync" if root else Path.home() / ".local" / "share" / "motionsync"


def user_cache_dir() -> Path:
    """Return the per-user cache directory, preserving existing Windows paths."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        return Path(root) / "1080_sync" / "cache" if root else Path.home() / ".1080_sync" / "cache"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "MotionSync"

    root = os.environ.get("XDG_CACHE_HOME")
    return Path(root) / "motionsync" if root else Path.home() / ".cache" / "motionsync"
