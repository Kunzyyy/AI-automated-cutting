"""CLI for generating a validated Edit Plan from a Footage Index."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from validator import EditPlanValidationError

from .pipeline import DirectorConfig, DirectorValidationError, generate_edit_plan
from .provider import DirectorProviderError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a validated edit_plan.json")
    parser.add_argument("footage_index", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("auto", "minimax", "openai"), default="auto")
    parser.add_argument("--model")
    parser.add_argument("--candidate-count", type=int, choices=(1, 2), default=2)
    parser.add_argument("--target-duration", type=float, default=15.0)
    parser.add_argument("--duration-tolerance", type=float, default=2.0)
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
    try:
        plan = generate_edit_plan(
            args.footage_index,
            args.output,
            DirectorConfig(
                provider=args.provider,
                model=args.model,
                candidate_count=args.candidate_count,
                target_duration_seconds=args.target_duration,
                duration_tolerance_seconds=args.duration_tolerance,
            ),
        )
    except (OSError, ValueError, DirectorProviderError, DirectorValidationError, EditPlanValidationError) as exc:
        logging.error("Director failed: %s", exc)
        return 2
    print(
        json.dumps(
            {
                "schema_version": plan["schema_version"],
                "job_id": plan["job_id"],
                "candidate_count": len(plan["candidates"]),
                "candidate_ids": [candidate["candidate_id"] for candidate in plan["candidates"]],
            },
            ensure_ascii=False,
        )
    )
    return 0
