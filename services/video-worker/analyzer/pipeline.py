"""Footage Index 1.0 pipeline with strict multimodal Shot Cards."""

from __future__ import annotations

import json
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .frames import cache_shot_frames, sample_times
from .metrics import measure_frames
from .probe import ProbeError, probe_video
from .scenes import detect_scenes
from .semantic import (
    SemanticProvider,
    SemanticProviderError,
    create_semantic_provider,
    validate_source_review_payload,
    validate_semantic_payload,
)
from .speech import (
    SpeechProvider,
    SpeechProviderError,
    create_speech_provider,
    speech_text_for_range,
    validate_transcript,
)

LOGGER = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
ANALYZER_VERSION = "1.0.0"


class FootageIndexValidationError(RuntimeError):
    """Raised when generated output does not satisfy the frozen contract."""


@dataclass(frozen=True)
class AnalyzerConfig:
    scene_threshold: float = 20.0
    min_scene_length: float = 0.5
    keyframe_width: int = 960
    proxy_width: int = 480
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    semantic_provider: str = "auto"
    semantic_model: str | None = None
    reuse_semantic_cache: bool = True
    speech_provider: str = "none"
    speech_model: str = "medium"
    speech_device: str = "cpu"
    speech_compute_type: str = "int8"
    source_joint_review: bool = True


