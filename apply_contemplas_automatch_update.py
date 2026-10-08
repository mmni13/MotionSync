from __future__ import annotations
import ast
import shutil
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parent
viewer = root / "viewer"
if not (viewer / "main_window.py").exists():
    raise SystemExit("Extract this update into the MotionSync project root beside VP.py.")

backup = root / ("viewer_backup_before_contemplas_automatch_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
backup.mkdir()
for name in ("main_window.py", "video_matcher.py"):
    shutil.copy2(viewer / name, backup / name)

shutil.copy2(root / "update_files" / "video_matcher.py", viewer / "video_matcher.py")
shutil.copy2(root / "update_files" / "contemplas_catalog.py", viewer / "contemplas_catalog.py")

path = viewer / "main_window.py"
text = path.read_text(encoding="utf-8")
anchor = "from viewer.video_matcher import scan_video_folder, match_runs_to_multiple_videos\n"
addition = "from viewer.contemplas_catalog import attach_manifest_package\n"
if addition not in text:
    if anchor not in text:
        raise RuntimeError("Could not find the video_matcher import in main_window.py")
    text = text.replace(anchor, anchor + addition, 1)
text = text.replace("offset_search_seconds=120.0,", "offset_search_seconds=120.0,")
text = text.replace("offset_search_seconds=300.0,", "offset_search_seconds=120.0,")
needle = "            for match in matches:\n                match_identity = self._identity_for_run(match.run)\n                existing_videos = self.run_library.get_videos(match_identity)\n"
replacement = "            for match in matches:\n                match_identity = self._identity_for_run(match.run)\n                if match.video.path.name.lower() in {\n                    'session.manifest.json',\n                    'swimlab-recording.json',\n                }:\n                    attach_manifest_package(\n                        self.run_library, match_identity, match.video.path\n                    )\n                    continue\n                existing_videos = self.run_library.get_videos(match_identity)\n"
if needle not in text:
    raise RuntimeError("Could not find the Auto-match attachment loop in main_window.py")
text = text.replace(needle, replacement, 1)
ast.parse(text)
compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

cache = viewer / "__pycache__"
if cache.exists():
    shutil.rmtree(cache)
for name in ("main_window.py", "video_matcher.py", "contemplas_catalog.py"):
    compile((viewer / name).read_text(encoding="utf-8"), str(viewer / name), "exec")
print("CONTEMPLAS Auto-match installed.")
print("Backup:", backup)
print("Recording root: %APPDATA%\\NTC SwimLab Analyse\\accounts")
print("Catalog: %LOCALAPPDATA%\\1080SyncViewer\\contemplas_recordings.json")
