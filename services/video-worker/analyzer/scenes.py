"""Scene detection with a whole-video fallback."""

from __future__ import annotations

from pathlib import Path

from .models import SceneRange


def fallback_scene(duration: float) -> list[SceneRange]:
    return [SceneRange(start=0.0, end=max(0.0, duration), detector="whole_video_fallback")]


def detect_scenes(
    path: Path,
    duration: float,
    threshold: float = 30.0,
    min_scene_length: float = 0.5,
) -> tuple[list[SceneRange], str | None]:
    """Return scenes and an optional warning when fallback was necessary."""
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError:
        return fallback_scene(duration), "pyscenedetect_unavailable"

    try:
        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=threshold))
        manager.detect_scenes(video)
        raw_scenes = manager.get_scene_list()
        scenes = [
            SceneRange(
                start=max(0.0, round(start.seconds, 6)),
                end=min(duration, round(end.seconds, 6)),
                detector="pyscenedetect_content",
            )
            for start, end in raw_scenes
            if end.seconds - start.seconds >= min_scene_length
        ]
        if scenes:
            return scenes, None
        return fallback_scene(duration), "pyscenedetect_returned_no_scenes"
    except Exception as exc:  # Video decoder failures vary by backend.
        return fallback_scene(duration), f"pyscenedetect_failed:{type(exc).__name__}"
