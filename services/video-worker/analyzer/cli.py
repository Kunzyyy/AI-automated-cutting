"""Command-line entry point for offline material analysis."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .pipeline import AnalyzerConfig, FootageIndexValidationError, analyze_directory
from .probe import ProbeError
from .semantic import SemanticProviderError
from .speech import SpeechProviderError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze videos and write footage_index.json")
    parser.add_argument("input_dir", type=Path, help="Directory containing source videos")
    parser.add_argument("--output", type=Path, help="Manifest path (default: INPUT/analysis/footage_index.json)")
    parser.add_argument("--cache-dir", type=Path, help="Keyframe cache directory")
    parser.add_argument("--scene-threshold", type=float, default=20.0)
    parser.add_argument("--min-scene-length", type=float, default=0.5)
    parser.add_argument("--keyframe-width", type=int, default=960)
    parser.add_argument("--proxy-width", type=int, default=480)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--job-id", default="local-analysis", help="Footage Index job_id")
    parser.add_argument("--provider", choices=("auto", "minimax", "openai"), default="auto")
    parser.add_argument("--model", help="Override the provider model name")
    parser.add_argument(
        "--no-semantic-cache",
        action="store_true",
        help="Call the multimodal provider again even when a matching per-shot cache exists",
    )
    parser.add_argument("--speech-provider", choices=("none", "faster-whisper"), default="none")
    parser.add_argument("--speech-model", default="medium")
    parser.add_argument("--speech-device", default="cpu")
    parser.add_argument("--speech-compute-type", default="int8")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    for noisy_logger in ("openai", "httpx", "httpcore"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    config = AnalyzerConfig(
        scene_threshold=args.scene_threshold,
        min_scene_length=args.min_scene_length,
        keyframe_width=args.keyframe_width,
        proxy_width=args.proxy_width,
        ffmpeg_bin=args.ffmpeg,
        ffprobe_bin=args.ffprobe,
        semantic_provider=args.provider,
        semantic_model=args.model,
        reuse_semantic_cache=not args.no_semantic_cache,
        speech_provider=args.speech_provider,
        speech_model=args.speech_model,
        speech_device=args.speech_device,
        speech_compute_type=args.speech_compute_type,
    )
    try:
        manifest = analyze_directory(args.input_dir, args.output, args.cache_dir, config, job_id=args.job_id)
    except (OSError, ValueError, ProbeError, SemanticProviderError, SpeechProviderError, FootageIndexValidationError) as exc:
        logging.error("Analysis failed: %s", exc)
        return 2
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "job_id": manifest["job_id"],
                "source_count": len(manifest["sources"]),
                "shot_count": len(manifest["shots"]),
                "analyzer_model": manifest["analyzer"].get("model"),
            },
            ensure_ascii=False,
        )
    )
    return 0
