from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_path_part(value: str | int | None, fallback: str = "unknown") -> str:
    """
    Convert ids/names into safe folder names.

    API ids are usually UUIDs and already safe, but this function also protects
    against spaces, slashes, Windows-reserved characters, and empty values.
    """
    if value is None:
        value = fallback

    text = str(value).strip()
    if not text:
        text = fallback

    text = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")

    return text or fallback


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """
    Write JSON atomically to reduce the risk of corrupted metadata if the app
    exits during write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name,
        suffix=".tmp",
        dir=str(path.parent),
    )

    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

        tmp_path.replace(path)

    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class RunIdentity:
    """
    Unique identity for one 1080 run.

    The hierarchy is:

        Client -> Session -> Set -> Run

    All ids should come from the 1080 API where possible.
    """

    client_id: str
    session_id: str
    set_id: str
    run_id: str

    client_name: str = ""
    session_name: str = ""
    set_name: str = ""
    run_name: str = ""

    set_index: int | None = None
    run_index: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunIdentity":
        return cls(
            client_id=str(data.get("client_id", "")),
            session_id=str(data.get("session_id", "")),
            set_id=str(data.get("set_id", "")),
            run_id=str(data.get("run_id", "")),
            client_name=str(data.get("client_name", "")),
            session_name=str(data.get("session_name", "")),
            set_name=str(data.get("set_name", "")),
            run_name=str(data.get("run_name", "")),
            set_index=data.get("set_index"),
            run_index=data.get("run_index"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "session_id": self.session_id,
            "session_name": self.session_name,
            "set_id": self.set_id,
            "set_name": self.set_name,
            "set_index": self.set_index,
            "run_id": self.run_id,
            "run_name": self.run_name,
            "run_index": self.run_index,
        }


class RunLibrary:
    """
    Local persistent storage for 1080 API run links and cached data.

    This class intentionally does NOT talk to the 1080 API and does NOT know
    anything about the video backend. It only manages local files/metadata.

    Local structure:

        %LOCALAPPDATA%/
          1080SyncViewer/
            runs/
              <client_id>/
                <session_id>/
                  <set_id>/
                    <run_id>/
                      run_meta.json
                      data.xlsx
    """

    APP_FOLDER_NAME = "1080SyncViewer"

    def __init__(self, root_dir: Path | None = None):
        if root_dir is None:
            root_dir = self.default_root_dir()

        self.root_dir = Path(root_dir)
        self.runs_dir = self.root_dir / "runs"
        self.api_cache_dir = self.root_dir / "api_cache"

        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.api_cache_dir.mkdir(parents=True, exist_ok=True)

    # ==================================================
    # ROOT PATHS
    # ==================================================
    @classmethod
    def default_root_dir(cls) -> Path:
        """
        Return the app's persistent local data directory.

        On Windows this becomes:

            %LOCALAPPDATA%/1080SyncViewer

        On other platforms it falls back to:

            ~/.1080SyncViewer
        """
        local_app_data = os.environ.get("LOCALAPPDATA")

        if local_app_data:
            return Path(local_app_data) / cls.APP_FOLDER_NAME

        return Path.home() / f".{cls.APP_FOLDER_NAME}"

    def get_run_dir(self, identity: RunIdentity) -> Path:
        return (
            self.runs_dir
            / _safe_path_part(identity.client_id, "client")
            / _safe_path_part(identity.session_id, "session")
            / _safe_path_part(identity.set_id, "set")
            / _safe_path_part(identity.run_id, "run")
        )

    def get_run_meta_path(self, identity: RunIdentity) -> Path:
        return self.get_run_dir(identity) / "run_meta.json"

    def get_data_excel_path(self, identity: RunIdentity) -> Path:
        return self.get_run_dir(identity) / "data.xlsx"

    # ==================================================
    # METADATA
    # ==================================================
    def create_base_meta(self, identity: RunIdentity) -> dict[str, Any]:
        now = _utc_now_iso()

        return {
            **identity.to_dict(),
            "video_path": None,
            "videos": [],
            "active_video_id": None,
            "shared_sync_offset_seconds": None,
            "data_excel_path": str(self.get_data_excel_path(identity)),
            "complete": False,
            "created_at": now,
            "updated_at": now,
            "notes": "",
        }

    def load_run_meta(self, identity: RunIdentity) -> dict[str, Any] | None:
        path = self.get_run_meta_path(identity)
        data = _read_json(path, default=None)

        if not isinstance(data, dict):
            return None

        return data

    def ensure_run_meta(self, identity: RunIdentity) -> dict[str, Any]:
        """
        Return existing metadata, or create a new metadata file if missing.
        """
        existing = self.load_run_meta(identity)

        if existing is not None:
            return existing

        meta = self.create_base_meta(identity)
        self.save_run_meta(identity, meta)
        return meta

    def save_run_meta(self, identity: RunIdentity, meta: dict[str, Any]) -> None:
        """
        Save metadata for a run.

        Identity fields are refreshed from the current identity, so display names
        can update if the API returns newer names later.
        """
        existing = self.load_run_meta(identity) or {}

        merged = {
            **existing,
            **meta,
            **identity.to_dict(),
            "updated_at": _utc_now_iso(),
        }

        if not merged.get("created_at"):
            merged["created_at"] = _utc_now_iso()

        # Keep the canonical Excel path stable unless explicitly overridden.
        if not merged.get("data_excel_path"):
            merged["data_excel_path"] = str(self.get_data_excel_path(identity))

        _atomic_write_json(self.get_run_meta_path(identity), merged)

    # ==================================================
    # VIDEO LINKING
    # ==================================================
    def _normalize_video_meta(self, meta: dict[str, Any]) -> dict[str, Any]:
        """Return metadata with a backward-compatible multi-video collection."""
        normalized = dict(meta or {})
        videos = normalized.get("videos")
        if not isinstance(videos, list):
            videos = []

        clean: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for index, item in enumerate(videos):
            if not isinstance(item, dict):
                continue
            value = item.get("path") or item.get("video_path")
            if not value:
                continue
            resolved = str(Path(value).expanduser().resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            entry = dict(item)
            entry["id"] = str(entry.get("id") or f"camera_{index + 1}")
            entry["path"] = resolved
            entry["label"] = str(entry.get("label") or f"Camera {index + 1}")
            entry["is_primary"] = bool(entry.get("is_primary", index == 0))
            entry["attachment_mode"] = str(entry.get("attachment_mode") or "manual")
            entry["match_details"] = dict(entry.get("match_details") or {})
            clean.append(entry)

        legacy = normalized.get("video_path")
        if legacy:
            legacy_path = str(Path(legacy).expanduser().resolve())
            if legacy_path not in seen_paths:
                clean.insert(0, {
                    "id": "camera_1",
                    "path": legacy_path,
                    "label": "Camera 1",
                    "is_primary": True,
                    "attachment_mode": str(normalized.get("video_attachment_mode") or "manual"),
                    "match_details": dict(normalized.get("video_match_details") or {}),
                })

        if clean and not any(item.get("is_primary") for item in clean):
            clean[0]["is_primary"] = True
        if clean:
            primary = next((item for item in clean if item.get("is_primary")), clean[0])
            normalized["video_path"] = primary["path"]
            normalized["active_video_id"] = str(
                normalized.get("active_video_id") or primary["id"]
            )
        else:
            normalized["video_path"] = None
            normalized["active_video_id"] = None
        normalized["videos"] = clean
        return normalized

    def get_videos(self, identity: RunIdentity) -> list[dict[str, Any]]:
        meta = self.load_run_meta(identity) or {}
        return [dict(item) for item in meta.get("videos", []) if isinstance(item, dict)]

    def get_active_video_id(self, identity: RunIdentity) -> str | None:
        meta = self.load_run_meta(identity) or {}
        value = meta.get("active_video_id")
        return str(value) if value else None

    def set_active_video(self, identity: RunIdentity, camera_id: str) -> None:
        meta = self.ensure_run_meta(identity)
        valid_ids = {str(item.get("id")) for item in meta.get("videos", [])}
        if str(camera_id) not in valid_ids:
            raise KeyError(f"Unknown camera id: {camera_id}")
        meta["active_video_id"] = str(camera_id)
        self.save_run_meta(identity, meta)

    def get_shared_sync_offset_seconds(
        self,
        identity: RunIdentity,
    ) -> float | None:
        """Return the run-wide LED-to-data aftersync correction in seconds."""
        meta = self.load_run_meta(identity) or {}
        value = meta.get("shared_sync_offset_seconds")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def set_shared_sync_offset_seconds(
        self,
        identity: RunIdentity,
        seconds: float,
    ) -> None:
        """Persist one time correction shared by every camera in the run."""
        meta = self.ensure_run_meta(identity)
        meta["shared_sync_offset_seconds"] = float(seconds)
        self.save_run_meta(identity, meta)

    def attach_video_angle(
        self,
        identity: RunIdentity,
        video_path: Path,
        *,
        label: str | None = None,
        attachment_mode: str = "manual",
        match_details: dict[str, Any] | None = None,
        make_primary: bool = False,
    ) -> dict[str, Any]:
        """Add one camera angle without replacing existing camera links."""
        video_path = Path(video_path)
        if not video_path.exists() or not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")
        resolved = str(video_path.resolve())
        meta = self.ensure_run_meta(identity)
        videos = [dict(item) for item in meta.get("videos", [])]

        for item in videos:
            if str(Path(item.get("path", "")).resolve()) == resolved:
                item["label"] = str(label or item.get("label") or video_path.stem)
                item["attachment_mode"] = str(attachment_mode or "manual")
                item["match_details"] = dict(match_details or item.get("match_details") or {})
                if make_primary:
                    for other in videos:
                        other["is_primary"] = other is item
                meta["videos"] = videos
                if make_primary:
                    meta["active_video_id"] = str(item.get("id"))
                    meta["video_path"] = resolved
                meta["complete"] = self._compute_complete_from_meta(meta)
                self.save_run_meta(identity, meta)
                return self.load_run_meta(identity) or meta

        existing_ids = {str(item.get("id")) for item in videos}
        number = 1
        camera_id = f"camera_{number}"
        while camera_id in existing_ids:
            number += 1
            camera_id = f"camera_{number}"
        if not videos:
            make_primary = True
        if make_primary:
            for item in videos:
                item["is_primary"] = False
        entry = {
            "id": camera_id,
            "path": resolved,
            "label": str(label or f"Camera {len(videos) + 1}"),
            "is_primary": bool(make_primary),
            "attachment_mode": str(attachment_mode or "manual"),
            "match_details": dict(match_details or {}),
        }
        videos.append(entry)
        meta["videos"] = videos
        if make_primary or not meta.get("video_path"):
            meta["video_path"] = resolved
            meta["active_video_id"] = camera_id
        meta["complete"] = self._compute_complete_from_meta(meta)
        self.save_run_meta(identity, meta)
        return self.load_run_meta(identity) or meta

    def remove_video_angle(self, identity: RunIdentity, camera_id: str) -> dict[str, Any]:
        meta = self.ensure_run_meta(identity)
        videos = [
            dict(item) for item in meta.get("videos", [])
            if str(item.get("id")) != str(camera_id)
        ]
        if videos and not any(item.get("is_primary") for item in videos):
            videos[0]["is_primary"] = True
        meta["videos"] = videos
        if videos:
            primary = next((item for item in videos if item.get("is_primary")), videos[0])
            meta["video_path"] = primary["path"]
            if meta.get("active_video_id") == str(camera_id):
                meta["active_video_id"] = primary["id"]
        else:
            meta["video_path"] = None
            meta["active_video_id"] = None
        meta["complete"] = self._compute_complete_from_meta(meta)
        self.save_run_meta(identity, meta)
        return self.load_run_meta(identity) or meta

    def attach_video(
        self,
        identity: RunIdentity,
        video_path: Path,
        *,
        attachment_mode: str = "manual",
        match_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a manual or automatic video link without copying the video."""
        video_path = Path(video_path)
        if not video_path.exists() or not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        meta = self.ensure_run_meta(identity)
        resolved = str(video_path.resolve())
        videos = [dict(item) for item in meta.get("videos", [])]
        primary = next((item for item in videos if item.get("is_primary")), None)
        if primary is None:
            result = self.attach_video_angle(
                identity,
                video_path,
                label="Camera 1",
                attachment_mode=attachment_mode,
                match_details=match_details,
                make_primary=True,
            )
            return result
        primary["path"] = resolved
        primary["attachment_mode"] = str(attachment_mode or "manual")
        primary["match_details"] = dict(match_details or {})
        meta["videos"] = videos
        meta["video_path"] = resolved
        meta["active_video_id"] = str(primary.get("id"))
        meta["video_attachment_mode"] = str(attachment_mode or "manual")
        meta["video_match_details"] = dict(match_details or {})
        meta["complete"] = self._compute_complete_from_meta(meta)
        self.save_run_meta(identity, meta)
        return self.load_run_meta(identity) or meta

    def get_video_path(self, identity: RunIdentity) -> Path | None:
        meta = self.load_run_meta(identity)

        if not meta:
            return None

        value = meta.get("video_path")

        if not value:
            return None

        return Path(value)

    # ==================================================
    # DATA / EXCEL CACHE
    # ==================================================
    def save_excel_bytes(self, identity: RunIdentity, content: bytes) -> Path:
        """
        Save generated/downloaded Excel bytes as the cached data.xlsx for a run.
        """
        if not isinstance(content, bytes):
            raise TypeError("Excel content must be bytes.")

        path = self.get_data_excel_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

        meta = self.ensure_run_meta(identity)
        meta["data_excel_path"] = str(path)
        meta["complete"] = self._compute_complete_from_meta(meta)

        self.save_run_meta(identity, meta)

        return path

    def copy_excel_file(self, identity: RunIdentity, source_excel_path: Path) -> Path:
        """
        Copy an existing Excel file into the run cache.

        This is useful if we first generate/export the API data somewhere else.
        """
        source_excel_path = Path(source_excel_path)

        if not source_excel_path.exists() or not source_excel_path.is_file():
            raise FileNotFoundError(f"Excel file not found: {source_excel_path}")

        destination = self.get_data_excel_path(identity)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if source_excel_path.resolve() != destination.resolve():
            shutil.copy2(source_excel_path, destination)

        meta = self.ensure_run_meta(identity)
        meta["data_excel_path"] = str(destination)
        meta["complete"] = self._compute_complete_from_meta(meta)

        self.save_run_meta(identity, meta)

        return destination

    def get_data_path(self, identity: RunIdentity) -> Path | None:
        meta = self.load_run_meta(identity)

        if not meta:
            return None

        value = meta.get("data_excel_path")

        if not value:
            return None

        return Path(value)

    # ==================================================
    # COMPLETION / STATUS
    # ==================================================
    def set_complete(self, identity: RunIdentity, complete: bool = True) -> dict[str, Any]:
        """
        Manually set completion state.

        If complete=True is requested, this method validates that both video and
        Excel data exist. This prevents false green checkmarks.
        """
        meta = self.ensure_run_meta(identity)

        if complete:
            missing = self.get_missing_requirements(identity)

            if missing:
                missing_text = ", ".join(missing)
                raise RuntimeError(
                    f"Cannot mark run complete. Missing: {missing_text}"
                )

        meta["complete"] = bool(complete)
        self.save_run_meta(identity, meta)

        return self.load_run_meta(identity) or meta

    def is_complete(self, identity: RunIdentity) -> bool:
        meta = self.load_run_meta(identity)

        if not meta:
            return False

        return bool(meta.get("complete")) and not self.get_missing_requirements(identity)

    def get_missing_requirements(self, identity: RunIdentity) -> list[str]:
        """
        Return a list of missing things required to load a run.
        """
        missing: list[str] = []
        meta = self.load_run_meta(identity)

        if not meta:
            return ["video", "data"]

        video_value = meta.get("video_path")
        data_value = meta.get("data_excel_path")

        if not video_value or not Path(video_value).exists():
            missing.append("video")

        if not data_value or not Path(data_value).exists():
            missing.append("data")

        return missing

    def get_status_icon(self, identity: RunIdentity) -> str:
        """
        Small UI helper for sidebar lists.

        ✅ = complete and loadable
        ⚠ = metadata exists but video/data missing
        ◯ = no local metadata yet
        """
        meta = self.load_run_meta(identity)

        if not meta:
            return "◯"

        missing = self.get_missing_requirements(identity)

        if not missing and bool(meta.get("complete")):
            return "✅"

        if missing:
            return "⚠"

        return "◯"

    def get_load_paths(self, identity: RunIdentity) -> tuple[Path, Path]:
        """
        Return video path and Excel path for loading into VideoDataViewer.

        Raises a clear error if the run is not ready.
        """
        meta = self.load_run_meta(identity)

        if not meta:
            raise RuntimeError("Run has no local metadata yet.")

        missing = self.get_missing_requirements(identity)

        if missing:
            raise RuntimeError(
                "Run is not ready to load. Missing: " + ", ".join(missing)
            )

        videos = meta.get("videos") or []
        active_id = str(meta.get("active_video_id") or "")
        active = next(
            (item for item in videos if str(item.get("id")) == active_id),
            None,
        )
        if active is None:
            active = next((item for item in videos if item.get("is_primary")), videos[0])
        video_path = Path(str(active["path"]))
        data_path = Path(str(meta["data_excel_path"]))

        return video_path, data_path

    def _compute_complete_from_meta(self, meta: dict[str, Any]) -> bool:
        video_path = meta.get("video_path")
        data_excel_path = meta.get("data_excel_path")

        if not video_path or not data_excel_path:
            return False

        return Path(video_path).exists() and Path(data_excel_path).exists()

    # ==================================================
    # HOUSEKEEPING
    # ==================================================
    def delete_run_local_data(self, identity: RunIdentity) -> None:
        """
        Delete only the local run metadata/data.xlsx folder.

        This does NOT delete the original video file and does NOT delete the
        VideoBackend frame cache.
        """
        run_dir = self.get_run_dir(identity)

        if run_dir.exists():
            shutil.rmtree(run_dir)

    def clear_api_cache(self) -> None:
        """
        Clear cached API list responses if later scripts choose to use them.
        """
        if self.api_cache_dir.exists():
            shutil.rmtree(self.api_cache_dir)

        self.api_cache_dir.mkdir(parents=True, exist_ok=True)