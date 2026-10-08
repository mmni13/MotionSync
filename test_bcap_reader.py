from pathlib import Path

from viewer.bcap_reader import BcapReader


path = Path(
    r"C:\Users\mmni\OneDrive - Team Danmark\Dokumenter"
    r"\GoPro_videoer\swimlab\Helena test"
    r"\camera-0-0001.bcap"
)

reader = BcapReader(
    path,
    fps=100.0,
)

reader.open()

print("Frames:", reader.frame_count)
print("Duration:", reader.duration_seconds)
print("First offset:", reader.offsets[0])
print("First length:", reader.lengths[0])

image = reader.read_image(0)

print("Image null:", image.isNull())
print("Image size:", image.width(), image.height())

reader.close()
