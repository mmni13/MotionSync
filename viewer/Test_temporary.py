from pathlib import Path


bcap_path = Path(
    r"C:\Users\mmni\OneDrive - Team Danmark\Dokumenter"
    r"\GoPro_videoer\swimlab\Helena test"
    r"\camera-0-0001.bcap"
)

output_dir = bcap_path.parent / "bcap_test_frames"
output_dir.mkdir(exist_ok=True)

data = bcap_path.read_bytes()

starts = []
ends = []

position = 0

while True:
    position = data.find(b"\xff\xd8\xff", position)

    if position < 0:
        break

    starts.append(position)
    position += 3

position = 0

while True:
    position = data.find(b"\xff\xd9", position)

    if position < 0:
        break

    ends.append(position + 2)
    position += 2

frames = []

end_index = 0

for start in starts:
    while (
        end_index < len(ends)
        and ends[end_index] <= start
    ):
        end_index += 1

    if end_index >= len(ends):
        break

    end = ends[end_index]
    frames.append((start, end))
    end_index += 1

print("Complete frames:", len(frames))

for frame_index in (
    0,
    len(frames) // 2,
    len(frames) - 1,
):
    start, end = frames[frame_index]

    output_path = (
        output_dir
        / f"frame_{frame_index:04d}.jpg"
    )

    output_path.write_bytes(
        data[start:end]
    )

    print(
        frame_index,
        start,
        end,
        end - start,
        output_path,
    )