"""AI Director pipeline: Footage Index -> validated Edit Plan 1.0."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validator import ValidatorConfig, all_candidates_valid, validate_edit_plan

from .provider import DirectorProvider, DirectorProviderError, create_director_provider


LOGGER = logging.getLogger(__name__)
DIRECTOR_VERSION = "1.0.0"


class DirectorValidationError(RuntimeError):
    """Raised when bounded Director revisions cannot produce a valid plan."""


@dataclass(frozen=True)
class DirectorConfig:
    provider: str = "auto"
    model: str | None = None
    candidate_count: int = 2
    target_duration_seconds: float = 15.0
    duration_tolerance_seconds: float = 2.0
    generic_product: bool = False


def _manifest_uri(path: Path, manifest_dir: Path) -> str:
    try:
        return path.resolve().relative_to(manifest_dir.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_uri()


def _build_document(
    payload: dict[str, Any],
    footage_index: dict[str, Any],
    footage_path: Path,
    output: Path,
    provider: DirectorProvider,
    config: DirectorConfig,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for candidate_index, decision in enumerate(payload["candidates"], start=1):
        timeline: list[dict[str, Any]] = []
        timeline_in = 0.0
        for sequence, selected in enumerate(decision["timeline"], start=1):
            source_in = round(float(selected["source_in_seconds"]), 6)
            source_out = round(float(selected["source_out_seconds"]), 6)
            duration = round(source_out - source_in, 6)
            clip: dict[str, Any] = {
                "sequence": sequence,
                "shot_id": selected["shot_id"].strip(),
                "source_asset_id": selected["source_asset_id"].strip(),
                "source_in_seconds": source_in,
                "source_out_seconds": source_out,
                "timeline_in_seconds": round(timeline_in, 6),
                "duration_seconds": duration,
                "role": selected["role"],
                "reason": selected["reason"].strip()[:500],
                "caption_intent": selected["caption_intent"].strip()[:300],
                "caption_evidence": selected["caption_evidence"].strip()[:500],
                "crop_strategy": selected["crop_strategy"],
                "transition": selected["transition"],
            }
            if "speed" in selected:
                clip["speed"] = selected["speed"]
            timeline.append(clip)
            timeline_in = round(timeline_in + duration, 6)
        candidates.append(
            {
                "candidate_id": f"candidate-{candidate_index}",
                "candidate_index": candidate_index,
                "revision": 0,
                "status": "draft",
                "narrative": decision["narrative"].strip()[:1000],
                "target_body_duration_seconds": config.target_duration_seconds,
                "total_body_duration_seconds": timeline_in,
                "timeline": timeline,
                "music_direction": decision["music_direction"],
                "caption_direction": decision["caption_direction"],
                "validation": {
                    "valid": False,
                    "checks": {
                        "time_ranges": False,
                        "action_order": False,
                        "caption_evidence": False,
                        "duplicate_shots": False,
                        "required_roles": False,
                        "duration": False,
                    },
                    "errors": [],
                    "warnings": [],
                },
            }
        )
    return {
        "schema_version": "1.0.0",
        "job_id": footage_index["job_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "footage_index_uri": _manifest_uri(footage_path, output.parent),
        "director": {
            "name": "evidence-grounded-ai-director",
            "version": DIRECTOR_VERSION,
            "model": f"{provider.name}/{provider.model}",
            "prompt_version": provider.prompt_version,
        },
        "candidates": candidates,
    }


def _validation_feedback(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_index": candidate["candidate_index"],
            "errors": candidate["validation"]["errors"],
            "warnings": candidate["validation"]["warnings"],
        }
        for candidate in plan["candidates"]
        if not candidate["validation"]["valid"]
    ]


def generate_edit_plan(
    footage_index_path: str | Path,
    output_path: str | Path,
    config: DirectorConfig | None = None,
    *,
    provider: DirectorProvider | None = None,
) -> dict[str, Any]:
    """Generate, validate, and atomically write an Edit Plan without heuristic fallback."""
    config = config or DirectorConfig()
    if config.candidate_count not in {1, 2}:
        raise ValueError("candidate_count must be one or two")
    footage_path = Path(footage_index_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite output: {output}")
    try:
        footage_index = json.loads(footage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DirectorValidationError(f"cannot read Footage Index: {footage_path}") from exc
    if footage_index.get("schema_version") != "1.0.0" or not footage_index.get("shots"):
        raise DirectorValidationError("Footage Index must be a non-empty 1.0.0 document")
    active_provider = provider or create_director_provider(config.provider, config.model)
    validator_config = ValidatorConfig(config.target_duration_seconds, config.duration_tolerance_seconds, generic_product=config.generic_product)

    previous_payload: dict[str, Any] | None = None
    feedback: list[dict[str, Any]] | None = None
    for revision_attempt in range(2):
        try:
            payload = active_provider.generate_candidates(
                footage_index,
                config.candidate_count,
                config.target_duration_seconds,
                previous_payload=previous_payload,
                validation_feedback=feedback,
            )
        except DirectorProviderError as exc:
            if revision_attempt == 0:
                feedback = [
                    {
                        "candidate_index": 0,
                        "errors": [{"code": "DIRECTOR_PAYLOAD_INVALID", "message": str(exc)}],
                        "warnings": [],
                    }
                ]
                LOGGER.warning("Director response was structurally invalid; requesting the single bounded correction")
                continue
            prior_feedback = f"; prior validation feedback: {json.dumps(feedback, ensure_ascii=False)}" if feedback else ""
            raise DirectorValidationError(f"Director provider failed twice: {exc}{prior_feedback}") from exc
        plan = _build_document(payload, footage_index, footage_path, output, active_provider, config)
        validated = validate_edit_plan(plan, footage_index, validator_config)
        if all_candidates_valid(validated):
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output)
            return validated
        previous_payload = payload
        feedback = _validation_feedback(validated)
        if revision_attempt == 0:
            LOGGER.warning("Director plan failed deterministic validation; requesting one bounded revision")

    raise DirectorValidationError(f"Director plan failed validation twice: {json.dumps(feedback, ensure_ascii=False)}")
