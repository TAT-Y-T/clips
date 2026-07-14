from __future__ import annotations

import csv
import json
import argparse
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VIEWS = ("primary", "left_hand", "right_hand", "secondary")
GRID_VIEWS = ("primary", "secondary", "left_hand", "right_hand")
# Crop parameters from the supplied left-eye crop script.
LEFT_ONLY_CROP_FILTER = "crop=1920:1200:160:0"
ANNOTATION_TIMEBASES = ("raw-primary", "aligned")
TIMELINE_FILES = {
    "src": "pts_src_{view}.csv",
    "encoded": "pts_encoded_{view}.csv",
    "decoded": "pts_decoded_{view}.csv",
    "timestamps": "timestamps_{view}.csv",
}


@dataclass(frozen=True)
class TimestampRow:
    frame: int
    pts_ns: int
    wallclock_ns: int


@dataclass(frozen=True)
class FrameTimeRow:
    frame: int
    pts_ns: int
    wallclock_ns: int
    mp4_frame: int
    mp4_pts_ns: int
    mp4_time_ns: int
    pts_match_error_ns: int


def load_timestamp_csv(path: Path) -> list[TimestampRow]:
    rows: list[TimestampRow] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for line_number, row in enumerate(reader, start=2):
            if not row or all(value in (None, "") for value in row.values()):
                continue
            if any(row.get(field) in (None, "") for field in ("frame", "pts_ns", "wallclock_ns")):
                continue
            try:
                frame = int(row["frame"])
                pts_ns = int(row["pts_ns"])
                wallclock_ns = int(row["wallclock_ns"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid timestamp row in {path} at line {line_number}") from exc
            rows.append(TimestampRow(frame=frame, pts_ns=pts_ns, wallclock_ns=wallclock_ns))
    if not rows:
        raise ValueError(f"No timestamp rows found in {path}")
    while len(rows) >= 2 and (
        rows[-1].pts_ns <= rows[-2].pts_ns
        or rows[-1].wallclock_ns <= rows[-2].wallclock_ns
    ):
        rows.pop()
    return rows


def load_mp4_frame_pts_ns(path: Path) -> list[int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    output = subprocess.check_output(command, text=True)
    pts_ns: list[int] = []
    for line in output.splitlines():
        value = line.strip().split(",", 1)[0]
        if not value:
            continue
        pts_ns.append(round(float(value) * 1_000_000_000))
    pts_ns = sorted(set(pts_ns))
    if not pts_ns:
        raise ValueError(f"No decoded frame timestamps found in {path}")
    return pts_ns


def build_validated_timeline(
    timestamp_rows: list[TimestampRow],
    mp4_pts_ns: list[int],
    tolerance_ns: int = 20_000_000,
) -> list[FrameTimeRow]:
    if not timestamp_rows:
        raise ValueError("Cannot validate an empty timestamp list")
    if not mp4_pts_ns:
        raise ValueError("Cannot validate against an empty MP4 frame list")

    csv_relative_pts = [row.pts_ns - timestamp_rows[0].pts_ns for row in timestamp_rows]
    mp4_first_pts_ns = mp4_pts_ns[0]
    timeline: list[FrameTimeRow] = []
    search_start = 0

    for mp4_index, mp4_pts in enumerate(mp4_pts_ns, start=1):
        mp4_time_ns = mp4_pts - mp4_first_pts_ns
        best_index = search_start
        best_error = abs(csv_relative_pts[best_index] - mp4_time_ns)
        candidate_index = search_start + 1
        while candidate_index < len(csv_relative_pts):
            error = abs(csv_relative_pts[candidate_index] - mp4_time_ns)
            if error > best_error and csv_relative_pts[candidate_index] > mp4_time_ns:
                break
            if error < best_error:
                best_index = candidate_index
                best_error = error
            candidate_index += 1

        if best_error <= tolerance_ns:
            row = timestamp_rows[best_index]
            timeline.append(
                FrameTimeRow(
                    frame=row.frame,
                    pts_ns=row.pts_ns,
                    wallclock_ns=row.wallclock_ns,
                    mp4_frame=mp4_index,
                    mp4_pts_ns=mp4_pts,
                    mp4_time_ns=mp4_time_ns,
                    pts_match_error_ns=best_error,
                )
            )
            search_start = best_index + 1
            if search_start >= len(csv_relative_pts):
                break
        else:
            search_start = best_index

    if not timeline:
        raise ValueError("No MP4 frames matched timestamp rows within tolerance")
    return timeline


def nearest_by_value(
    rows: list[TimestampRow] | list[FrameTimeRow],
    attr: str,
    target: int,
) -> tuple[TimestampRow | FrameTimeRow, int]:
    if not rows:
        raise ValueError("Cannot search an empty timestamp list")
    row = min(rows, key=lambda candidate: abs(getattr(candidate, attr) - target))
    return row, abs(getattr(row, attr) - target)


def _relative_pts_ns(row: TimestampRow, rows: list[TimestampRow]) -> int:
    return row.pts_ns - rows[0].pts_ns


def map_primary_time_to_wallclock(
    rows: list[TimestampRow] | list[FrameTimeRow],
    seconds: float,
) -> tuple[TimestampRow | FrameTimeRow, int]:
    target_time_ns = round(seconds * 1_000_000_000)
    if isinstance(rows[0], FrameTimeRow):
        return nearest_by_value(rows, "mp4_time_ns", target_time_ns)
    target_pts_ns = rows[0].pts_ns + target_time_ns
    return nearest_by_value(rows, "pts_ns", target_pts_ns)


def map_wallclock_to_view_time(
    rows: list[TimestampRow] | list[FrameTimeRow],
    wallclock_ns: int,
) -> tuple[TimestampRow | FrameTimeRow, float, int]:
    row, error_ns = nearest_by_value(rows, "wallclock_ns", wallclock_ns)
    if isinstance(row, FrameTimeRow):
        relative_sec = row.mp4_time_ns / 1_000_000_000
    else:
        relative_sec = _relative_pts_ns(row, rows) / 1_000_000_000
    return row, relative_sec, error_ns


def row_ffmpeg_sec(row: TimestampRow | FrameTimeRow, fallback_relative_sec: float) -> float:
    if isinstance(row, FrameTimeRow):
        return row.mp4_pts_ns / 1_000_000_000
    return fallback_relative_sec


def recording_video_path(recording_dir: Path, view: str, input_subdir: str | None = None) -> Path:
    video_dir = recording_dir / input_subdir if input_subdir else recording_dir
    return video_dir / f"recording_{view}.mp4"


def has_recording_inputs(recording_dir: Path, input_subdir: str | None = None) -> bool:
    return (recording_dir / "_timestamps").is_dir() and all(
        recording_video_path(recording_dir, view, input_subdir).exists()
        for view in VIEWS
    )


def has_aligned_inputs(recording_dir: Path) -> bool:
    return all((recording_dir / f"recording_{view}.mp4").exists() for view in VIEWS) and all(
        (recording_dir / f"aligned_timestamps_{view}.csv").exists()
        for view in VIEWS
    )


def find_recording_dirs(root: Path, input_subdir: str | None = None) -> list[Path]:
    root = root.expanduser().resolve()
    if has_recording_inputs(root, input_subdir) or (input_subdir is None and has_aligned_inputs(root)):
        return [root]
    if not root.is_dir():
        return []
    recording_dirs: set[Path] = set()
    for timestamps_dir in root.rglob("_timestamps"):
        candidate = timestamps_dir.parent
        if has_recording_inputs(candidate):
            recording_dirs.add(candidate)
        if input_subdir and candidate.name != input_subdir and has_recording_inputs(candidate, input_subdir):
            recording_dirs.add(candidate)
    if input_subdir is None:
        for aligned_dir in root.rglob("aligned"):
            if has_aligned_inputs(aligned_dir):
                recording_dirs.add(aligned_dir)
    return sorted(recording_dirs)


def default_annotation_path(recording_dir: Path) -> Path:
    candidates = [
        recording_dir / "annotation.json",
        recording_dir / "anotation.json",
        recording_dir / "anootation.json",
    ]
    if recording_dir.name == "aligned":
        candidates.extend(
            [recording_dir.parent / candidate.name for candidate in candidates]
        )
    return next((candidate for candidate in candidates if candidate.exists()), candidates[0])


def is_segments_annotation(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload.get("instances", {}).get("segments"), list)


def load_annotations(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)

    sections = payload.get("section")
    if isinstance(sections, list):
        raw_sections = sections
    else:
        segments = payload.get("instances", {}).get("segments")
        if not isinstance(segments, list):
            raise ValueError(f"{path} must contain section[] or instances.segments[]")
        raw_sections = []
        for segment_index, segment in enumerate(segments, start=1):
            ranges = segment.get("group")
            if not isinstance(ranges, list):
                raise ValueError(f"Segment {segment_index} must contain a group list")
            normalized_ranges = []
            for range_index, time_range in enumerate(ranges):
                if not isinstance(time_range, dict):
                    raise ValueError(f"Segment {segment_index} group {range_index} must be an object")
                if "startTime" not in time_range or "endTime" not in time_range:
                    raise ValueError(f"Segment {segment_index} group {range_index} needs startTime/endTime")
                normalized_ranges.append([time_range["startTime"], time_range["endTime"]])
            attributes = segment.get("attributes") or {}
            description = "\n".join(str(value) for value in attributes.values() if value not in (None, ""))
            raw_sections.append({
                "id": segment.get("id", segment_index),
                "time": normalized_ranges,
                "description": description,
            })

    normalized_sections: list[dict[str, Any]] = []
    for section_index, section in enumerate(raw_sections):
        section_id = section.get("id", section_index + 1)
        ranges = section.get("time")
        if not isinstance(ranges, list):
            raise ValueError(f"Section {section_id} must contain a time list")
        normalized_ranges: list[list[float]] = []
        for range_index, time_range in enumerate(ranges):
            if (
                not isinstance(time_range, list)
                or len(time_range) != 2
                or not all(isinstance(value, (int, float)) for value in time_range)
            ):
                raise ValueError(f"Section {section_id} range {range_index} must be [start, end]")
            start_sec = float(time_range[0])
            end_sec = float(time_range[1])
            if start_sec >= end_sec:
                raise ValueError(f"Section {section_id} range {range_index} start must be less than end")
            normalized_ranges.append([start_sec, end_sec])
        normalized_sections.append({
            "id": section_id,
            "time": normalized_ranges,
            "description": str(section.get("description", "")),
        })
    return normalized_sections


def build_left_only_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    crop_filter: str = LEFT_ONLY_CROP_FILTER,
) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-ss", f"{start_sec:.6f}",
        "-t", f"{end_sec - start_sec:.6f}",
        "-vf", f"{crop_filter},setpts=PTS-STARTPTS",
        "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", "15M", "-maxrate", "15M", "-bufsize", "30M",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]


def build_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    reencode: bool,
) -> list[str]:
    if reencode:
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-ss",
            f"{start_sec:.6f}",
            "-t",
            f"{end_sec - start_sec:.6f}",
        ]
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac"])
    else:
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start_sec:.6f}",
            "-to",
            f"{end_sec:.6f}",
            "-i",
            str(input_path),
        ]
        command.extend(["-c", "copy"])
    command.append(str(output_path))
    return command