def _manifest_uri(path: Path, manifest_dir: Path) -> str:
    """Use portable relative references for artifacts beside the manifest."""
    try:
        return path.resolve().relative_to(manifest_dir.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_uri()


def _unit(value: float | int | None, default: float = 0.0) -> float:
    try:
        return round(min(max(float(value), 0.0), 1.0), 4)
    except (TypeError, ValueError):
        return round(default, 4)


def _quality_from_metrics(technical: dict[str, Any], has_keyframes: bool) -> dict[str, Any]:
    sharpness = _unit(technical.get("sharpness"))
    brightness = technical.get("brightness")
    exposure = _unit(1.0 - abs(float(brightness) - 0.5) * 2.0) if brightness is not None else 0.0
    stability = _unit(1.0 - _unit(technical.get("motion_score")))
    overall = round((sharpness + exposure + stability) / 3.0, 4)
    rejection_reasons = [
        warning
        for warning in technical.get("warnings", [])
        if warning in {"no_frames_for_metrics", "underexposed", "overexposed", "low_sharpness"}
    ]
    usable = has_keyframes and not any(reason in rejection_reasons for reason in {"no_frames_for_metrics", "underexposed", "overexposed"})
    result: dict[str, Any] = {
        "overall": overall,
        "sharpness": sharpness,
        "exposure": exposure,
        "stability": stability,
        "usable": usable,
    }
    if rejection_reasons:
        result["rejection_reasons"] = sorted(set(rejection_reasons))
    return result


def _cache_key(provider: SemanticProvider, context: dict[str, Any], keyframes: list[Path]) -> dict[str, Any]:
    return {
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": provider.prompt_version,
        "source_uri": context["source_uri"],
        "start_seconds": context["start_seconds"],
        "end_seconds": context["end_seconds"],
        "keyframes": [path.name for path in keyframes],
    }


def _load_cached_semantics(
    path: Path,
    provider: SemanticProvider,
    context: dict[str, Any],
    keyframes: list[Path],
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("cache_key") != _cache_key(provider, context, keyframes):
            return None
        return validate_semantic_payload(cached.get("payload"))
    except (OSError, json.JSONDecodeError, SemanticProviderError):
        LOGGER.warning("Ignoring invalid semantic cache: %s", path)
        return None


def _save_semantic_cache(
    path: Path,
    provider: SemanticProvider,
    context: dict[str, Any],
    keyframes: list[Path],
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"cache_key": _cache_key(provider, context, keyframes), "payload": payload}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _speech_cache_key(provider: SpeechProvider, source: Path) -> dict[str, Any]:
    stat = source.stat()
    return {
        "provider": provider.name,
        "model": provider.model,
        "version": provider.version,
        "source_uri": source.as_uri(),
        "source_size": stat.st_size,
        "source_modified_ns": stat.st_mtime_ns,
    }


def _load_speech_cache(path: Path, provider: SpeechProvider, source: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("cache_key") != _speech_cache_key(provider, source):
            return None
        return validate_transcript(cached.get("transcript"))
    except (OSError, json.JSONDecodeError, SpeechProviderError):
        LOGGER.warning("Ignoring invalid speech cache: %s", path)
        return None


def _save_speech_cache(path: Path, provider: SpeechProvider, source: Path, transcript: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"cache_key": _speech_cache_key(provider, source), "transcript": transcript}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _source_review_cache_key(
    provider: SemanticProvider,
    source_context: dict[str, Any],
    shot_contexts: list[dict[str, Any]],
    shot_frames: dict[str, list[Path]],
) -> dict[str, Any]:
    serialized = json.dumps(shot_contexts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": getattr(provider, "source_review_prompt_version", "source-shot-review-v1"),
        "source_uri": source_context["uri"],
        "shot_context_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "shot_frames": {
            shot_id: [path.name for path in paths]
            for shot_id, paths in sorted(shot_frames.items())
        },
    }


def _source_review_cache_path(cache_root: Path, source_id: str, provider: SemanticProvider) -> Path:
    """Keep prompt revisions side by side so a new review never overwrites an audited result."""
    version = getattr(provider, "source_review_prompt_version", "source-shot-review-v1")
    if version == "source-shot-review-v1":
        return cache_root / source_id / "semantic_review.json"
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("_")[:80] or "unknown"
    return cache_root / source_id / f"semantic_review_{safe_version}.json"


def _load_source_review_cache(
    path: Path,
    provider: SemanticProvider,
    source_context: dict[str, Any],
    shot_contexts: list[dict[str, Any]],
    shot_frames: dict[str, list[Path]],
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("cache_key") != _source_review_cache_key(
            provider, source_context, shot_contexts, shot_frames
        ):
            return None
        return validate_source_review_payload(cached.get("payload"), [shot["shot_id"] for shot in shot_contexts])
    except (OSError, json.JSONDecodeError, SemanticProviderError):
        LOGGER.warning("Ignoring invalid source review cache: %s", path)
        return None


def _save_source_review_cache(
    path: Path,
    provider: SemanticProvider,
    source_context: dict[str, Any],
    shot_contexts: list[dict[str, Any]],
    shot_frames: dict[str, list[Path]],
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "cache_key": _source_review_cache_key(provider, source_context, shot_contexts, shot_frames),
        "payload": payload,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _apply_source_review(shots: list[dict[str, Any]], review: dict[str, Any]) -> None:
    revisions = {item["shot_id"]: item for item in review["shots"]}
    for shot in shots:
        revision = revisions[shot["shot_id"]]
        for field in ("description", "semantic_tags", "people", "product", "action", "model_confidence", "continuity"):
            shot[field] = revision[field]


def _validate_footage_index(document: dict[str, Any], schema_path: Path) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise FootageIndexValidationError("jsonschema is required to validate footage_index.json") from exc
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FootageIndexValidationError(f"cannot read footage index schema: {schema_path}") from exc
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}" for error in errors[:10]
        )
        raise FootageIndexValidationError(f"generated Footage Index violates schema 1.0.0: {details}")
    source_ids = {source["asset_id"] for source in document["sources"]}
    if len(source_ids) != len(document["sources"]):
        raise FootageIndexValidationError("source asset_id values must be unique")
    shot_ids: set[str] = set()
    source_durations = {source["asset_id"]: source["duration_seconds"] for source in document["sources"]}
    for shot in document["shots"]:
        if shot["shot_id"] in shot_ids:
            raise FootageIndexValidationError(f"duplicate shot_id: {shot['shot_id']}")
        shot_ids.add(shot["shot_id"])
        source_id = shot["source_asset_id"]
        if source_id not in source_ids:
            raise FootageIndexValidationError(f"shot references missing source: {source_id}")
        if not shot["end_seconds"] > shot["start_seconds"]:
            raise FootageIndexValidationError(f"invalid time range for {shot['shot_id']}")
        if shot["end_seconds"] > source_durations[source_id] + 0.001:
            raise FootageIndexValidationError(f"shot exceeds source duration: {shot['shot_id']}")
        if abs(shot["duration_seconds"] - (shot["end_seconds"] - shot["start_seconds"])) > 0.001:
            raise FootageIndexValidationError(f"duration mismatch for {shot['shot_id']}")
    for shot in document["shots"]:
        for relation in shot.get("continuity", []):
            if relation["other_shot_id"] not in shot_ids:
                raise FootageIndexValidationError(
                    f"continuity reference missing for {shot['shot_id']}: {relation['other_shot_id']}"
                )


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "schemas" / "footage_index.schema.json"


def analyze_directory(
    input_dir: str | Path,
    output_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    config: AnalyzerConfig | None = None,
    *,
    job_id: str = "local-analysis",
    semantic_provider: SemanticProvider | None = None,
    speech_provider: SpeechProvider | None = None,
) -> dict[str, Any]:
    """Analyze every source and atomically write a contract-valid Footage Index."""
    config = config or AnalyzerConfig()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", job_id):
        raise ValueError("job_id does not satisfy the Footage Index id contract")
    source_root = Path(input_dir).expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"input directory does not exist: {source_root}")

    output = Path(output_path).expanduser().resolve() if output_path else source_root / "analysis" / "footage_index.json"
    cache_root = Path(cache_dir).expanduser().resolve() if cache_dir else output.parent / "cache"
    provider = semantic_provider or create_semantic_provider(config.semantic_provider, config.semantic_model)
    active_speech_provider = speech_provider or create_speech_provider(
        config.speech_provider,
        config.speech_model,
        device=config.speech_device,
        compute_type=config.speech_compute_type,
    )
    video_paths = sorted(
        (item for item in source_root.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda item: item.name.casefold(),
    )
    if not video_paths:
        raise FileNotFoundError(f"no supported video files found in {source_root}")

    output.parent.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for source_number, video_path in enumerate(video_paths, start=1):
        source_id = f"source_{source_number:03d}"
        try:
            metadata = probe_video(video_path, config.ffprobe_bin)
        except ProbeError as exc:
            raise ProbeError(f"cannot analyze {video_path.name}: {exc}") from exc
        source_context = {
            "asset_id": source_id,
            "uri": video_path.as_uri(),
            "duration_seconds": metadata.duration,
            "width": metadata.width,
            "height": metadata.height,
            "fps": metadata.fps,
            "has_audio": metadata.has_audio,
        }
        sources.append(source_context)

        transcript: dict[str, Any] = {"language": "", "language_probability": None, "segments": []}
        if active_speech_provider and metadata.has_audio:
            speech_cache_path = cache_root / source_id / "speech.json"
            cached_transcript = _load_speech_cache(speech_cache_path, active_speech_provider, video_path)
            if cached_transcript is not None:
                transcript = cached_transcript
                LOGGER.info("Reusing speech cache for %s", source_id)
            else:
                LOGGER.info(
                    "Transcribing %s with %s/%s",
                    video_path.name,
                    active_speech_provider.name,
                    active_speech_provider.model,
                )
                transcript = active_speech_provider.transcribe(video_path)
                _save_speech_cache(speech_cache_path, active_speech_provider, video_path, transcript)
        elif active_speech_provider and not metadata.has_audio:
            warnings.append(
                {"code": "SOURCE_HAS_NO_AUDIO", "message": "Speech provider requested but source has no audio", "asset_id": source_id}
            )

        scene_ranges, scene_warning = detect_scenes(
            video_path,
            metadata.duration,
            threshold=config.scene_threshold,
            min_scene_length=config.min_scene_length,
        )
        if scene_warning:
            warnings.append({"code": "SCENE_DETECTOR_FALLBACK", "message": scene_warning, "asset_id": source_id})

        source_shots: list[dict[str, Any]] = []
        review_frames: dict[str, list[Path]] = {}
        for shot_number, scene in enumerate(scene_ranges, start=1):
            shot_id = f"{source_id}_shot_{shot_number:03d}"
            LOGGER.info(
                "Analyzing %s (%s %.3f-%.3fs)",
                shot_id,
                video_path.name,
                scene.start,
                scene.end,
            )
            cached = cache_shot_frames(
                video_path,
                scene,
                shot_id,
                cache_root,
                keyframe_width=config.keyframe_width,
                proxy_width=config.proxy_width,
                ffmpeg_bin=config.ffmpeg_bin,
            )
            if not cached.keyframes:
                raise OSError(f"{shot_id} has no readable keyframes; refusing to invent semantic evidence")
            technical = measure_frames(cached.keyframes, list(cached.warnings))
            for warning in technical.get("warnings", []):
                warnings.append(
                    {"code": warning.split(":", 1)[0][:100].upper(), "message": warning, "asset_id": source_id, "shot_id": shot_id}
                )
            keyframe_times = sample_times(scene)[: len(cached.keyframes)]
            context = {
                "shot_id": shot_id,
                "source_asset_id": source_id,
                "source_uri": video_path.as_uri(),
                "source_filename": video_path.name,
                "source_duration_seconds": metadata.duration,
                "start_seconds": round(scene.start, 6),
                "end_seconds": round(scene.end, 6),
                "duration_seconds": round(scene.duration, 6),
                "keyframe_timestamps_seconds": keyframe_times,
            }
            semantic_cache_path = cache_root / shot_id / "semantic.json"
            semantic = (
                _load_cached_semantics(semantic_cache_path, provider, context, cached.keyframes)
                if config.reuse_semantic_cache
                else None
            )
            if semantic is None:
                semantic = provider.analyze_shot(context, cached.keyframes)
                _save_semantic_cache(semantic_cache_path, provider, context, cached.keyframes, semantic)
            else:
                LOGGER.info("Reusing semantic cache for %s", shot_id)
            quality = _quality_from_metrics(technical, bool(cached.keyframes))
            evidence: dict[str, Any] = {
                "keyframe_uris": [_manifest_uri(path, output.parent) for path in cached.keyframes],
                "ocr_text": semantic["ocr_text"],
                "speech_text": speech_text_for_range(transcript, scene.start, scene.end),
            }
            if cached.proxy:
                evidence["proxy_clip_uri"] = _manifest_uri(cached.proxy, output.parent)
            review_frames[shot_id] = list(cached.keyframes)
            source_shots.append(
                {
                    "shot_id": shot_id,
                    "source_asset_id": source_id,
                    "start_seconds": round(scene.start, 6),
                    "end_seconds": round(scene.end, 6),
                    "duration_seconds": round(scene.duration, 6),
                    "description": semantic["description"],
                    "semantic_tags": semantic["semantic_tags"],
                    "people": semantic["people"],
                    "product": semantic["product"],
                    "action": semantic["action"],
                    "composition": semantic["composition"],
                    "quality": quality,
                    "motion": {
                        "intensity": _unit(technical.get("motion_score")),
                        "camera_motion": semantic["camera_motion"],
                    },
                    "transition_fitness": semantic["transition_fitness"],
                    "evidence": evidence,
                    "model_confidence": semantic["model_confidence"],
                }
            )

        if not config.source_joint_review:
            shots.extend(source_shots)
            continue

        review_contexts = [
            {
                "shot_id": shot["shot_id"],
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
                "description": shot["description"],
                "semantic_tags": shot["semantic_tags"],
                "people": shot["people"],
                "product": shot["product"],
                "action": shot["action"],
                "model_confidence": shot["model_confidence"],
                "ocr_text": shot["evidence"]["ocr_text"],
                "speech_text": shot["evidence"]["speech_text"],
            }
            for shot in source_shots
        ]
        review_cache_path = _source_review_cache_path(cache_root, source_id, provider)
        review = (
            _load_source_review_cache(
                review_cache_path, provider, source_context, review_contexts, review_frames
            )
            if config.reuse_semantic_cache
            else None
        )
        if review is None:
            LOGGER.info("Jointly reviewing all %d shots from %s with %s/%s", len(source_shots), source_id, provider.name, provider.model)
            review = provider.review_source_shots(source_context, review_contexts, review_frames)
            _save_source_review_cache(
                review_cache_path, provider, source_context, review_contexts, review_frames, review
            )
        else:
            LOGGER.info("Reusing source review cache for %s", source_id)
        _apply_source_review(source_shots, review)
        shots.extend(source_shots)

    if not shots:
        raise FootageIndexValidationError("analysis produced no shots")
    analyzer_model = f"{provider.name}/{provider.model}"
    if active_speech_provider:
        analyzer_model += f"; {active_speech_provider.name}/{active_speech_provider.model}"
    document: dict[str, Any] = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analyzer": {
            "name": "shot-card-analyzer",
            "version": ANALYZER_VERSION,
            "model": analyzer_model,
            "prompt_version": provider.prompt_version,
        },
        "sources": sources,
        "shots": shots,
        "warnings": warnings,
    }
    _validate_footage_index(document, _schema_path())
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return document
