from __future__ import annotations

import ast
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
MAIN = VIEWER / "main_window.py"

if not MAIN.exists():
    raise SystemExit("Place this file beside VP.py and run it from the MotionSync project root.")

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / f"viewer_backup_before_automatch_LED_{stamp}"
backup.mkdir()
shutil.copy2(MAIN, backup / "main_window.py")

text = MAIN.read_text(encoding="utf-8")

# Import the catalog snapshot helper used to identify the cameras belonging to
# the package that Auto-match just attached.
anchor = "from viewer.contemplas_catalog import attach_manifest_package\n"
replacement = (
    "from viewer.contemplas_catalog import "
    "attach_manifest_package, cached_snapshot\n"
)
if replacement not in text:
    if anchor not in text:
        raise RuntimeError(
            "The CONTEMPLAS Auto-match import was not found. Install the known-path Auto-match update first."
        )
    text = text.replace(anchor, replacement, 1)

# Insert a GUI helper before the existing manual camera-angle method. This does
# not start background decoding. It only stores the chosen camera as active and
# clears stale synchronization for that one synchronized package.
method_anchor = "    def _add_camera_angle(self):\n"
if "def _choose_automatched_led_camera" not in text:
    helper = '''    def _choose_automatched_led_camera(self, identity, manifest_path: Path) -> None:
        """Choose which camera in an automatically matched package sees the LED."""
        snapshot = cached_snapshot(manifest_path)
        package_paths = {
            str(Path(camera["path"]).resolve())
            for camera in snapshot.get("cameras") or []
        }
        attached = [
            camera
            for camera in self.run_library.get_videos(identity)
            if camera.get("path")
            and str(Path(camera["path"]).resolve()) in package_paths
        ]
        if not attached:
            return

        labels = [
            str(camera.get("label") or Path(camera["path"]).stem)
            for camera in attached
        ]
        selected_label, accepted = QInputDialog.getItem(
            self,
            "Select LED synchronization camera",
            "Select the camera angle that can see the synchronization LED:",
            labels,
            0,
            False,
            flags=Qt.Dialog | Qt.WindowStaysOnTopHint,
        )
        if not accepted:
            return

        selected = attached[labels.index(selected_label)]
        selected_id = str(selected["id"])
        self.run_library.set_active_video(identity, selected_id)

        details = dict(selected.get("match_details") or {})
        group_id = details.get("sync_group_id")
        if group_id:
            run_meta = self.run_library.ensure_run_meta(identity)
            groups = dict(run_meta.get("synchronized_camera_groups") or {})
            groups.pop(str(group_id), None)
            run_meta["synchronized_camera_groups"] = groups
            self.run_library.save_run_meta(identity, run_meta)

        selected_path = Path(selected["path"]).resolve()
        selected_cache_dir = cache_dir_for_video(selected_path, self.video.cache_root)
        try:
            (selected_cache_dir / "sync_meta.json").unlink(missing_ok=True)
        except Exception:
            pass

        cache_meta_file = selected_cache_dir / "cache_meta.json"
        if cache_meta_file.exists():
            cache_meta = _read_json(cache_meta_file)
            for key in (
                "led_frame",
                "sync_frame",
                "led_time_seconds",
                "sync_time_seconds",
                "data_delay_ms",
                "user_offset_frames",
            ):
                cache_meta.pop(key, None)
            _write_json(cache_meta_file, cache_meta)

        if self.current_run_identity == identity:
            self.active_camera_id = selected_id
            self._load_camera_views(identity)
            self._rebuild_camera_strip()

'''
    if method_anchor not in text:
        raise RuntimeError("Could not locate _add_camera_angle() in main_window.py")
    text = text.replace(method_anchor, helper + method_anchor, 1)

# Add the selection call directly after package attachment. The call is guarded
# so rerunning this installer will not duplicate it.
if "self._choose_automatched_led_camera(" not in text:
    pattern = re.compile(
        r"(?P<indent>\s*)attach_manifest_package\(\s*\n"
        r"(?P=indent)\s*self\.run_library,\s*\n"
        r"(?P=indent)\s*match_identity,\s*\n"
        r"(?P=indent)\s*match\.video\.path,\s*\n"
        r"(?P=indent)\s*\)"
    )
    match = pattern.search(text)
    if match:
        indent = match.group("indent")
        original = match.group(0)
        addition = (
            original
            + "\n"
            + indent
            + "self._choose_automatched_led_camera(\n"
            + indent
            + "    match_identity, match.video.path\n"
            + indent
            + ")"
        )
        text = text[: match.start()] + addition + text[match.end() :]
    else:
        one_line = (
            "attach_manifest_package(self.run_library, match_identity, match.video.path)"
        )
        if one_line not in text:
            raise RuntimeError("Could not locate the CONTEMPLAS package attachment call.")
        text = text.replace(
            one_line,
            one_line
            + "\n                    self._choose_automatched_led_camera("
            + "match_identity, match.video.path)",
            1,
        )

ast.parse(text, filename=str(MAIN))
compile(text, str(MAIN), "exec")
MAIN.write_text(text, encoding="utf-8")

pycache = VIEWER / "__pycache__"
if pycache.exists():
    shutil.rmtree(pycache)

print("Auto-match LED-camera selection installed successfully.")
print("Backup:", backup)
