from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from viewer.app_paths import user_data_dir


def default_recording_root() -> Path | None:
    if sys.platform != "win32":
        return None
    roaming = os.environ.get("APPDATA")
    if roaming:
        return Path(roaming) / "NTC SwimLab Analyse" / "accounts"
    return Path.home() / "AppData" / "Roaming" / "NTC SwimLab Analyse" / "accounts"


def catalog_path() -> Path:
    target = user_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target / "contemplas_recordings.json"


def _load() -> dict:
    try:
        value = json.loads(catalog_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {"version": 1, "root": "", "root_mtime_ns": 0, "manifests": [], "recordings": {}}


def _save(value: dict) -> None:
    path = catalog_path()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _snapshot(path: Path) -> dict:
    from viewer.contemplas_manifest import (
        load_contemplas_manifest,
    )

    path = Path(path).resolve()

    package = load_contemplas_manifest(
        path
    )

    raw_manifest = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    manifest = raw_manifest.get(
        "manifest",
        raw_manifest,
    )

    source = (
        manifest.get("source")
        or {}
    )

    clips = (
        manifest.get("clips")
        or []
    )

    clip = (
        clips[0]
        if clips
        and isinstance(clips[0], dict)
        else {}
    )

    capture_timing = (
        clip.get("captureTiming")
        or {}
    )

    recording_started_at = (
        clip.get("startedAt")
        or source.get("startedAt")
        or package.get("started_at")
    )

    recording_stopped_at = (
        clip.get("stoppedAt")
        or clip.get("stopRequestedAt")
    )

    try:
        duration_seconds = float(
            capture_timing.get(
                "buttonDurationSeconds"
            )
            or capture_timing.get(
                "durationSeconds"
            )
            or 0.0
        )
    except (TypeError, ValueError):
        duration_seconds = 0.0

    cameras = []

    for camera in (
        package.get("cameras")
        or []
    ):
        cameras.append(
            {
                "camera_id": str(
                    camera["camera_id"]
                ),
                "label": str(
                    camera.get("label")
                    or camera["camera_id"]
                ),
                "path": str(
                    Path(
                        camera["path"]
                    ).resolve()
                ),
                "fps": float(
                    camera.get("fps")
                    or 100.0
                ),
                "frame_count": int(
                    camera.get("frame_count")
                    or 0
                ),
                "rotation": int(
                    camera.get("rotation")
                    or 0
                ),
                "sync_offset_frames": int(
                    camera.get(
                        "sync_offset_frames"
                    )
                    or 0
                ),
                "sync_group_id": str(
                    camera["sync_group_id"]
                ),
            }
        )

    return {
        "snapshot_version": 2,
        "session_id": str(
            package["session_id"]
        ),
        "started_at": (
            recording_started_at
        ),
        "stopped_at": (
            recording_stopped_at
        ),
        "duration_seconds": (
            duration_seconds
        ),
        "manifest_path": str(path),
        "manifest_mtime_ns": int(
            path.stat().st_mtime_ns
        ),
        "cameras": cameras,
    }

def cached_snapshot(
    path: Path,
) -> dict:
    path = Path(path).resolve()

    data = _load()

    recordings = data.get(
        "recordings",
        {},
    )

    for item in recordings.values():
        stored_path = Path(
            item.get(
                "manifest_path",
                "",
            )
        )

        if (
            stored_path.resolve()
            != path
        ):
            continue

        same_file = (
            int(
                item.get(
                    "manifest_mtime_ns",
                    0,
                )
                or 0
            )
            == int(
                path.stat().st_mtime_ns
            )
        )

        current_schema = (
            int(
                item.get(
                    "snapshot_version",
                    0,
                )
                or 0
            )
            >= 2
        )

        if (
            same_file
            and current_schema
        ):
            return item

    item = _snapshot(path)

    data.setdefault(
        "recordings",
        {},
    )[
        item["session_id"]
    ] = item

    _save(data)

    return item


def discover_default_manifests(*, force_rescan: bool = False) -> list[Path]:
    recording_root = default_recording_root()
    if recording_root is None:
        return []
    root = recording_root.resolve()
    if not root.exists():
        return []
    data = _load()
    cached = [Path(value) for value in data.get("manifests") or []]
    current_stamp = int(root.stat().st_mtime_ns)
    if (
        not force_rescan
        and str(root) == str(data.get("root") or "")
        and current_stamp == int(data.get("root_mtime_ns") or 0)
        and cached
        and all(path.exists() for path in cached)
    ):
        return cached

    found: list[Path] = []
    for current, _directories, files in os.walk(root):
        names = {name.lower(): name for name in files}
        chosen = names.get("swimlab-recording.json") or names.get("session.manifest.json")
        if chosen:
            found.append((Path(current) / chosen).resolve())

    recordings = data.setdefault("recordings", {})
    valid: list[Path] = []
    for path in sorted(set(found)):
        try:
            item = _snapshot(path)
        except Exception:
            continue
        recordings[item["session_id"]] = item
        valid.append(path)

    data.update({
        "version": 1,
        "root": str(root),
        "root_mtime_ns": current_stamp,
        "manifests": [str(path) for path in valid],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _save(data)
    return valid


def attach_manifest_package(run_library, identity, manifest_path: Path) -> int:
    snapshot = cached_snapshot(manifest_path)
    existing = run_library.get_videos(identity)
    existing_paths = {
        str(Path(item.get("path", "")).resolve())
        for item in existing
        if item.get("path")
    }
    added = 0
    for camera in snapshot["cameras"]:
        camera_path = Path(camera["path"]).resolve()
        if not camera_path.exists() or str(camera_path) in existing_paths:
            continue
        details = {
            "source_type": "bcap",
            "manifest_path": str(Path(manifest_path).resolve()),
            "manifest_camera_id": camera["camera_id"],
            "sync_group_id": camera["sync_group_id"],
            "synchronized": True,
            "sync_offset_frames": camera["sync_offset_frames"],
            "fps": camera["fps"],
            "frame_count": camera["frame_count"],
            "rotation": camera["rotation"],
        }
        run_library.attach_video_angle(
            identity,
            camera_path,
            label=camera["label"],
            attachment_mode="contemplas_automatic",
            match_details=details,
            make_primary=(not existing and added == 0),
        )
        existing_paths.add(str(camera_path))
        added += 1
    return added