def build_grid_ffmpeg_command(
    input_paths: dict[str, Path],
    output_path: Path,
    cell_width: int,
    cell_height: int,
    fps: int = 30,
) -> list[str]:
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for view in GRID_VIEWS:
        command.extend(["-i", str(input_paths[view])])

    filters = []
    for index in range(len(GRID_VIEWS)):
        filters.append(
            f"[{index}:v]setpts=PTS-STARTPTS,fps={fps},"
            f"scale={cell_width}:{cell_height}:force_original_aspect_ratio=decrease,"
            f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{index}]"
        )
    filters.append(
        f"[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|{cell_width}_0|0_{cell_height}|{cell_width}_{cell_height}[v]"
    )

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-r",
            str(fps),
            "-vsync",
            "cfr",
            "-shortest",
            str(output_path),
        ]
    )
    return command


def build_alignment_plan(
    recording_dir: Path,
    output_dir: Path | None = None,
    timeline: str = "timestamps",
    validate_mp4_frames: bool = True,
    input_subdir: str | None = None,
) -> dict[str, Any]:
    if timeline not in TIMELINE_FILES:
        raise ValueError(f"Unknown timeline {timeline!r}. Expected one of {', '.join(TIMELINE_FILES)}")
    recording_dir = recording_dir.resolve()
    output_dir = (output_dir or recording_dir / "aligned").resolve()

    for view in VIEWS:
        video_path = recording_video_path(recording_dir, view, input_subdir)
        if not video_path.exists():
            raise FileNotFoundError(video_path)

    raw_timestamp_rows = {
        view: load_timestamp_csv(
            recording_dir / "_timestamps" / TIMELINE_FILES[timeline].format(view=view)
        )
        for view in VIEWS
    }
    if validate_mp4_frames:
        timestamp_rows = {
            view: build_validated_timeline(
                raw_timestamp_rows[view],
                load_mp4_frame_pts_ns(recording_video_path(recording_dir, view, input_subdir)),
            )
            for view in VIEWS
        }
    else:
        timestamp_rows = raw_timestamp_rows

    overlap_start_wallclock_ns = max(rows[0].wallclock_ns for rows in timestamp_rows.values())
    overlap_end_wallclock_ns = min(rows[-1].wallclock_ns for rows in timestamp_rows.values())
    if overlap_start_wallclock_ns >= overlap_end_wallclock_ns:
        raise ValueError(
            "No shared wallclock overlap across all views: "
            f"start={overlap_start_wallclock_ns} end={overlap_end_wallclock_ns}"
        )

    planned_views: dict[str, Any] = {}
    for view in VIEWS:
        view_rows = timestamp_rows[view]
        start_row, start_sec, start_wallclock_error_ns = map_wallclock_to_view_time(
            view_rows,
            overlap_start_wallclock_ns,
        )
        end_row, end_sec, end_wallclock_error_ns = map_wallclock_to_view_time(
            view_rows,
            overlap_end_wallclock_ns,
        )
        ffmpeg_start_sec = row_ffmpeg_sec(start_row, start_sec)
        ffmpeg_end_sec = row_ffmpeg_sec(end_row, end_sec)
        if ffmpeg_start_sec >= ffmpeg_end_sec:
            raise ValueError(f"Mapped aligned end is not after start for view {view}")
        planned_views[view] = {
            "input_path": str(recording_video_path(recording_dir, view, input_subdir)),
            "output_path": str(output_dir / f"recording_{view}.mp4"),
            "start_sec": start_sec,
            "end_sec": end_sec,
            "ffmpeg_start_sec": ffmpeg_start_sec,
            "ffmpeg_end_sec": ffmpeg_end_sec,
            "start_frame": start_row.frame,
            "end_frame": end_row.frame,
            "start_mp4_frame": getattr(start_row, "mp4_frame", None),
            "end_mp4_frame": getattr(end_row, "mp4_frame", None),
            "start_mp4_pts_ns": getattr(start_row, "mp4_pts_ns", None),
            "end_mp4_pts_ns": getattr(end_row, "mp4_pts_ns", None),
            "start_pts_match_error_ns": getattr(start_row, "pts_match_error_ns", None),
            "end_pts_match_error_ns": getattr(end_row, "pts_match_error_ns", None),
            "start_wallclock_ns": start_row.wallclock_ns,
            "end_wallclock_ns": end_row.wallclock_ns,
            "start_wallclock_error_ns": start_wallclock_error_ns,
            "end_wallclock_error_ns": end_wallclock_error_ns,
        }

    return {
        "recording_dir": str(recording_dir),
        "input_subdir": input_subdir,
        "output_dir": str(output_dir),
        "timeline": timeline,
        "mp4_frame_validated": validate_mp4_frames,
        "overlap_start_wallclock_ns": overlap_start_wallclock_ns,
        "overlap_end_wallclock_ns": overlap_end_wallclock_ns,
        "aligned_duration_sec": (overlap_end_wallclock_ns - overlap_start_wallclock_ns) / 1_000_000_000,
        "views": planned_views,
    }


