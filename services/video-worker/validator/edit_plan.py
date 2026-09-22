"""Deterministic semantic and contract validation for Edit Plan 1.0."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class EditPlanValidationError(RuntimeError):
    """Raised when an Edit Plan cannot be structurally validated."""


@dataclass(frozen=True)
class ValidatorConfig:
    target_duration_seconds: float = 15.0
    duration_tolerance_seconds: float = 2.0
    epsilon: float = 0.001
    generic_product: bool = False


CUSTOMIZATION_ROLES = {
    "unboxing",
    "multiple_variants",
    "photo_selection",
    "text_customization",
    "gift_box",
}
WEARING_RESULT_ROLES = {"wearing_result", "daily_life"}


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "schemas" / "edit_plan.schema.json"


def _message(code: str, message: str, clip: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if clip:
        if isinstance(clip.get("sequence"), int):
            result["sequence"] = clip["sequence"]
        if isinstance(clip.get("shot_id"), str):
            result["shot_id"] = clip["shot_id"]
    return result


def _evidence_corpus(shot: dict[str, Any]) -> list[str]:
    evidence = shot.get("evidence") or {}
    product = shot.get("product") or {}
    action = shot.get("action") or {}
    return [
        str(shot.get("description") or ""),
        str(evidence.get("ocr_text") or ""),
        str(evidence.get("speech_text") or ""),
        str(product.get("identity_label") or ""),
        str(product.get("state_before") or ""),
        str(product.get("state_after") or ""),
        str(action.get("label") or ""),
        str(action.get("state_before") or ""),
        str(action.get("state_after") or ""),
    ]


def _has_exact_evidence(evidence: str, shot: dict[str, Any]) -> bool:
    needle = " ".join(evidence.casefold().split())
    if len(needle) < 4:
        return False
    return any(needle in " ".join(item.casefold().split()) for item in _evidence_corpus(shot) if item)


def _validate_schema(document: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise EditPlanValidationError("jsonschema is required to validate edit plans") from exc
    try:
        schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditPlanValidationError("cannot read edit_plan.schema.json") from exc
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
            for error in errors[:10]
        )
        raise EditPlanValidationError(f"Edit Plan violates schema 1.0.0: {details}")


def validate_edit_plan(
    document: dict[str, Any],
    footage_index: dict[str, Any],
    config: ValidatorConfig | None = None,
) -> dict[str, Any]:
    """Return a copy with deterministic candidate validation results."""
    config = config or ValidatorConfig()
    plan = copy.deepcopy(document)
    if plan.get("job_id") != footage_index.get("job_id"):
        raise EditPlanValidationError("Edit Plan and Footage Index job_id values differ")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 2:
        raise EditPlanValidationError("Edit Plan must contain one or two candidates")
    # Reject malformed scalar/container types before relational checks cast values
    # to floats. The document is checked again after validation fields are updated.
    _validate_schema(plan)

    sources = {source["asset_id"]: source for source in footage_index.get("sources", [])}
    shots = {shot["shot_id"]: shot for shot in footage_index.get("shots", [])}
    candidate_sequences: list[tuple[str, ...]] = []

    for candidate in candidates:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        checks = {
            "time_ranges": True,
            "action_order": True,
            "caption_evidence": True,
            "duplicate_shots": True,
            "required_roles": True,
            "duration": True,
        }
        timeline = candidate.get("timeline")
        if not isinstance(timeline, list) or not timeline:
            raise EditPlanValidationError("candidate timeline must be a non-empty array")

        selected_ids: list[str] = []
        selected_shots: dict[str, dict[str, Any]] = {}
        timeline_positions: dict[str, int] = {}
        expected_timeline_in = 0.0
        roles: set[str] = set()

        for index, clip in enumerate(timeline, start=1):
            shot_id = str(clip.get("shot_id") or "")
            source_id = str(clip.get("source_asset_id") or "")
            if clip.get("sequence") != index:
                checks["time_ranges"] = False
                errors.append(_message("SEQUENCE_NOT_CONTIGUOUS", f"Expected sequence {index}", clip))
            if abs(float(clip.get("timeline_in_seconds", -1)) - expected_timeline_in) > config.epsilon:
                checks["time_ranges"] = False
                errors.append(
                    _message(
                        "TIMELINE_GAP_OR_OVERLAP",
                        f"timeline_in_seconds must be {expected_timeline_in:.6f}",
                        clip,
                    )
                )

            shot = shots.get(shot_id)
            source = sources.get(source_id)
            if shot is None:
                checks["time_ranges"] = False
                errors.append(_message("SHOT_NOT_FOUND", "Referenced shot does not exist", clip))
            elif shot.get("source_asset_id") != source_id:
                checks["time_ranges"] = False
                errors.append(_message("SHOT_SOURCE_MISMATCH", "Shot does not belong to source_asset_id", clip))
            if source is None:
                checks["time_ranges"] = False
                errors.append(_message("SOURCE_NOT_FOUND", "Referenced source does not exist", clip))

            try:
                source_in = float(clip["source_in_seconds"])
                source_out = float(clip["source_out_seconds"])
                duration = float(clip["duration_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EditPlanValidationError(f"invalid numeric clip fields at sequence {index}") from exc
            if source_out <= source_in:
                checks["time_ranges"] = False
                errors.append(_message("INVALID_SOURCE_RANGE", "source_out_seconds must exceed source_in_seconds", clip))
            if abs(duration - (source_out - source_in)) > config.epsilon:
                checks["time_ranges"] = False
                errors.append(_message("CLIP_DURATION_MISMATCH", "duration_seconds must equal source_out - source_in", clip))
            if shot and (
                source_in < float(shot["start_seconds"]) - config.epsilon
                or source_out > float(shot["end_seconds"]) + config.epsilon
            ):
                checks["time_ranges"] = False
                errors.append(_message("CLIP_OUTSIDE_SHOT", "Clip trim exceeds its Shot Card range", clip))
            if source and source_out > float(source["duration_seconds"]) + config.epsilon:
                checks["time_ranges"] = False
                errors.append(_message("CLIP_OUTSIDE_SOURCE", "Clip trim exceeds source duration", clip))

            transition = clip.get("transition") or {}
            transition_duration = float(transition.get("duration_seconds", 0))
            if transition.get("type") == "cut" and transition_duration > config.epsilon:
                checks["time_ranges"] = False
                errors.append(_message("CUT_HAS_DURATION", "A cut transition must have zero duration", clip))
            if transition_duration > duration / 2 + config.epsilon:
                checks["time_ranges"] = False
                errors.append(_message("TRANSITION_TOO_LONG", "Transition exceeds half of clip duration", clip))

            if shot_id in selected_ids:
                checks["duplicate_shots"] = False
                errors.append(_message("DUPLICATE_SHOT", "The same shot is selected more than once", clip))
            selected_ids.append(shot_id)
            timeline_positions.setdefault(shot_id, index)
            if shot:
                selected_shots[shot_id] = shot

            role = str(clip.get("role") or "")
            roles.add(role)
            if shot and role not in set(shot.get("semantic_tags") or []):
                checks["required_roles"] = False
                errors.append(_message("ROLE_NOT_SUPPORTED", "Clip role is not supported by the Shot Card", clip))

            caption_evidence = str(clip.get("caption_evidence") or "").strip()
            if not shot or not _has_exact_evidence(caption_evidence, shot):
                checks["caption_evidence"] = False
                errors.append(
                    _message(
                        "CAPTION_EVIDENCE_NOT_FOUND",
                        "caption_evidence must be an exact substring from description, OCR, speech, product, or action evidence",
                        clip,
                    )
                )

            expected_timeline_in = round(expected_timeline_in + duration, 6)

        candidate_sequences.append(tuple(selected_ids))
        if len(selected_ids) != len(set(selected_ids)):
            checks["duplicate_shots"] = False

        for shot_id, shot in selected_shots.items():
            for relation in shot.get("continuity", []):
                other_id = relation.get("other_shot_id")
                if other_id not in timeline_positions:
                    continue
                current_position = timeline_positions[shot_id]
                other_position = timeline_positions[other_id]
                relation_name = relation.get("relation")
                if relation_name == "must_precede" and current_position >= other_position:
                    checks["action_order"] = False
                    errors.append(_message("MUST_PRECEDE_VIOLATION", f"{shot_id} must precede {other_id}"))
                elif relation_name == "must_follow" and current_position <= other_position:
                    checks["action_order"] = False
                    errors.append(_message("MUST_FOLLOW_VIOLATION", f"{shot_id} must follow {other_id}"))
                elif relation_name == "conflict":
                    checks["action_order"] = False
                    errors.append(_message("CONTINUITY_CONFLICT", f"{shot_id} conflicts with {other_id}"))
                elif relation_name == "duplicate":
                    checks["duplicate_shots"] = False
                    errors.append(_message("VISUAL_DUPLICATE", f"{shot_id} duplicates {other_id}"))
                elif relation_name == "good_before" and current_position >= other_position:
                    warnings.append(_message("GOOD_BEFORE_REVERSED", f"Prefer {shot_id} before {other_id}"))
                elif relation_name == "good_after" and current_position <= other_position:
                    warnings.append(_message("GOOD_AFTER_REVERSED", f"Prefer {shot_id} after {other_id}"))

        if not roles.intersection({"product_macro", "product_detail", "product_display"} if config.generic_product else {"product_macro"}):
            checks["required_roles"] = False
            errors.append(_message("PRODUCT_MACRO_REQUIRED", "Candidate must include a product_macro clip"))
        if not config.generic_product and not roles.intersection(CUSTOMIZATION_ROLES):
            checks["required_roles"] = False
            errors.append(_message("CUSTOMIZATION_REQUIRED", "Candidate must show customization or a core selling point"))
        if not config.generic_product and not roles.intersection(WEARING_RESULT_ROLES):
            checks["required_roles"] = False
            errors.append(_message("WEARING_RESULT_REQUIRED", "Candidate must include a wearing or usage result"))

        declared_total = float(candidate.get("total_body_duration_seconds", -1))
        declared_target = float(candidate.get("target_body_duration_seconds", -1))
        if abs(declared_total - expected_timeline_in) > config.epsilon:
            checks["duration"] = False
            errors.append(_message("TOTAL_DURATION_MISMATCH", "total_body_duration_seconds does not match timeline"))
        if abs(declared_target - config.target_duration_seconds) > config.epsilon:
            checks["duration"] = False
            errors.append(_message("TARGET_DURATION_MISMATCH", f"Target duration must be {config.target_duration_seconds:.1f}s"))
        if abs(expected_timeline_in - config.target_duration_seconds) > config.duration_tolerance_seconds:
            checks["duration"] = False
            errors.append(
                _message(
                    "BODY_DURATION_OUT_OF_RANGE",
                    f"Body duration {expected_timeline_in:.3f}s is outside target tolerance",
                )
            )

        candidate["validation"] = {
            "valid": not errors,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
        }
        candidate["status"] = "validated" if not errors else "rejected"

    if len(candidates) == 2:
        same_sequence = candidate_sequences[0] == candidate_sequences[1]
        same_narrative = str(candidates[0].get("narrative", "")).strip().casefold() == str(
            candidates[1].get("narrative", "")
        ).strip().casefold()
        if same_sequence or same_narrative:
            for candidate in candidates:
                candidate["validation"]["valid"] = False
                candidate["validation"]["checks"]["duplicate_shots"] = False
                candidate["validation"]["errors"].append(
                    _message("CANDIDATES_NOT_DISTINCT", "Two candidates must use distinct narratives and shot sequences")
                )
                candidate["status"] = "rejected"

    _validate_schema(plan)
    return plan


def all_candidates_valid(document: dict[str, Any]) -> bool:
    candidates = document.get("candidates") or []
    return bool(candidates) and all(candidate.get("validation", {}).get("valid") is True for candidate in candidates)
