from __future__ import annotations

import ast
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
MATCHER = VIEWER / "video_matcher.py"
MAIN = VIEWER / "main_window.py"

if not MATCHER.exists() or not MAIN.exists():
    raise SystemExit(
        "Place this script in the MotionSync project root beside VP.py, "
        "then run it again."
    )

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / f"viewer_backup_before_offset_fix_{stamp}"
backup.mkdir()
shutil.copy2(MATCHER, backup / MATCHER.name)
shutil.copy2(MAIN, backup / MAIN.name)

text = MATCHER.read_text(encoding="utf-8")

# Remove the faulty statement wherever a previous patch inserted it. If placed
# in a function without offset_search_seconds as an argument, Python treats the
# variable as local and raises UnboundLocalError before matching begins.
text = re.sub(
    r"(?m)^\s*offset_search_seconds\s*=\s*min\(\s*float\(offset_search_seconds\)\s*,\s*90(?:\.0)?\s*\)\s*\n",
    "",
    text,
)

# Put the limit at the actual API boundary: the function parameter default.
text = text.replace(
    "offset_search_seconds: float = 120.0,",
    "offset_search_seconds: float = 90.0,",
)
text = text.replace(
    "offset_search_seconds: float = 300.0,",
    "offset_search_seconds: float = 90.0,",
)

# Verify that the matcher function really has the parameter after editing.
tree = ast.parse(text, filename=str(MATCHER))
matcher_function = next(
    (
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "match_runs_to_multiple_videos"
    ),
    None,
)
if matcher_function is None:
    raise RuntimeError("match_runs_to_multiple_videos() was not found.")
parameter_names = {
    argument.arg
    for argument in (
        list(matcher_function.args.args)
        + list(matcher_function.args.kwonlyargs)
    )
}
if "offset_search_seconds" not in parameter_names:
    raise RuntimeError(
        "match_runs_to_multiple_videos() has no offset_search_seconds parameter."
    )

compile(text, str(MATCHER), "exec")
MATCHER.write_text(text, encoding="utf-8")

# Keep the explicit UI call at 90 seconds as well.
main_text = MAIN.read_text(encoding="utf-8")
main_text = re.sub(
    r"offset_search_seconds\s*=\s*(?:120|300)(?:\.0)?\s*,",
    "offset_search_seconds=90.0,",
    main_text,
)
ast.parse(main_text, filename=str(MAIN))
compile(main_text, str(MAIN), "exec")
MAIN.write_text(main_text, encoding="utf-8")

pycache = VIEWER / "__pycache__"
if pycache.exists():
    shutil.rmtree(pycache)

print("Offset-search scope fix installed successfully.")
print("Backup:", backup)
print("Matching range: 90 seconds")