def build_clip_plan(
    recording_dir: Path,
    annotation_path: Path,
    output_dir: Path | None = None,
    timeline: str = "timestamps",
    validate_mp4_frames: bool = True,
    input_subdir: str | None = None,
    annotation_timebase: str = "raw-primary",
    include_only_left: bool = False,
    simple_output_names: bool = False,
) -> dict[str, Any]:
    if timeline not in TIMELINE_FILES:
        raise ValueError(f"Unknown timeline {timeline!r}. Expected one of {', '.join(TIMELINE_FILES)}")
    if annotation_timebase not in ANNOTATION_TIMEBASES:
        raise ValueError(
            f"Unknown annotation timebase {annotation_timebase!r}. "
            f"Expected one of {', '.join(ANNOTATION_TIMEBASES)}"
        )
    recording_dir = recording_dir.resolve()
    annotation_path = annotation_path.resolve()
    output_dir = (output_dir or recording_dir / "clips").resolve()
    sections = load_annotations(annotation_path)
    for view in VIEWS:
        video_path = recording_video_path(recording_dir, view, input_subdir)
        if not video_path.exists():
            raise FileNotFoundError(video_path)

    raw_timestamp_rows = {
        view: load_timestamp_csv(
            recording_dir / "_timestamps" / TIMELINE_FILES[timeline].format(view=view)
        )
        for view in VIEWS
    }
    if validate_mp4_frames:
        timestamp_rows = {
            view: build_validated_timeline(
                raw_timestamp_rows[view],
                load_mp4_frame_pts_ns(recording_video_path(recording_dir, view, input_subdir)),
            )
            for view in VIEWS
        }
    else:
        timestamp_rows = raw_timestamp_rows

    planned_sections: list[dict[str, Any]] = []
    skipped_sections: list[dict[str, Any]] = []
    primary_rows = timestamp_rows["primary"]
    common_start_wallclock_ns = max(rows[0].wallclock_ns for rows in timestamp_rows.values())
    common_end_wallclock_ns = min(rows[-1].wallclock_ns for rows in timestamp_rows.values())
    if common_start_wallclock_ns >= common_end_wallclock_ns:
        raise ValueError(
            "No shared wallclock overlap across all views: "
            f"start={common_start_wallclock_ns} end={common_end_wallclock_ns}"
        )
    aligned_start_wallclock_ns: int | None = None
    aligned_end_wallclock_ns: int | None = None
    aligned_duration_sec: float | None = None
    aligned_view_offsets_sec: dict[str, float] | None = None
    if annotation_timebase == "aligned":
        aligned_start_wallclock_ns = max(rows[0].wallclock_ns for rows in timestamp_rows.values())
        aligned_end_wallclock_ns = min(rows[-1].wallclock_ns for rows in timestamp_rows.values())
        if aligned_start_wallclock_ns >= aligned_end_wallclock_ns:
            raise ValueError(
                "No shared wallclock overlap across all views: "
                f"start={aligned_start_wallclock_ns} end={aligned_end_wallclock_ns}"
            )
        aligned_duration_sec = (
            aligned_end_wallclock_ns - aligned_start_wallclock_ns
        ) / 1_000_000_000
        aligned_view_offsets_sec = {
            view: map_wallclock_to_view_time(rows, aligned_start_wallclock_ns)[1]
            for view, rows in timestamp_rows.items()
        }

    for section in sections:
        for range_index, (requested_start_sec, requested_end_sec) in enumerate(section["time"]):
            clipped_to_common_overlap = False
            if annotation_timebase == "raw-primary":
                primary_start_row, primary_start_error_ns = map_primary_time_to_wallclock(
                    primary_rows,
                    requested_start_sec,
                )
                primary_end_row, primary_end_error_ns = map_primary_time_to_wallclock(
                    primary_rows,
                    requested_end_sec,
                )
                canonical_start_wallclock_ns = primary_start_row.wallclock_ns
                canonical_end_wallclock_ns = primary_end_row.wallclock_ns
                requested_canonical_start_wallclock_ns = canonical_start_wallclock_ns
                requested_canonical_end_wallclock_ns = canonical_end_wallclock_ns
                canonical_start_wallclock_ns = max(
                    canonical_start_wallclock_ns, common_start_wallclock_ns
                )
                canonical_end_wallclock_ns = min(
                    canonical_end_wallclock_ns, common_end_wallclock_ns
                )
                clipped_to_common_overlap = (
                    canonical_start_wallclock_ns != requested_canonical_start_wallclock_ns
                    or canonical_end_wallclock_ns != requested_canonical_end_wallclock_ns
                )
                if clipped_to_common_overlap:
                    primary_start_row, _ = nearest_by_value(
                        primary_rows, "wallclock_ns", canonical_start_wallclock_ns
                    )
                    primary_end_row, _ = nearest_by_value(
                        primary_rows, "wallclock_ns", canonical_end_wallclock_ns
                    )
            else:
                assert aligned_start_wallclock_ns is not None
                assert aligned_end_wallclock_ns is not None
                effective_start_sec = min(max(requested_start_sec, 0.0), aligned_duration_sec)
                effective_end_sec = min(max(requested_end_sec, 0.0), aligned_duration_sec)
                clipped_to_common_overlap = (
                    effective_start_sec != requested_start_sec
                    or effective_end_sec != requested_end_sec
                )
                canonical_start_wallclock_ns = (
                    aligned_start_wallclock_ns + round(effective_start_sec * 1_000_000_000)
                )
                canonical_end_wallclock_ns = (
                    aligned_start_wallclock_ns + round(effective_end_sec * 1_000_000_000)
                )
                primary_start_row, primary_start_error_ns = nearest_by_value(
                    primary_rows,
                    "wallclock_ns",
                    canonical_start_wallclock_ns,
                )
                primary_end_row, primary_end_error_ns = nearest_by_value(
                    primary_rows,
                    "wallclock_ns",
                    canonical_end_wallclock_ns,
                )
            if canonical_start_wallclock_ns >= canonical_end_wallclock_ns:
                skipped_sections.append(
                    {
                        "id": section["id"],
                        "range_index": range_index,
                        "reason": "no overlap with all four video timelines",
                        "requested_primary_start_sec": requested_start_sec,
                        "requested_primary_end_sec": requested_end_sec,
                    }
                )
                continue
            clips: dict[str, Any] = {}
            invalid_view: str | None = None
            for view in VIEWS:
                view_rows = timestamp_rows[view]
                start_row, start_sec, start_wallclock_error_ns = map_wallclock_to_view_time(
                    view_rows,
                    canonical_start_wallclock_ns,
                )
                end_row, end_sec, end_wallclock_error_ns = map_wallclock_to_view_time(
                    view_rows,
                    canonical_end_wallclock_ns,
                )
                if start_sec >= end_sec:
                    invalid_view = view
                    break
                simple_name = {"primary": "primary.mp4", "secondary": "secondary.mp4", "left_hand": "left_hand.mp4", "right_hand": "right_hand.mp4"}[view]
                output_name = simple_name if simple_output_names else f"recording_{view}.mp4"
                clips[view] = {
                    "input_path": str(recording_video_path(recording_dir, view, input_subdir)),
                    "output_path": str(
                        output_dir / f"section_{section['id']}" / output_name
                    ),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "ffmpeg_start_sec": getattr(start_row, "mp4_pts_ns", round(start_sec * 1_000_000_000))
                    / 1_000_000_000,
                    "ffmpeg_end_sec": getattr(end_row, "mp4_pts_ns", round(end_sec * 1_000_000_000))
                    / 1_000_000_000,
                    "start_frame": start_row.frame,
                    "end_frame": end_row.frame,
                    "start_mp4_frame": getattr(start_row, "mp4_frame", None),
                    "end_mp4_frame": getattr(end_row, "mp4_frame", None),
                    "start_mp4_pts_ns": getattr(start_row, "mp4_pts_ns", None),
                    "end_mp4_pts_ns": getattr(end_row, "mp4_pts_ns", None),
                    "start_pts_match_error_ns": getattr(start_row, "pts_match_error_ns", None),
                    "end_pts_match_error_ns": getattr(end_row, "pts_match_error_ns", None),
                    "start_wallclock_ns": start_row.wallclock_ns,
                    "end_wallclock_ns": end_row.wallclock_ns,
                    "start_wallclock_error_ns": start_wallclock_error_ns,
                    "end_wallclock_error_ns": end_wallclock_error_ns,
                }
            if invalid_view is not None:
                skipped_sections.append(
                    {
                        "id": section["id"],
                        "range_index": range_index,
                        "reason": f"frame quantization collapsed interval for view {invalid_view}",
                        "requested_primary_start_sec": requested_start_sec,
                        "requested_primary_end_sec": requested_end_sec,
                    }
                )
                continue
            if include_only_left:
                clips["only_left"] = {
                    "input_path": str(recording_video_path(recording_dir, "primary", input_subdir)),
                    "output_path": str(
                        output_dir / f"section_{section['id']}" /
                        ("only_left.mp4" if simple_output_names else "recording_primary_left.mp4")
                    ),
                    "start_sec": clips["primary"]["start_sec"],
                    "end_sec": clips["primary"]["end_sec"],
                    "ffmpeg_start_sec": clips["primary"]["ffmpeg_start_sec"],
                    "ffmpeg_end_sec": clips["primary"]["ffmpeg_end_sec"],
                    "crop_filter": LEFT_ONLY_CROP_FILTER,
                    "source_view": "primary",
                }
            planned_sections.append(
                {
                    "id": section["id"],
                    "range_index": range_index,
                    "requested_primary_start_sec": requested_start_sec,
                    "requested_primary_end_sec": requested_end_sec,
                    "clipped_to_common_overlap": clipped_to_common_overlap,
                    "effective_start_wallclock_ns": canonical_start_wallclock_ns,
                    "effective_end_wallclock_ns": canonical_end_wallclock_ns,
                    "canonical_start_wallclock_ns": canonical_start_wallclock_ns,
                    "canonical_end_wallclock_ns": canonical_end_wallclock_ns,
                    "primary_start_frame": primary_start_row.frame,
                    "primary_end_frame": primary_end_row.frame,
                    "primary_start_pts_error_ns": primary_start_error_ns,
                    "primary_end_pts_error_ns": primary_end_error_ns,
                    "description": section.get("description", ""),
                    "clips": clips,
                }
            )

    return {
        "recording_dir": str(recording_dir),
        "input_subdir": input_subdir,
        "annotation_path": str(annotation_path),
        "output_dir": str(output_dir),
        "timeline": timeline,
        "mp4_frame_validated": validate_mp4_frames,
        "annotation_timebase": annotation_timebase,
        "aligned_start_wallclock_ns": aligned_start_wallclock_ns,
        "aligned_end_wallclock_ns": aligned_end_wallclock_ns,
        "aligned_duration_sec": aligned_duration_sec,
        "aligned_view_offsets_sec": aligned_view_offsets_sec,
        "sections": planned_sections,
        "skipped_sections": skipped_sections,
        "common_start_wallclock_ns": common_start_wallclock_ns,
        "common_end_wallclock_ns": common_end_wallclock_ns,
    }


