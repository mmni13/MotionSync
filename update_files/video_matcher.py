from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import av


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
MANIFEST_NAMES = {"session.manifest.json", "swimlab-recording.json"}
VIDEO_TIMESTAMP_KEYS = (
    "com.apple.quicktime.creationdate",
    "creation_time",
    "date",
    "encoded_date",
    "tagged_date",
)
RUN_TIMESTAMP_KEYS = (
    "created",
    "timestamp",
    "startTime",
    "started",
    "date",
    "timeStamp",
)


@dataclass(frozen=True)
class VideoMetadata:
    path: Path
    timestamp: datetime
    duration_seconds: float
    timestamp_source: str
    used_filesystem_fallback: bool = False


@dataclass(frozen=True)
class VideoMatch:
    run: dict[str, Any]
    video: VideoMetadata
    run_timestamp: datetime
    clock_delta_seconds: float
    residual_seconds: float
    confidence: str


def _local_timezone():
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_timestamp(value: Any, *, default_tz=None) -> datetime | None:
    """Parse ISO/API timestamps and normalize them to timezone-aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("UTC "):
            text = text[4:].strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
            text = text[:-2] + ":" + text[-2:]
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in (
                "%Y:%m:%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S",
            ):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    pass
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz or _local_timezone())
    return parsed.astimezone(timezone.utc)


def extract_run_timestamp(run: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """Return the first reliable timestamp from a normalized 1080 run."""
    candidates: list[tuple[dict[str, Any], str]] = []
    if isinstance(run, dict):
        candidates.append((run, "run"))
        raw = run.get("_raw")
        if isinstance(raw, dict):
            candidates.append((raw, "raw"))
            motions = raw.get("motions") or []
            if isinstance(motions, list):
                candidates.extend(
                    (motion, f"motion[{index}]")
                    for index, motion in enumerate(motions)
                    if isinstance(motion, dict)
                )
        training_set = run.get("_training_set")
        if isinstance(training_set, dict):
            candidates.append((training_set, "training_set"))

    for mapping, prefix in candidates:
        for key in RUN_TIMESTAMP_KEYS:
            if key not in mapping:
                continue
            parsed = parse_timestamp(mapping.get(key))
            if parsed is not None:
                return parsed, f"{prefix}.{key}"
    return None, None


def read_video_metadata(
    path: Path,
    *,
    allow_filesystem_fallback: bool = False,
) -> VideoMetadata:
    """
    Read video duration and the best available embedded recording timestamp.

    Timestamp priority:

    1. com.apple.quicktime.creationdate
       The original capture date recorded by iPhone/Apple devices.

    2. creation_time
       Common timestamp used by GoPro and other cameras.

    3. Other embedded date fields.

    4. Filesystem modification time, only when explicitly enabled.

    Timestamp priority is evaluated across both container and stream metadata.
    This is important because an iPhone video may contain:
        container com.apple.quicktime.creationdate = original recording time
        stream creation_time = transfer/export time
    """
    path = Path(path).resolve()

    if path.name.lower() in MANIFEST_NAMES:
        from viewer.contemplas_catalog import cached_snapshot
        item = cached_snapshot(path)
        timestamp = parse_timestamp(item.get("started_at"))
        if timestamp is None:
            raise ValueError("CONTEMPLAS manifest has no usable startedAt timestamp")
        cameras = item.get("cameras") or []
        duration = 0.0
        if cameras:
            fps = float(cameras[0].get("fps") or 0.0)
            count = int(cameras[0].get("frame_count") or 0)
            duration = count / fps if fps > 0 else 0.0
        return VideoMetadata(
            path=path,
            timestamp=timestamp,
            duration_seconds=duration,
            timestamp_source="contemplas.manifest.startedAt",
            used_filesystem_fallback=False,
        )

    container = av.open(str(path))

    try:
        stream = container.streams.video[0]

        # --------------------------------------------------
        # Determine video duration
        # --------------------------------------------------

        duration = 0.0

        if (
            stream.duration is not None
            and stream.time_base is not None
        ):
            duration = float(
                stream.duration * stream.time_base
            )

        elif container.duration is not None:
            duration = float(
                container.duration / av.time_base
            )

        duration = max(0.0, duration)

        # --------------------------------------------------
        # Normalize all metadata keys to lowercase
        # --------------------------------------------------

        container_metadata = {
            str(key).lower(): value
            for key, value in dict(
                getattr(container, "metadata", {}) or {}
            ).items()
        }

        stream_metadata = {
            str(key).lower(): value
            for key, value in dict(
                getattr(stream, "metadata", {}) or {}
            ).items()
        }

        # --------------------------------------------------
        # Search by timestamp quality, not source order
        # --------------------------------------------------
        #
        # The previous implementation likely checked every
        # stream key before checking the container. That allowed
        # stream.creation_time to win before reaching the better
        # container.com.apple.quicktime.creationdate.
        #
        # Each key is now checked across all sources before moving
        # to the next, lower-priority key.
        # --------------------------------------------------

        timestamp_candidates = (
            (
                "com.apple.quicktime.creationdate",
                (
                    (
                        container_metadata.get(
                            "com.apple.quicktime.creationdate"
                        ),
                        "container.com.apple.quicktime.creationdate",
                    ),
                    (
                        stream_metadata.get(
                            "com.apple.quicktime.creationdate"
                        ),
                        "stream.com.apple.quicktime.creationdate",
                    ),
                ),
            ),
            (
                "creation_time",
                (
                    (
                        container_metadata.get("creation_time"),
                        "container.creation_time",
                    ),
                    (
                        stream_metadata.get("creation_time"),
                        "stream.creation_time",
                    ),
                ),
            ),
            (
                "date",
                (
                    (
                        container_metadata.get("date"),
                        "container.date",
                    ),
                    (
                        stream_metadata.get("date"),
                        "stream.date",
                    ),
                ),
            ),
            (
                "encoded_date",
                (
                    (
                        container_metadata.get("encoded_date"),
                        "container.encoded_date",
                    ),
                    (
                        stream_metadata.get("encoded_date"),
                        "stream.encoded_date",
                    ),
                ),
            ),
            (
                "tagged_date",
                (
                    (
                        container_metadata.get("tagged_date"),
                        "container.tagged_date",
                    ),
                    (
                        stream_metadata.get("tagged_date"),
                        "stream.tagged_date",
                    ),
                ),
            ),
        )

        for _key, candidates in timestamp_candidates:
            for raw_value, source_name in candidates:
                if raw_value is None:
                    continue

                parsed = parse_timestamp(raw_value)

                if parsed is None:
                    continue

                return VideoMetadata(
                    path=path,
                    timestamp=parsed,
                    duration_seconds=duration,
                    timestamp_source=source_name,
                    used_filesystem_fallback=False,
                )

    finally:
        container.close()

    # --------------------------------------------------
    # Optional filesystem fallback
    # --------------------------------------------------

    if not allow_filesystem_fallback:
        raise ValueError(
            f"No usable embedded recording timestamp found "
            f"in {path.name}"
        )

    fallback_timestamp = datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=timezone.utc,
    )

    return VideoMetadata(
        path=path,
        timestamp=fallback_timestamp,
        duration_seconds=duration,
        timestamp_source="filesystem.mtime",
        used_filesystem_fallback=True,
    )


def scan_video_folder(
    folder: Path,
    *,
    recursive: bool = False,
    allow_filesystem_fallback: bool = False,
):
    """Read normal videos and cached CONTEMPLAS manifests.

    CONTEMPLAS is discovered automatically below %APPDATA%/NTC SwimLab Analyse;
    users never need to browse to the hidden application-data directory.
    """
    from viewer.contemplas_catalog import discover_default_manifests

    folder = Path(folder).resolve()
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    candidates = [
        path for path in sorted(iterator)
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    candidates.extend(discover_default_manifests())

    videos: list[VideoMetadata] = []
    errors: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path in candidates:
        path = Path(path).resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            videos.append(
                read_video_metadata(
                    path,
                    allow_filesystem_fallback=allow_filesystem_fallback,
                )
            )
        except Exception as exc:
            errors.append((path, str(exc)))
    return videos, errors


def _assign_for_offset(
    runs_with_times,
    videos,
    offset_seconds: float,
    tolerance_seconds: float,
):
    """Build one maximum-quality one-video-per-run camera layer.

    A run and a video can each occur once in a layer. Equal-residual videos are
    deliberately ordered by timestamp/path rather than treated as ambiguous;
    remaining synchronized videos are available to the next layer.
    """
    edges = []
    for run_index, (_run, run_time, _source) in enumerate(runs_with_times):
        for video_index, video in enumerate(videos):
            delta = (video.timestamp - run_time).total_seconds()
            residual = abs(delta - float(offset_seconds))
            if residual <= float(tolerance_seconds):
                edges.append((
                    residual,
                    abs(delta - float(offset_seconds)),
                    run_index,
                    video_index,
                    delta,
                ))

    edges.sort(key=lambda item: (
        item[0],
        item[1],
        runs_with_times[item[2]][1],
        videos[item[3]].timestamp,
        str(videos[item[3]].path),
    ))

    assigned_runs: set[int] = set()
    assigned_videos: set[int] = set()
    assignments = []
    for residual, _distance, run_index, video_index, delta in edges:
        if run_index in assigned_runs or video_index in assigned_videos:
            continue
        assigned_runs.add(run_index)
        assigned_videos.add(video_index)
        assignments.append((run_index, video_index, delta, residual))

    assignments.sort(key=lambda item: item[0])
    return assignments


def _prepare_runs(runs):
    runs_with_times = []
    missing_timestamp = []
    for run in runs:
        run_time, source = extract_run_timestamp(run)
        if run_time is None:
            missing_timestamp.append({
                "run": run,
                "reason": "run has no timestamp",
            })
        else:
            runs_with_times.append((run, run_time, source))
    runs_with_times.sort(key=lambda item: item[1])
    return runs_with_times, missing_timestamp


def _best_camera_layer(
    runs_with_times,
    videos,
    *,
    expected_clock_offset_seconds: float,
    offset_search_seconds: float,
    residual_tolerance_seconds: float,
):
    """Find the strongest one-video-per-run layer in the remaining videos."""
    if not runs_with_times or not videos:
        return None

    hypotheses = {float(expected_clock_offset_seconds)}
    for _run, run_time, _source in runs_with_times:
        for video in videos:
            delta = (video.timestamp - run_time).total_seconds()
            if abs(delta - expected_clock_offset_seconds) <= offset_search_seconds:
                hypotheses.add(float(delta))

    evaluated = []
    for hypothesis in hypotheses:
        assignments = _assign_for_offset(
            runs_with_times,
            videos,
            hypothesis,
            residual_tolerance_seconds,
        )
        if not assignments:
            continue
        residual_sum = sum(item[3] for item in assignments)
        worst_residual = max(item[3] for item in assignments)
        prior_distance = abs(hypothesis - expected_clock_offset_seconds)
        key = (
            -len(assignments),
            residual_sum,
            worst_residual,
            prior_distance,
            hypothesis,
        )
        evaluated.append((key, hypothesis, assignments))

    if not evaluated:
        return None

    evaluated.sort(key=lambda item: item[0])
    _key, hypothesis, assignments = evaluated[0]

    # Refine with the median pair delta, then assign the layer once more.
    deltas = sorted(item[2] for item in assignments)
    middle = len(deltas) // 2
    if len(deltas) % 2:
        refined_offset = deltas[middle]
    else:
        refined_offset = (deltas[middle - 1] + deltas[middle]) / 2.0

    refined_assignments = _assign_for_offset(
        runs_with_times,
        videos,
        refined_offset,
        residual_tolerance_seconds,
    )
    if refined_assignments:
        assignments = refined_assignments

    return {
        "offset": float(refined_offset),
        "assignments": assignments,
    }


def _assignments_to_matches(runs_with_times, videos, assignments):
    matches = []
    for run_index, video_index, delta, residual in assignments:
        run, run_time, _source = runs_with_times[run_index]
        matches.append(VideoMatch(
            run=run,
            video=videos[video_index],
            run_timestamp=run_time,
            clock_delta_seconds=float(delta),
            residual_seconds=float(residual),
            confidence="high" if residual <= 2.0 else "medium",
        ))
    return matches


def match_runs_to_videos(
    runs: Iterable[dict[str, Any]],
    videos: Iterable[VideoMetadata],
    *,
    expected_clock_offset_seconds: float = 58.0,
    offset_search_seconds: float = 90.0,
    residual_tolerance_seconds: float = 8.0,
    ambiguity_margin_seconds: float = 2.0,
):
    """Conservative legacy matcher for one expected video per run.

    This retains ambiguity rejection because callers using this function are
    explicitly asking for one video per run. Multi-angle Auto-match uses
    ``match_runs_to_multiple_videos`` below and does not reject parallel cameras
    merely because their timestamps are equally good.
    """
    runs = list(runs)
    videos = sorted(
        list(videos),
        key=lambda item: (item.timestamp, str(item.path)),
    )
    runs_with_times, missing_timestamp = _prepare_runs(runs)

    layer = _best_camera_layer(
        runs_with_times,
        videos,
        expected_clock_offset_seconds=expected_clock_offset_seconds,
        offset_search_seconds=offset_search_seconds,
        residual_tolerance_seconds=residual_tolerance_seconds,
    )
    if layer is None:
        return {
            "matches": [],
            "ambiguous": [],
            "unmatched": missing_timestamp + [
                {"run": run, "reason": "no video fits the session clock offset"}
                for run, _time, _source in runs_with_times
            ],
            "unused_videos": videos,
            "estimated_clock_offset_seconds": None,
        }

    refined_offset = layer["offset"]
    assignments = layer["assignments"]
    accepted_assignments = []
    ambiguous = []

    for run_index, video_index, delta, residual in assignments:
        run, run_time, source = runs_with_times[run_index]
        alternatives = []
        for other_index, other_video in enumerate(videos):
            if other_index == video_index:
                continue
            other_delta = (other_video.timestamp - run_time).total_seconds()
            other_residual = abs(other_delta - refined_offset)
            if other_residual <= residual_tolerance_seconds:
                alternatives.append((other_residual, other_video))
        alternatives.sort(key=lambda item: (item[0], str(item[1].path)))

        if (
            ambiguity_margin_seconds > 0
            and alternatives
            and alternatives[0][0] - residual < ambiguity_margin_seconds
        ):
            ambiguous.append({
                "run": run,
                "run_timestamp": run_time,
                "timestamp_source": source,
                "candidates": [
                    (residual, videos[video_index]),
                    *alternatives[:2],
                ],
            })
        else:
            accepted_assignments.append(
                (run_index, video_index, delta, residual)
            )

    matches = _assignments_to_matches(
        runs_with_times,
        videos,
        accepted_assignments,
    )
    used_video_indexes = {item[1] for item in accepted_assignments}
    accepted_run_indexes = {item[0] for item in accepted_assignments}
    ambiguous_run_ids = {id(item["run"]) for item in ambiguous}

    unmatched = list(missing_timestamp)
    for run_index, (run, run_time, source) in enumerate(runs_with_times):
        if run_index not in accepted_run_indexes and id(run) not in ambiguous_run_ids:
            unmatched.append({
                "run": run,
                "run_timestamp": run_time,
                "timestamp_source": source,
                "reason": "no unique video fits the session clock offset",
            })

    return {
        "matches": matches,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "unused_videos": [
            video for index, video in enumerate(videos)
            if index not in used_video_indexes
        ],
        "estimated_clock_offset_seconds": refined_offset,
    }


def match_runs_to_multiple_videos(
    runs: Iterable[dict[str, Any]],
    videos: Iterable[VideoMetadata],
    *,
    expected_clock_offset_seconds: float = 58.0,
    offset_search_seconds: float = 90.0,
    residual_tolerance_seconds: float = 8.0,
    ambiguity_margin_seconds: float = 2.0,
    minimum_additional_group_matches: int = 2,
):
    """Match multiple synchronized or unsynchronized camera layers per run.

    Parallel videos are not ambiguity competitors. The strongest one-video-per-
    run layer is extracted, its files are removed, and the remaining files are
    searched again. Equal timestamps therefore become Camera 1, Camera 2, etc.
    instead of causing every run to be rejected as ambiguous.

    ``ambiguity_margin_seconds`` is accepted for API compatibility but is not
    applied across parallel camera candidates in this multi-angle function.
    """
    del ambiguity_margin_seconds

    runs = list(runs)
    remaining_videos = sorted(
        list(videos),
        key=lambda item: (item.timestamp, str(item.path)),
    )
    runs_with_times, missing_timestamp = _prepare_runs(runs)

    groups = []
    all_matches = []
    used_paths: set[Path] = set()
    matched_run_ids: set[str] = set()
    rejected_layers = []

    while remaining_videos:
        layer = _best_camera_layer(
            runs_with_times,
            remaining_videos,
            expected_clock_offset_seconds=expected_clock_offset_seconds,
            offset_search_seconds=offset_search_seconds,
            residual_tolerance_seconds=residual_tolerance_seconds,
        )
        if layer is None:
            break

        assignments = layer["assignments"]
        if not assignments:
            break

        group_index = len(groups) + 1
        if group_index > 1:
            required = max(1, int(minimum_additional_group_matches))
            if len(assignments) < required:
                rejected_layers.append({
                    "estimated_clock_offset_seconds": layer["offset"],
                    "match_count": len(assignments),
                    "reason": (
                        f"additional group has {len(assignments)} match(es); "
                        f"requires {required}"
                    ),
                })
                break

        group_matches = _assignments_to_matches(
            runs_with_times,
            remaining_videos,
            assignments,
        )
        groups.append({
            "index": group_index,
            "matches": group_matches,
            "estimated_clock_offset_seconds": layer["offset"],
        })
        all_matches.extend(group_matches)

        selected_paths = {
            remaining_videos[video_index].path.resolve()
            for _run_index, video_index, _delta, _residual in assignments
        }
        used_paths.update(selected_paths)

        for match in group_matches:
            matched_run_ids.add(str(
                match.run.get("id")
                or match.run.get("index")
                or id(match.run)
            ))

        next_remaining = [
            video for video in remaining_videos
            if video.path.resolve() not in selected_paths
        ]
        if len(next_remaining) == len(remaining_videos):
            break
        remaining_videos = next_remaining

    unmatched = list(missing_timestamp)
    for run, run_time, source in runs_with_times:
        run_id = str(run.get("id") or run.get("index") or id(run))
        if run_id not in matched_run_ids:
            unmatched.append({
                "run": run,
                "run_timestamp": run_time,
                "timestamp_source": source,
                "reason": "no camera layer fits the session clock model",
            })

    return {
        "matches": all_matches,
        "match_groups": groups,
        # Parallel synchronized cameras are valid, not ambiguous.
        "ambiguous": [],
        "unmatched": unmatched,
        "unused_videos": remaining_videos,
        "estimated_clock_offset_seconds": (
            groups[0]["estimated_clock_offset_seconds"]
            if groups else None
        ),
        "estimated_clock_offsets_seconds": [
            group["estimated_clock_offset_seconds"]
            for group in groups
        ],
        "rejected_camera_layers": rejected_layers,
    }
