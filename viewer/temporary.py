
import av
import statistics
from collections import Counter
from pathlib import Path

path = Path(
    r"C:\Users\mmni\OneDrive - Team Danmark\Dokumenter\GoPro_videoer\Iphone\IMG_E8655.MOV"
)

container = av.open(str(path))
stream = container.streams.video[0]

print("=== FILE AND STREAM METADATA ===")
print("File:", path)
print("File exists:", path.exists())
print("File size:", path.stat().st_size)
print("average_rate:", stream.average_rate)
print("base_rate:", stream.base_rate)
print("guessed_rate:", stream.guessed_rate)
print("time_base:", stream.time_base)
print("stream.frames:", stream.frames)
print("stream.duration:", stream.duration)
print("container.duration:", container.duration)
print("container metadata:", dict(container.metadata))
print("stream metadata:", dict(stream.metadata))

if stream.duration is not None and stream.time_base is not None:
    stream_duration = float(stream.duration * stream.time_base)
    print("stream duration seconds:", stream_duration)
else:
    stream_duration = None

if container.duration is not None:
    print(
        "container duration seconds:",
        float(container.duration / av.time_base),
    )

times = []
pts_values = []

print()
print("=== DECODING FIRST 5000 FRAME TIMESTAMPS ===")

for index, frame in enumerate(container.decode(stream)):
    if frame.pts is not None:
        pts_values.append(int(frame.pts))
        times.append(float(frame.pts * stream.time_base))

    if index >= 4999:
        break

container.close()

intervals = [
    times[index] - times[index - 1]
    for index in range(1, len(times))
    if times[index] > times[index - 1]
]

print("Decoded timestamps:", len(times))

if times:
    print("First timestamp:", times[0])
    print("Last timestamp:", times[-1])

    decoded_duration = times[-1] - times[0]

    print("Decoded timestamp duration:", decoded_duration)

    if decoded_duration > 0:
        print(
            "Effective FPS from decoded range:",
            (len(times) - 1) / decoded_duration,
        )

if intervals:
    median_interval = statistics.median(intervals)
    mean_interval = statistics.mean(intervals)

    print()
    print("=== FRAME INTERVAL ANALYSIS ===")
    print("Minimum interval:", min(intervals))
    print("Maximum interval:", max(intervals))
    print("Mean interval:", mean_interval)
    print("Median interval:", median_interval)
    print("Mean effective FPS:", 1.0 / mean_interval)
    print("Median effective FPS:", 1.0 / median_interval)

    rounded_intervals = [
        round(value, 7)
        for value in intervals
    ]

    interval_counts = Counter(rounded_intervals)

    print()
    print("Most common frame intervals:")

    for interval, count in interval_counts.most_common(20):
        effective_fps = (
            1.0 / interval
            if interval > 0
            else 0.0
        )

        print(
            f"  {interval:.7f} s"
            f" | {effective_fps:.3f} FPS"
            f" | {count} occurrences"
        )

    unique_intervals = sorted(interval_counts)

    print()
    print(
        "Number of unique rounded intervals:",
        len(unique_intervals),
    )

    print(
        "First 30 unique intervals:",
        unique_intervals[:30],
    )

    nominal_interval = (
        1.0 / float(stream.average_rate)
        if stream.average_rate
        else median_interval
    )

    larger_gaps = [
        interval
        for interval in intervals
        if interval > nominal_interval * 1.5
    ]

    print(
        "Intervals above 1.5x nominal:",
        len(larger_gaps),
    )

print()
print("=== FIRST 30 FRAME TIMESTAMPS ===")

for index, timestamp in enumerate(times[:30]):
    previous = (
        timestamp - times[index - 1]
        if index > 0
        else 0.0
    )

    print(
        f"{index:4d}: "
        f"time={timestamp:.9f} "
        f"delta={previous:.9f}"
    )