def build_aligned_input_clip_plan(
    recording_dir: Path,
    annotation_path: Path,
    output_dir: Path | None = None,
    include_only_left: bool = False,
    simple_output_names: bool = False,
) -> dict[str, Any]:
    recording_dir = recording_dir.resolve()
    annotation_path = annotation_path.resolve()
    output_dir = (output_dir or recording_dir / "clips").resolve()
    sections = load_annotations(annotation_path)
    for view in VIEWS:
        video_path = recording_dir / f"recording_{view}.mp4"
        if not video_path.exists():
            raise FileNotFoundError(video_path)

    planned_sections: list[dict[str, Any]] = []
    for section in sections:
        for range_index, (start_sec, end_sec) in enumerate(section["time"]):
            clips: dict[str, Any] = {}
            for view in VIEWS:
                simple_name = {"primary": "primary.mp4", "secondary": "secondary.mp4", "left_hand": "left_hand.mp4", "right_hand": "right_hand.mp4"}[view]
                output_name = simple_name if simple_output_names else f"recording_{view}.mp4"
                clips[view] = {
                    "input_path": str(recording_dir / f"recording_{view}.mp4"),
                    "output_path": str(
                        output_dir / f"section_{section['id']}" / output_name
                    ),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "ffmpeg_start_sec": start_sec,
                    "ffmpeg_end_sec": end_sec,
                }
            if include_only_left:
                clips["only_left"] = {
                    "input_path": str(recording_dir / "recording_primary.mp4"),
                    "output_path": str(
                        output_dir / f"section_{section['id']}" /
                        ("only_left.mp4" if simple_output_names else "recording_primary_left.mp4")
                    ),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "ffmpeg_start_sec": start_sec,
                    "ffmpeg_end_sec": end_sec,
                    "crop_filter": LEFT_ONLY_CROP_FILTER,
                    "source_view": "primary",
                }
            planned_sections.append(
                {
                    "id": section["id"],
                    "range_index": range_index,
                    "requested_start_sec": start_sec,
                    "requested_end_sec": end_sec,
                    "description": section.get("description", ""),
                    "clips": clips,
                }
            )

    return {
        "recording_dir": str(recording_dir),
        "annotation_path": str(annotation_path),
        "output_dir": str(output_dir),
        "aligned_input": True,
        "sections": planned_sections,
    }


