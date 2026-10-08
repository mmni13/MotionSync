from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def default_recording_root() -> Path:
    roaming = os.environ.get("APPDATA")
    if roaming:
        return Path(roaming) / "NTC SwimLab Analyse" / "accounts"
    return Path.home() / "AppData" / "Roaming" / "NTC SwimLab Analyse" / "accounts"


def catalog_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home()
    target = root / "1080SyncViewer"
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
    from viewer.contemplas_manifest import load_contemplas_manifest
    package = load_contemplas_manifest(path)
    cameras = []
    for camera in package.get("cameras") or []:
        cameras.append({
            "camera_id": str(camera["camera_id"]),
            "label": str(camera.get("label") or camera["camera_id"]),
            "path": str(Path(camera["path"]).resolve()),
            "fps": float(camera.get("fps") or 100.0),
            "frame_count": int(camera.get("frame_count") or 0),
            "rotation": int(camera.get("rotation") or 0),
            "sync_offset_frames": int(camera.get("sync_offset_frames") or 0),
            "sync_group_id": str(camera["sync_group_id"]),
        })
    return {
        "session_id": str(package["session_id"]),
        "started_at": package.get("started_at"),
        "manifest_path": str(path.resolve()),
        "manifest_mtime_ns": int(path.stat().st_mtime_ns),
        "cameras": cameras,
    }


def cached_snapshot(path: Path) -> dict:
    path = Path(path).resolve()
    data = _load()
    for item in data.get("recordings", {}).values():
        if Path(item.get("manifest_path", "")).resolve() == path:
            if int(item.get("manifest_mtime_ns") or 0) == int(path.stat().st_mtime_ns):
                return item
    item = _snapshot(path)
    data.setdefault("recordings", {})[item["session_id"]] = item
    _save(data)
    return item


def discover_default_manifests(*, force_rescan: bool = False) -> list[Path]:
    root = default_recording_root().resolve()
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
