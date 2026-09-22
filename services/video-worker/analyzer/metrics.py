"""Cheap visual metrics; deliberately no model or network dependency."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _empty_metrics(warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "sharpness": None,
        "brightness": None,
        "contrast": None,
        "motion_score": None,
        "warnings": list(warnings or []),
    }


def measure_frames(paths: list[Path], inherited_warnings: list[str] | None = None) -> dict[str, Any]:
    warnings = list(inherited_warnings or [])
    try:
        import cv2
        import numpy as np
    except ImportError:
        warnings.append("opencv_or_numpy_unavailable")
        return _empty_metrics(warnings)

    grays = []
    for path in paths:
        try:
            encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        except OSError:
            image = None
        if image is None:
            warnings.append(f"unreadable_frame:{path.name}")
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        grays.append(gray)
    if not grays:
        warnings.append("no_frames_for_metrics")
        return _empty_metrics(warnings)

    brightness = float(np.mean([frame.mean() / 255.0 for frame in grays]))
    contrast = float(np.mean([frame.std() / 64.0 for frame in grays]))
    raw_sharpness = float(np.mean([cv2.Laplacian(frame, cv2.CV_64F).var() for frame in grays]))
    sharpness = min(raw_sharpness / 500.0, 1.0)

    motion_values: list[float] = []
    for previous, current in zip(grays, grays[1:]):
        previous_small = cv2.resize(previous, (320, 180), interpolation=cv2.INTER_AREA)
        current_small = cv2.resize(current, (320, 180), interpolation=cv2.INTER_AREA)
        motion_values.append(float(cv2.absdiff(previous_small, current_small).mean() / 255.0))
    motion = float(np.mean(motion_values)) if motion_values else 0.0

    if brightness < 0.12:
        warnings.append("underexposed")
    elif brightness > 0.90:
        warnings.append("overexposed")
    if contrast < 0.20:
        warnings.append("low_contrast")
    if sharpness < 0.08:
        warnings.append("low_sharpness")

    return {
        "sharpness": round(sharpness, 4),
        "brightness": round(brightness, 4),
        "contrast": round(min(contrast, 1.0), 4),
        "motion_score": round(motion, 4),
        "warnings": sorted(set(warnings)),
    }