def write_manifest(plan: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "clip_manifest.json"
    manifest_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    for section in plan["sections"]:
        description = section.get("description", "")
        if description:
            section_dir = Path(next(iter(section["clips"].values()))["output_path"]).parent
            section_dir.mkdir(parents=True, exist_ok=True)
            (section_dir / "describe.txt").write_text(description.rstrip() + "\n", encoding="utf-8")
    return manifest_path


def is_complete_clip_output(plan: dict[str, Any], make_grid: bool = False) -> bool:
    if not plan["sections"]:
        return False
    output_dir = Path(plan["output_dir"])
    required_paths = [output_dir / "clip_manifest.json"]
    for section in plan["sections"]:
        for clip in section["clips"].values():
            required_paths.append(Path(clip["output_path"]))
        if section.get("description"):
            section_dir = Path(next(iter(section["clips"].values()))["output_path"]).parent
            required_paths.append(section_dir / "describe.txt")
        if make_grid:
            section_dir = Path(next(iter(section["clips"].values()))["output_path"]).parent
            required_paths.append(section_dir / "combined_grid.mp4")
    return all(path.is_file() and path.stat().st_size > 0 for path in required_paths)


def _progress_text(index: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[--------------------] 0/0   0%"
    filled = round(width * index / total)
    percent = round(100 * index / total)
    return f"[{'#' * filled}{'-' * (width - filled)}] {index}/{total} {percent:3d}%"


def format_elapsed(seconds: float) -> str:
    """Format a monotonic elapsed duration for CLI progress messages."""
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds_value = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m {seconds_value:02d}s"
    return f"{minutes:02d}m {seconds_value:02d}s"


def _recording_label(recording_dir: Path) -> str:
    return recording_dir.parent.name if recording_dir.name == "aligned" else recording_dir.name


def _batch_relative_output_path(root: Path, recording_dir: Path) -> Path:
    base_dir = recording_dir.parent if recording_dir.name == "aligned" else recording_dir
    try:
        return base_dir.resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return Path(base_dir.name)


def resolve_output_dir(
    root: Path,
    recording_dir: Path,
    output_arg: str | None,
    batch_mode: bool,
    align_full: bool,
    aligned_input: bool,
) -> Path:
    if output_arg:
        output_root = Path(output_arg)
        if batch_mode:
            return output_root / _batch_relative_output_path(root, recording_dir)
        return output_root
    if batch_mode and aligned_input and not align_full:
        return root.expanduser().resolve() / "clips_output" / _batch_relative_output_path(root, recording_dir)
    return recording_dir / ("aligned" if align_full else "clips")


ALIGNMENT_REPORT_FIELDS = [
    "section_id",
    "range_index",
    "boundary",
    "view",
    "requested_primary_sec",
    "cut_sec",
    "csv_frame",
    "mp4_frame",
    "mp4_pts_ns",
    "wallclock_ns",
    "target_wallclock_ns",
    "wallclock_error_ns",
    "pts_match_error_ns",
]


def build_alignment_report_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in plan["sections"]:
        for boundary in ("start", "end"):
            requested_primary_sec = section[f"requested_primary_{boundary}_sec"]
            target_wallclock_ns = section[f"canonical_{boundary}_wallclock_ns"]
            for view, clip in section["clips"].items():
                if clip.get("source_view") == "primary":
                    continue
                rows.append(
                    {
                        "section_id": section["id"],
                        "range_index": section["range_index"],
                        "boundary": boundary,
                        "view": view,
                        "requested_primary_sec": requested_primary_sec,
                        "cut_sec": clip[f"{boundary}_sec"],
                        "csv_frame": clip[f"{boundary}_frame"],
                        "mp4_frame": clip.get(f"{boundary}_mp4_frame"),
                        "mp4_pts_ns": clip.get(f"{boundary}_mp4_pts_ns"),
                        "wallclock_ns": clip[f"{boundary}_wallclock_ns"],
                        "target_wallclock_ns": target_wallclock_ns,
                        "wallclock_error_ns": clip[f"{boundary}_wallclock_error_ns"],
                        "pts_match_error_ns": clip.get(f"{boundary}_pts_match_error_ns"),
                    }
                )
    return rows


def write_alignment_reports(plan: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = build_alignment_report_rows(plan)

    csv_path = output_dir / "alignment_report.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALIGNMENT_REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    md_path = output_dir / "alignment_report.md"
    lines = [
        "# Alignment Report",
        "",
        f"- timeline: `{plan['timeline']}`",
        f"- mp4_frame_validated: `{plan['mp4_frame_validated']}`",
        "",
        "| section | boundary | view | requested_primary_sec | cut_sec | csv_frame | mp4_frame | mp4_pts_ns | wallclock_ns | target_wallclock_ns | wallclock_error_ms | pts_match_error_ms |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        wallclock_error_ms = row["wallclock_error_ns"] / 1_000_000
        pts_match_error_ns = row["pts_match_error_ns"]
        pts_match_error_ms = "" if pts_match_error_ns is None else f"{pts_match_error_ns / 1_000_000:.3f}"
        lines.append(
            "| "
            f"{row['section_id']} | "
            f"{row['boundary']} | "
            f"{row['view']} | "
            f"{row['requested_primary_sec']:.6f} | "
            f"{row['cut_sec']:.6f} | "
            f"{row['csv_frame']} | "
            f"{'' if row['mp4_frame'] is None else row['mp4_frame']} | "
            f"{'' if row['mp4_pts_ns'] is None else row['mp4_pts_ns']} | "
            f"{row['wallclock_ns']} | "
            f"{row['target_wallclock_ns']} | "
            f"{wallclock_error_ms:.3f} | "
            f"{pts_match_error_ms} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


ALIGNMENT_FULL_REPORT_FIELDS = [
    "boundary",
    "view",
    "cut_sec",
    "ffmpeg_cut_sec",
    "csv_frame",
    "mp4_frame",
    "mp4_pts_ns",
    "wallclock_ns",
    "target_wallclock_ns",
    "wallclock_error_ns",
    "pts_match_error_ns",
]


def build_alignment_full_report_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for boundary in ("start", "end"):
        target_wallclock_ns = plan[f"overlap_{boundary}_wallclock_ns"]
        for view, clip in plan["views"].items():
            rows.append(
                {
                    "boundary": boundary,
                    "view": view,
                    "cut_sec": clip[f"{boundary}_sec"],
                    "ffmpeg_cut_sec": clip[f"ffmpeg_{boundary}_sec"],
                    "csv_frame": clip[f"{boundary}_frame"],
                    "mp4_frame": clip.get(f"{boundary}_mp4_frame"),
                    "mp4_pts_ns": clip.get(f"{boundary}_mp4_pts_ns"),
                    "wallclock_ns": clip[f"{boundary}_wallclock_ns"],
                    "target_wallclock_ns": target_wallclock_ns,
                    "wallclock_error_ns": clip[f"{boundary}_wallclock_error_ns"],
                    "pts_match_error_ns": clip.get(f"{boundary}_pts_match_error_ns"),
                }
            )
    return rows


def write_alignment_full_outputs(plan: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "alignment_manifest.json"
    manifest_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    rows = build_alignment_full_report_rows(plan)
    report_path = output_dir / "alignment_report.md"
    lines = [
        "# Full Alignment Report",
        "",
        f"- timeline: `{plan['timeline']}`",
        f"- mp4_frame_validated: `{plan['mp4_frame_validated']}`",
        f"- overlap_start_wallclock_ns: `{plan['overlap_start_wallclock_ns']}`",
        f"- overlap_end_wallclock_ns: `{plan['overlap_end_wallclock_ns']}`",
        f"- aligned_duration_sec: `{plan['aligned_duration_sec']:.9f}`",
        "",
        "| boundary | view | cut_sec | ffmpeg_cut_sec | csv_frame | mp4_frame | mp4_pts_ns | wallclock_ns | target_wallclock_ns | wallclock_error_ms | pts_match_error_ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        wallclock_error_ms = row["wallclock_error_ns"] / 1_000_000
        pts_match_error_ns = row["pts_match_error_ns"]
        pts_match_error_ms = "" if pts_match_error_ns is None else f"{pts_match_error_ns / 1_000_000:.3f}"
        lines.append(
            "| "
            f"{row['boundary']} | "
            f"{row['view']} | "
            f"{row['cut_sec']:.6f} | "
            f"{row['ffmpeg_cut_sec']:.6f} | "
            f"{row['csv_frame']} | "
            f"{'' if row['mp4_frame'] is None else row['mp4_frame']} | "
            f"{'' if row['mp4_pts_ns'] is None else row['mp4_pts_ns']} | "
            f"{row['wallclock_ns']} | "
            f"{row['target_wallclock_ns']} | "
            f"{wallclock_error_ms:.3f} | "
            f"{pts_match_error_ms} |"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path, report_path


def export_clips(plan: dict[str, Any], reencode: bool, skip_existing: bool = True) -> None:
    for section in plan["sections"]:
        for clip in section["clips"].values():
            output_path = Path(clip["output_path"])
            if skip_existing and output_path.is_file() and output_path.stat().st_size > 0:
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if clip.get("source_view") == "primary":
                command = build_left_only_ffmpeg_command(
                    Path(clip["input_path"]),
                    output_path,
                    clip.get("ffmpeg_start_sec", clip["start_sec"]),
                    clip.get("ffmpeg_end_sec", clip["end_sec"]),
                    crop_filter=clip.get("crop_filter", LEFT_ONLY_CROP_FILTER),
                )
            else:
                command = build_ffmpeg_command(
                    Path(clip["input_path"]),
                    output_path,
                    clip.get("ffmpeg_start_sec", clip["start_sec"]),
                    clip.get("ffmpeg_end_sec", clip["end_sec"]),
                    reencode=reencode,
                )
            subprocess.run(command, check=True)


def export_aligned_videos(plan: dict[str, Any], reencode: bool) -> None:
    for clip in plan["views"].values():
        output_path = Path(clip["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = build_ffmpeg_command(
            Path(clip["input_path"]),
            output_path,
            clip["ffmpeg_start_sec"],
            clip["ffmpeg_end_sec"],
            reencode=reencode,
        )
        subprocess.run(command, check=True)


def export_grid_clips(plan: dict[str, Any], cell_width: int, cell_height: int) -> None:
    for section in plan["sections"]:
        section_dir = Path(next(iter(section["clips"].values()))["output_path"]).parent
        input_paths = {
            view: Path(section["clips"][view]["output_path"])
            for view in GRID_VIEWS
        }
        output_path = section_dir / "combined_grid.mp4"
        command = build_grid_ffmpeg_command(input_paths, output_path, cell_width, cell_height)
        subprocess.run(command, check=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export aligned clips from four camera recordings using source timestamp CSV files.",
    )
    parser.add_argument(
        "recording_dir",
        nargs="?",
        default="/Users/tong/Downloads/0002_1782611070",
        help="Directory containing recordings, annotation.json, and _timestamps/",
    )
    parser.add_argument(
        "--annotation",
        default=None,
        help="Annotation JSON path. Defaults to <recording_dir>/annotation.json.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory. Defaults to <recording_dir>/clips.",
    )
    parser.add_argument(
        "--input-subdir",
        default=None,
        help="Read recording_*.mp4 from this subdirectory inside each recording folder, e.g. upload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write clip_manifest.json without running ffmpeg.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild clip outputs even when resume files look complete.",
    )
    parser.add_argument(
        "--no-timer",
        action="store_true",
        help="Disable elapsed-time messages while processing.",
    )
    parser.add_argument(
        "--align-full",
        action="store_true",
        help="Export full-length aligned videos trimmed to the shared wallclock overlap.",
    )
    parser.add_argument(
        "--reencode",
        action="store_true",
        help="Re-encode clips for more precise cuts. Default uses stream copy.",
    )
    parser.add_argument(
        "--timeline",
        choices=tuple(TIMELINE_FILES),
        default="timestamps",
        help="Timestamp CSV family to use. Defaults to timestamps.",
    )
    parser.add_argument(
        "--no-validate-mp4-frames",
        action="store_true",
        help="Use timestamp CSV rows directly without matching them to decoded MP4 frames.",
    )
    parser.add_argument(
        "--annotation-timebase",
        choices=ANNOTATION_TIMEBASES,
        default="raw-primary",
        help=(
            "Time base used by annotation.json. 'raw-primary' treats times as "
            "original primary-video seconds; 'aligned' treats them as seconds "
            "from the shared aligned overlap. Both modes cut the original videos."
        ),
    )
    parser.add_argument(
        "--include-only-left",
        action="store_true",
        help="Also export a left-eye crop from each primary clip.",
    )
    parser.add_argument(
        "--simple-output-names",
        action="store_true",
        help="Use primary.mp4/secondary.mp4/left_hand.mp4/right_hand.mp4/only_left.mp4 names.",
    )
    parser.add_argument(
        "--make-grid",
        action="store_true",
        help="After exporting the four clips for each section, also create a 2x2 combined_grid.mp4.",
    )
    parser.add_argument(
        "--grid-cell-width",
        type=int,
        default=960,
        help="Width of each cell in the combined grid video.",
    )
    parser.add_argument(
        "--grid-cell-height",
        type=int,
        default=540,
        help="Height of each cell in the combined grid video.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.recording_dir)
    recording_dirs = find_recording_dirs(root, input_subdir=args.input_subdir)
    if not recording_dirs:
        raise FileNotFoundError(f"No recording folders found under {root}")

    batch_mode = len(recording_dirs) > 1 or recording_dirs[0] != root.expanduser().resolve()
    total = len(recording_dirs)
    batch_started_at = time.monotonic()
    if batch_mode:
        print(f"[clips] found {total} recording dirs under {root}", flush=True)

    for index, recording_dir in enumerate(recording_dirs, start=1):
        label = _recording_label(recording_dir)
        recording_started_at = time.monotonic()
        if batch_mode:
            print(f"[clips] {_progress_text(index, total)} {label}: start", flush=True)
        elif not args.no_timer:
            print(f"[clips] {label}: start (elapsed 00m 00s)", flush=True)
        input_subdir = None if args.input_subdir and recording_dir.name == args.input_subdir else args.input_subdir
        aligned_input = input_subdir is None and has_aligned_inputs(recording_dir) and not (recording_dir / "_timestamps").is_dir()
        output_dir = resolve_output_dir(
            root,
            recording_dir,
            args.output_dir,
            batch_mode,
            args.align_full,
            aligned_input,
        )
        if args.align_full:
            plan = build_alignment_plan(
                recording_dir,
                output_dir=output_dir,
                timeline=args.timeline,
                validate_mp4_frames=not args.no_validate_mp4_frames,
                input_subdir=input_subdir,
            )
            write_alignment_full_outputs(plan, output_dir)
            if not args.dry_run:
                export_aligned_videos(plan, reencode=args.reencode)
            if not args.no_timer:
                print(
                    f"[clips] {label}: done in {format_elapsed(time.monotonic() - recording_started_at)} "
                    f"(total {format_elapsed(time.monotonic() - batch_started_at)})",
                    flush=True,
                )
            continue

        annotation_path = Path(args.annotation) if args.annotation else default_annotation_path(recording_dir)
        segments_annotation = annotation_path.exists() and is_segments_annotation(annotation_path)
        include_only_left = args.include_only_left or segments_annotation
        simple_output_names = args.simple_output_names or segments_annotation
        if segments_annotation and args.output_dir is None:
            output_dir = (recording_dir.parent if recording_dir.name == "aligned" else recording_dir) / "clip"
        if aligned_input:
            if batch_mode and not annotation_path.exists():
                message = f"[clips] {label}: skipped missing annotation {annotation_path}"
                if not args.no_timer:
                    message += f" (elapsed {format_elapsed(time.monotonic() - recording_started_at)})"
                print(message, flush=True)
                continue
            plan = build_aligned_input_clip_plan(
                recording_dir,
                annotation_path,
                output_dir=output_dir,
                include_only_left=include_only_left,
                simple_output_names=simple_output_names,
            )
            if not args.force and not args.dry_run and is_complete_clip_output(plan, make_grid=args.make_grid):
                message = f"[clips] {label}: skipped complete clips {output_dir}"
                if not args.no_timer:
                    message += f" (elapsed {format_elapsed(time.monotonic() - recording_started_at)})"
                print(message, flush=True)
                continue
            write_manifest(plan, output_dir)
            if not args.dry_run:
                export_clips(plan, reencode=args.reencode, skip_existing=not args.force)
                if args.make_grid:
                    export_grid_clips(plan, args.grid_cell_width, args.grid_cell_height)
            if not args.no_timer:
                print(
                    f"[clips] {label}: done in {format_elapsed(time.monotonic() - recording_started_at)} "
                    f"(total {format_elapsed(time.monotonic() - batch_started_at)})",
                    flush=True,
                )
            continue

        if batch_mode and not annotation_path.exists():
            message = f"[clips] {label}: skipped missing annotation {annotation_path}"
            if not args.no_timer:
                message += f" (elapsed {format_elapsed(time.monotonic() - recording_started_at)})"
            print(message, flush=True)
            continue
        plan = build_clip_plan(
            recording_dir,
            annotation_path,
            output_dir=output_dir,
            timeline=args.timeline,
            validate_mp4_frames=not args.no_validate_mp4_frames,
            input_subdir=input_subdir,
            annotation_timebase=args.annotation_timebase,
            include_only_left=include_only_left,
            simple_output_names=simple_output_names,
        )
        if not args.force and not args.dry_run and is_complete_clip_output(plan, make_grid=args.make_grid):
            message = f"[clips] {label}: skipped complete clips {output_dir}"
            if not args.no_timer:
                message += f" (elapsed {format_elapsed(time.monotonic() - recording_started_at)})"
            print(message, flush=True)
            continue
        write_manifest(plan, output_dir)
        write_alignment_reports(plan, output_dir)
        if not args.dry_run:
            export_clips(plan, reencode=args.reencode, skip_existing=not args.force)
            if args.make_grid:
                export_grid_clips(plan, args.grid_cell_width, args.grid_cell_height)
        if not args.no_timer:
            print(
                f"[clips] {label}: done in {format_elapsed(time.monotonic() - recording_started_at)} "
                f"(total {format_elapsed(time.monotonic() - batch_started_at)})",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
