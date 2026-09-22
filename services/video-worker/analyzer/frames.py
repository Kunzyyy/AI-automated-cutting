"""Deterministic keyframe and low-resolution proxy-clip cache."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from .models import CachedFrames, SceneRange


def _is_nonempty_file(path: Path) -> bool:
    """Return whether an existing cache artifact is safe to reuse."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def sample_times(scene: SceneRange) -> list[float]:
    """Pick stable beginning/middle/end samples without touching cut boundaries."""
    if scene.duration <= 0:
        return []
    edge = min(0.15, scene.duration * 0.1)
    candidates = [scene.start + edge, (scene.start + scene.end) / 2, scene.end - edge]
    unique: list[float] = []
    for value in candidates:
        value = round(min(max(value, scene.start), scene.end), 6)
        if not unique or abs(value - unique[-1]) >= 0.02:
            unique.append(value)
    return unique


def extract_frame(
    video_path: Path,
    timestamp: float,
    output_path: Path,
    width: int,
    ffmpeg_bin: str = "ffmpeg",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str | None]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{timestamp:.6f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({width},iw)':-2",
        "-q:v",
        "3",
        str(output_path),
    ]
    try:
        result = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"cannot_run_ffmpeg:{exc}"
    if result.returncode != 0 or not output_path.exists():
        detail = (result.stderr or "frame_not_created").strip()[-300:]
        return False, detail
    return True, None


def extract_proxy_clip(
    video_path: Path,
    scene: SceneRange,
    output_path: Path,
    width: int,
    ffmpeg_bin: str = "ffmpeg",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str | None]:
    """Create a muted H.264 proxy covering the complete shot time range."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{scene.start:.6f}",
        "-i",
        str(video_path),
        "-t",
        f"{scene.duration:.6f}",
        "-an",
        "-vf",
        f"scale='min({width},iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        result = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"cannot_run_ffmpeg:{exc}"
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        detail = (result.stderr or "proxy_clip_not_created").strip()[-300:]
        return False, detail
    return True, None


def cache_shot_frames(
    video_path: Path,
    scene: SceneRange,
    shot_id: str,
    cache_root: Path,
    keyframe_width: int = 960,
    proxy_width: int = 480,
    ffmpeg_bin: str = "ffmpeg",
) -> CachedFrames:
    shot_dir = cache_root / shot_id
    keyframes: list[Path] = []
    warnings: list[str] = []
    times = sample_times(scene)
    for index, timestamp in enumerate(times, start=1):
        target = shot_dir / f"keyframe_{index:02d}_{timestamp:.3f}s.jpg"
        if _is_nonempty_file(target):
            keyframes.append(target)
            continue
        ok, detail = extract_frame(video_path, timestamp, target, keyframe_width, ffmpeg_bin)
        if ok:
            keyframes.append(target)
        else:
            warnings.append(f"keyframe_extraction_failed:{index}:{detail}")

    proxy: Path | None = None
    if scene.duration > 0:
        target = shot_dir / "proxy.mp4"
        if _is_nonempty_file(target):
            proxy = target
        else:
            ok, detail = extract_proxy_clip(video_path, scene, target, proxy_width, ffmpeg_bin)
            if ok:
                proxy = target
            else:
                warnings.append(f"proxy_extraction_failed:{detail}")
    return CachedFrames(keyframes=keyframes, proxy=proxy, warnings=warnings)
