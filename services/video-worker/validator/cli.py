"""CLI for validating an Edit Plan against its Footage Index."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .edit_plan import EditPlanValidationError, ValidatorConfig, all_candidates_valid, validate_edit_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate edit_plan.json against footage_index.json")
    parser.add_argument("edit_plan", type=Path)
    parser.add_argument("footage_index", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New validated Edit Plan path")
    parser.add_argument("--target-duration", type=float, default=15.0)
    parser.add_argument("--duration-tolerance", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError(f"refusing to overwrite output: {args.output}")
        plan = json.loads(args.edit_plan.read_text(encoding="utf-8"))
        footage = json.loads(args.footage_index.read_text(encoding="utf-8"))
        validated = validate_edit_plan(
            plan,
            footage,
            ValidatorConfig(args.target_duration, args.duration_tolerance),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)
    except (OSError, json.JSONDecodeError, EditPlanValidationError, ValueError) as exc:
        logging.error("Validation failed: %s", exc)
        return 2
    print(
        json.dumps(
            {
                "candidate_count": len(validated["candidates"]),
                "valid": all_candidates_valid(validated),
                "error_count": sum(len(candidate["validation"]["errors"]) for candidate in validated["candidates"]),
            },
            ensure_ascii=False,
        )
    )
    return 0 if all_candidates_valid(validated) else 1
