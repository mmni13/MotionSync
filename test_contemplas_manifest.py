from pathlib import Path

from viewer.contemplas_manifest import (
    load_contemplas_manifest,
)


path = Path(
    r"C:\Users\mmni\OneDrive - Team Danmark\Dokumenter"
    r"\GoPro_videoer\swimlab\Helena test"
    r"\session.manifest.json"
)

package = load_contemplas_manifest(path)

print("Session:", package["session_id"])
print("FPS:", package["fps"])
print("Cameras:", len(package["cameras"]))

for camera in package["cameras"]:
    print()
    print("Label:", camera["label"])
    print("Path:", camera["path"])
    print("Exists:", camera["path"].exists())
    print("FPS:", camera["fps"])
    print("Frames:", camera["frame_count"])
    print("Sync offset:", camera["sync_offset_frames"])
