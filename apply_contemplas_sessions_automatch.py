from __future__ import annotations
import ast
import shutil
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
SOURCE = ROOT / "update_files" / "main_window.py"
TARGET = VIEWER / "main_window.py"
if not TARGET.exists():
    raise SystemExit("Run this installer from the MotionSync project root beside VP.py.")
if not SOURCE.exists():
    raise SystemExit("Missing update_files/main_window.py. Extract the complete update first.")
source_text = SOURCE.read_text(encoding="utf-8")
ast.parse(source_text, filename=str(SOURCE))
compile(source_text, str(SOURCE), "exec")
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / f"viewer_backup_before_sessions_automatch_{stamp}"
backup.mkdir(parents=True)
shutil.copy2(TARGET, backup / "main_window.py")
shutil.copy2(SOURCE, TARGET)
cache = VIEWER / "__pycache__"
if cache.exists():
    shutil.rmtree(cache)
compile(TARGET.read_text(encoding="utf-8"), str(TARGET), "exec")
print("CONTEMPLAS sessions Auto-match installed successfully.")
print("Backup:", backup)
print(r"Scanned root: %APPDATA%\NTC SwimLab Analyse\accounts")
