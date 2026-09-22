"""FFprobe wrapper with strict, testable JSON parsing."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from .models import VideoMetadata


class ProbeError(RuntimeError):
    """Raised when a file cannot be inspected as a video."""


def parse_frame_rate(value: str | int | float | None) -> float:
    if value in (None, "", "0/0", "N/A"):
        return 0.0
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def parse_probe_payload(payload: dict[str, Any]) -> VideoMetadata:
    streams = payload.get("streams") or []
    format_info = payload.get("format") or {}
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError("ffprobe result contains no video stream")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)

    try:
        duration = float(format_info.get("duration") or video.get("duration") or 0.0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise ProbeError(f"invalid ffprobe metadata: {exc}") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise ProbeError("video has invalid duration or dimensions")

    fps = parse_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    return VideoMetadata(
        duration=round(duration, 6),
        width=width,
        height=height,
        fps=round(fps, 6),
        has_audio=audio is not None,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
        format_name=format_info.get("format_name"),
        bit_rate=_optional_int(format_info.get("bit_rate")),
    )


def probe_video(
    path: Path,
    ffprobe_bin: str = "ffprobe",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> VideoMetadata:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,bit_rate:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ProbeError(f"cannot run {ffprobe_bin}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "unknown ffprobe error").strip()[-500:]
        raise ProbeError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc
    return parse_probe_payload(payload)
