"""Structured multimodal semantics for Shot Cards.

The analyzer deliberately has no implicit heuristic fallback.  A caller must
provide a working multimodal provider; missing credentials, transport errors,
invalid JSON, and out-of-contract fields are surfaced as explicit failures.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol, Sequence


SEMANTIC_TAGS = {
    "unboxing",
    "product_macro",
    "multiple_variants",
    "photo_selection",
    "text_customization",
    "wearing_action",
    "wearing_result",
    "touching_memory",
    "gift_box",
    "daily_life",
    "emotional_close",
    "product_detail", "product_display", "usage_demo", "usage_result", "packaging",
}
ACTION_PHASES = {"setup", "begin", "middle", "complete", "result", "none"}
DIRECTIONS = {"left", "right", "up", "down", "toward_camera", "away_from_camera", "mixed", "static"}
SHOT_SIZES = {"extreme_close_up", "close_up", "medium", "wide", "screen_capture", "mixed"}
CAPTION_REGIONS = {"top", "upper_middle", "center", "lower_middle", "bottom"}
CAMERA_MOTIONS = {"static", "pan", "tilt", "zoom", "push_in", "pull_out", "handheld", "mixed"}
CONTINUITY_RELATIONS = {"must_precede", "must_follow", "good_before", "good_after", "duplicate", "conflict"}
LOGGER = logging.getLogger(__name__)


class SemanticProviderError(RuntimeError):
    """Raised when semantic analysis cannot produce a trustworthy Shot Card."""


class SemanticProvider(Protocol):
    """Minimal provider boundary used by the analyzer and unit tests."""

    name: str
    model: str
    prompt_version: str

    def analyze_shot(self, context: dict[str, Any], keyframes: Sequence[Path]) -> dict[str, Any]:
        """Return validated semantic fields for exactly one shot."""

    def review_source_shots(
        self,
        source_context: dict[str, Any],
        shot_contexts: Sequence[dict[str, Any]],
        shot_frames: dict[str, Sequence[Path]],
    ) -> dict[str, Any]:
        """Jointly review every shot from one source and return all revisions."""


def _registry_value(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ)
        try:
            value, _ = winreg.QueryValueEx(key, name)
        finally:
            winreg.CloseKey(key)
        return str(value).strip() or None
    except (OSError, ImportError):
        return None


def _credential(name: str) -> str | None:
    return (os.environ.get(name) or _registry_value(name) or "").strip() or None


def provider_is_configured(provider: str = "auto") -> bool:
    """Check credential presence without reading or printing secret contents."""
    provider = provider.lower()
    minimax = bool(_credential("MINIMAX_API_KEY") and _credential("MINIMAX_BASE_URL"))
    openai = bool(_credential("OPENAI_API_KEY"))
    if provider == "minimax":
        return minimax
    if provider == "openai":
        return openai
    return minimax or openai


def create_semantic_provider(provider: str = "auto", model: str | None = None) -> "OpenAICompatibleSemanticProvider":
    """Create MiniMax/OpenAI adapter, preferring MiniMax when ``auto`` is used."""
    provider = provider.lower()
    if provider not in {"auto", "minimax", "openai"}:
        raise SemanticProviderError(f"unsupported semantic provider: {provider}")

    minimax_key = _credential("MINIMAX_API_KEY")
    minimax_url = _credential("MINIMAX_BASE_URL")
    openai_key = _credential("OPENAI_API_KEY")

    selected = provider
    if selected == "auto":
        selected = "minimax" if minimax_key and minimax_url else "openai"

    if selected == "minimax":
        if not minimax_key or not minimax_url:
            raise SemanticProviderError("MiniMax is not configured: MINIMAX_API_KEY and MINIMAX_BASE_URL are required")
        return OpenAICompatibleSemanticProvider(
            api_key=minimax_key,
            base_url=minimax_url,
            model=model or _credential("MINIMAX_MODEL") or "MiniMax-M3",
            provider_name="minimax",
        )

    if not openai_key:
        raise SemanticProviderError("OpenAI is not configured: OPENAI_API_KEY is required")
    return OpenAICompatibleSemanticProvider(
        api_key=openai_key,
        base_url=None,
        model=model or _credential("OPENAI_VISION_MODEL") or "gpt-4o",
        provider_name="openai",
    )


def _data_url(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SemanticProviderError(f"cannot read keyframe {path}: {exc}") from exc
    if not data:
        raise SemanticProviderError(f"keyframe is empty: {path}")
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _contact_sheet_data_url(shot_id: str, paths: Sequence[Path]) -> str:
    """Build one visibly labelled early/middle/late image for a single shot."""
    if not paths:
        raise SemanticProviderError(f"cannot build an empty contact sheet for {shot_id}")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise SemanticProviderError("opencv and numpy are required for source review contact sheets") from exc

    cell_width = 320
    image_height = 500
    label_height = 48
    temporal_labels = ["EARLY", "MIDDLE", "LATE"] if len(paths) == 3 else [f"FRAME_{index + 1}" for index in range(len(paths))]
    panels = []
    for path, temporal_label in zip(paths, temporal_labels):
        try:
            encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        except OSError:
            image = None
        if image is None:
            raise SemanticProviderError(f"cannot decode source review frame: {path.name}")

        panel = np.full((label_height + image_height, cell_width, 3), 20, dtype=np.uint8)
        height, width = image.shape[:2]
        scale = min(cell_width / width, image_height / height)
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
        x = (cell_width - resized_width) // 2
        y = label_height + (image_height - resized_height) // 2
        panel[y : y + resized_height, x : x + resized_width] = resized
        cv2.putText(
            panel,
            f"{shot_id} {temporal_label}",
            (7, 31),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.43,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        panels.append(panel)

    contact_sheet = np.hstack(panels)
    ok, encoded_sheet = cv2.imencode(".jpg", contact_sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise SemanticProviderError(f"cannot encode source review contact sheet for {shot_id}")
    return f"data:image/jpeg;base64,{base64.b64encode(encoded_sheet.tobytes()).decode('ascii')}"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif hasattr(item, "text"):
                parts.append(str(item.text))
        return "".join(parts)
    return str(content or "")


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Parse JSON while tolerating only well-known model display wrappers."""
    candidate = raw.strip()
    think_match = re.fullmatch(r"<think>[\s\S]*?</think>\s*([\s\S]+)", candidate)
    if think_match:
        candidate = think_match.group(1).strip()
    fence_match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, flags=re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise SemanticProviderError("semantic response is not a single JSON object") from exc
    if not isinstance(payload, dict):
        raise SemanticProviderError("semantic response must be a JSON object")
    return payload


def _as_string_list(value: Any, field: str, allowed: set[str] | None = None, *, min_items: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < min_items:
        raise SemanticProviderError(f"{field} must be an array with at least {min_items} item(s)")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SemanticProviderError(f"{field} contains a non-string value")
        clean = item.strip()
        if not clean:
            continue
        if allowed is not None and clean not in allowed:
            raise SemanticProviderError(f"{field} contains unsupported value: {clean}")
        if clean not in result:
            result.append(clean)
    if len(result) < min_items:
        raise SemanticProviderError(f"{field} has no usable value")
    return result


def _unit_score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticProviderError(f"{field} must be a number")
    score = float(value)
    if not 0 <= score <= 1:
        raise SemanticProviderError(f"{field} must be between 0 and 1")
    return round(score, 4)


def _check_fields(
    value: dict[str, Any],
    field: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    if missing:
        raise SemanticProviderError(f"{field} missing required field(s): {', '.join(missing)}")
    unexpected = sorted(set(value) - required - optional)
    if unexpected:
        raise SemanticProviderError(f"{field} contains unexpected field(s): {', '.join(unexpected)}")


def validate_semantic_payload(payload: Any) -> dict[str, Any]:
    """Validate and normalize the model-owned subset of a Shot Card."""
    if not isinstance(payload, dict):
        raise SemanticProviderError("semantic response must be a JSON object")

    _check_fields(
        payload,
        "semantic response",
        required={
            "description",
            "semantic_tags",
            "people",
            "product",
            "action",
            "composition",
            "transition_fitness",
            "camera_motion",
            "ocr_text",
            "model_confidence",
        },
    )

    required_objects = ["people", "product", "action", "composition", "transition_fitness"]
    for field in required_objects:
        if not isinstance(payload.get(field), dict):
            raise SemanticProviderError(f"semantic response missing object: {field}")

    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SemanticProviderError("description must be a non-empty string")

    people = payload["people"]
    product = payload["product"]
    action = payload["action"]
    composition = payload["composition"]
    transition = payload["transition_fitness"]

    _check_fields(
        people,
        "people",
        required={"visible", "identity_labels", "clothing_labels", "emotion"},
    )
    _check_fields(
        product,
        "product",
        required={"visible", "identity_label", "visibility_score", "state_before", "state_after"},
    )
    _check_fields(
        action,
        "action",
        required={"label", "phase", "state_before", "state_after", "direction"},
    )
    _check_fields(
        composition,
        "composition",
        required={"shot_size", "subject_bbox", "safe_caption_regions"},
    )
    _check_fields(
        transition,
        "transition_fitness",
        required={"good_entry", "good_exit"},
        optional={"notes"},
    )

    for field, value in (("people.visible", people.get("visible")), ("product.visible", product.get("visible"))):
        if not isinstance(value, bool):
            raise SemanticProviderError(f"{field} must be boolean")

    phase = action.get("phase")
    direction = action.get("direction")
    shot_size = composition.get("shot_size")
    camera_motion = payload.get("camera_motion")
    if phase not in ACTION_PHASES:
        raise SemanticProviderError(f"unsupported action.phase: {phase}")
    if direction not in DIRECTIONS:
        raise SemanticProviderError(f"unsupported action.direction: {direction}")
    if shot_size not in SHOT_SIZES:
        raise SemanticProviderError(f"unsupported composition.shot_size: {shot_size}")
    if camera_motion not in CAMERA_MOTIONS:
        raise SemanticProviderError(f"unsupported camera_motion: {camera_motion}")

    bbox = composition.get("subject_bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise SemanticProviderError("composition.subject_bbox must contain four normalized numbers")
    normalized_bbox = [_unit_score(item, "composition.subject_bbox") for item in bbox]

    for field in ("identity_label", "state_before", "state_after"):
        if not isinstance(product.get(field), str):
            raise SemanticProviderError(f"product.{field} must be a string")
    for field in ("label", "state_before", "state_after"):
        if not isinstance(action.get(field), str):
            raise SemanticProviderError(f"action.{field} must be a string")
    if not action["label"].strip():
        raise SemanticProviderError("action.label must be non-empty")

    for field in ("emotion",):
        if not isinstance(people.get(field), str):
            raise SemanticProviderError(f"people.{field} must be a string")
    for field in ("good_entry", "good_exit"):
        if not isinstance(transition.get(field), bool):
            raise SemanticProviderError(f"transition_fitness.{field} must be boolean")

    ocr_text = payload.get("ocr_text", "")
    if not isinstance(ocr_text, str):
        raise SemanticProviderError("ocr_text must be a string")

    result = {
        "description": description.strip()[:1000],
        "semantic_tags": _as_string_list(payload.get("semantic_tags"), "semantic_tags", SEMANTIC_TAGS, min_items=1),
        "people": {
            "visible": people["visible"],
            "identity_labels": _as_string_list(people.get("identity_labels", []), "people.identity_labels"),
            "clothing_labels": _as_string_list(people.get("clothing_labels", []), "people.clothing_labels"),
            "emotion": people["emotion"].strip()[:100],
        },
        "product": {
            "visible": product["visible"],
            "identity_label": product["identity_label"].strip()[:200],
            "visibility_score": _unit_score(product.get("visibility_score"), "product.visibility_score"),
            "state_before": product["state_before"].strip()[:300],
            "state_after": product["state_after"].strip()[:300],
        },
        "action": {
            "label": action["label"].strip()[:200],
            "phase": phase,
            "state_before": action["state_before"].strip()[:300],
            "state_after": action["state_after"].strip()[:300],
            "direction": direction,
        },
        "composition": {
            "shot_size": shot_size,
            "subject_bbox": normalized_bbox,
            "safe_caption_regions": _as_string_list(
                composition.get("safe_caption_regions", []),
                "composition.safe_caption_regions",
                CAPTION_REGIONS,
            ),
        },
        "transition_fitness": {
            "good_entry": transition["good_entry"],
            "good_exit": transition["good_exit"],
            "notes": str(transition.get("notes") or "").strip()[:500],
        },
        "camera_motion": camera_motion,
        "ocr_text": ocr_text.strip()[:5000],
        "model_confidence": _unit_score(payload.get("model_confidence"), "model_confidence"),
    }
    return result


def validate_source_review_payload(payload: Any, expected_shot_ids: Sequence[str]) -> dict[str, Any]:
    """Validate source-level AI revisions without making semantic judgments in code."""
    if not isinstance(payload, dict):
        raise SemanticProviderError("source review response must be a JSON object")
    _check_fields(payload, "source review", required={"shots"})
    items = payload.get("shots")
    if not isinstance(items, list):
        raise SemanticProviderError("source review shots must be an array")

    expected = list(expected_shot_ids)
    if len(expected) != len(set(expected)):
        raise SemanticProviderError("expected source shot ids must be unique")
    seen: set[str] = set()
    normalized_items: list[dict[str, Any]] = []
    required = {
        "shot_id", "description", "semantic_tags", "people", "product", "action",
        "model_confidence", "continuity",
    }
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SemanticProviderError(f"source review shots[{index}] must be an object")
        _check_fields(item, f"source review shots[{index}]", required=required)
        shot_id = item.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise SemanticProviderError(f"source review shots[{index}].shot_id must be a non-empty string")
        shot_id = shot_id.strip()
        if shot_id in seen:
            raise SemanticProviderError(f"source review contains duplicate shot_id: {shot_id}")
        seen.add(shot_id)

        semantic = validate_semantic_payload(
            {
                "description": item["description"],
                "semantic_tags": item["semantic_tags"],
                "people": item["people"],
                "product": item["product"],
                "action": item["action"],
                "composition": {
                    "shot_size": "mixed",
                    "subject_bbox": [0, 0, 1, 1],
                    "safe_caption_regions": [],
                },
                "transition_fitness": {"good_entry": True, "good_exit": True, "notes": ""},
                "camera_motion": "mixed",
                "ocr_text": "",
                "model_confidence": item["model_confidence"],
            }
        )
        continuity = item.get("continuity")
        if not isinstance(continuity, list):
            raise SemanticProviderError(f"source review {shot_id}.continuity must be an array")
        normalized_continuity: list[dict[str, Any]] = []
        relation_keys: set[tuple[str, str]] = set()
        for relation_index, relation in enumerate(continuity):
            field = f"source review {shot_id}.continuity[{relation_index}]"
            if not isinstance(relation, dict):
                raise SemanticProviderError(f"{field} must be an object")
            _check_fields(
                relation,
                field,
                required={"other_shot_id", "relation", "score"},
                optional={"reason"},
            )
            other_shot_id = relation.get("other_shot_id")
            relation_name = relation.get("relation")
            if not isinstance(other_shot_id, str) or not other_shot_id.strip():
                raise SemanticProviderError(f"{field}.other_shot_id must be a non-empty string")
            other_shot_id = other_shot_id.strip()
            if other_shot_id == shot_id:
                raise SemanticProviderError(f"{field} cannot reference its own shot")
            if relation_name not in CONTINUITY_RELATIONS:
                raise SemanticProviderError(f"{field}.relation contains unsupported value: {relation_name}")
            key = (other_shot_id, relation_name)
            if key in relation_keys:
                raise SemanticProviderError(f"{field} duplicates an existing continuity relation")
            relation_keys.add(key)
            reason = relation.get("reason", "")
            if not isinstance(reason, str):
                raise SemanticProviderError(f"{field}.reason must be a string")
            normalized_relation = {
                "other_shot_id": other_shot_id,
                "relation": relation_name,
                "score": _unit_score(relation.get("score"), f"{field}.score"),
            }
            if reason.strip():
                normalized_relation["reason"] = reason.strip()[:500]
            normalized_continuity.append(normalized_relation)

        normalized_items.append(
            {
                "shot_id": shot_id,
                "description": semantic["description"],
                "semantic_tags": semantic["semantic_tags"],
                "people": semantic["people"],
                "product": semantic["product"],
                "action": semantic["action"],
                "model_confidence": semantic["model_confidence"],
                "continuity": normalized_continuity,
            }
        )

    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        unexpected = sorted(seen - set(expected))
        raise SemanticProviderError(
            f"source review shot_id set mismatch; missing={missing}, unexpected={unexpected}"
        )
    valid_ids = set(expected)
    for item in normalized_items:
        for relation in item["continuity"]:
            if relation["other_shot_id"] not in valid_ids:
                raise SemanticProviderError(
                    f"source review {item['shot_id']} continuity references unknown shot: {relation['other_shot_id']}"
                )
    by_id = {item["shot_id"]: item for item in normalized_items}
    return {"shots": [by_id[shot_id] for shot_id in expected]}


def validate_source_review_quality(
    review: dict[str, Any],
    shot_contexts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Reject broad semantic drift and mechanically generated continuity graphs."""
    contexts = {str(shot["shot_id"]): shot for shot in shot_contexts}
    revisions = {str(shot["shot_id"]): shot for shot in review["shots"]}
    if set(contexts) != set(revisions):
        raise SemanticProviderError("source review quality check requires the same shot IDs")

    drift_limit = max(1, len(contexts) // 3)
    tag_drift = sum(
        set(revisions[shot_id]["semantic_tags"]) != set(contexts[shot_id]["semantic_tags"])
        for shot_id in contexts
    )
    if tag_drift > drift_limit:
        raise SemanticProviderError(
            f"source review changed semantic tags for {tag_drift}/{len(contexts)} shots; limit is {drift_limit}"
        )
    phase_drift = sum(
        revisions[shot_id]["action"]["phase"] != contexts[shot_id]["action"]["phase"]
        for shot_id in contexts
    )
    if phase_drift > drift_limit:
        raise SemanticProviderError(
            f"source review changed action phase for {phase_drift}/{len(contexts)} shots; limit is {drift_limit}"
        )

    relations = {
        (shot["shot_id"], relation["other_shot_id"], relation["relation"])
        for shot in review["shots"]
        for relation in shot["continuity"]
    }
    inverse_relations = {
        "good_before": "good_after",
        "good_after": "good_before",
        "must_precede": "must_follow",
        "must_follow": "must_precede",
    }
    for shot_id, other_shot_id, relation in relations:
        inverse = inverse_relations.get(relation)
        if inverse and (other_shot_id, shot_id, inverse) in relations:
            raise SemanticProviderError(
                f"source review contains redundant reciprocal continuity for {shot_id} and {other_shot_id}"
            )

    ordered_ids = [str(shot["shot_id"]) for shot in shot_contexts]
    adjacent_pairs = {
        frozenset((ordered_ids[index], ordered_ids[index + 1]))
        for index in range(len(ordered_ids) - 1)
    }
    weak_pairs = {
        frozenset((shot_id, other_shot_id))
        for shot_id, other_shot_id, relation in relations
        if relation in {"good_before", "good_after"}
    }
    if len(adjacent_pairs) >= 3 and adjacent_pairs.issubset(weak_pairs):
        raise SemanticProviderError("source review created a complete weak relation chain from adjacent shots")
    return review


SYSTEM_PROMPT = """You are a forensic shot-card annotator for short-form product ads.
Analyze only the supplied frames from one continuous source time range. Return one JSON object and no prose.
Never infer speech, identities, chronology outside the time range, or product claims that are not visually supported.
Use conservative empty strings/lists and a low model_confidence when uncertain. The semantic_tags field must use one or
more of: unboxing, product_macro, multiple_variants, photo_selection, text_customization, wearing_action,
wearing_result, touching_memory, gift_box, daily_life, emotional_close. If none can be supported, fail by returning
{"error":"insufficient visual evidence"}; do not invent a tag.

Required JSON shape:
{
  "description": "literal visual description",
  "semantic_tags": ["product_macro"],
  "people": {"visible": true, "identity_labels": [], "clothing_labels": [], "emotion": "neutral"},
  "product": {"visible": true, "identity_label": "memorial pin", "visibility_score": 0.9,
              "state_before": "", "state_after": ""},
  "action": {"label": "show product", "phase": "result", "state_before": "", "state_after": "",
             "direction": "static"},
  "composition": {"shot_size": "close_up", "subject_bbox": [0.2,0.2,0.6,0.6],
                  "safe_caption_regions": ["top"]},
  "transition_fitness": {"good_entry": true, "good_exit": true, "notes": ""},
  "camera_motion": "static",
  "ocr_text": "only clearly legible text",
  "model_confidence": 0.8
}
Allowed action.phase: setup, begin, middle, complete, result, none.
Allowed action.direction: left, right, up, down, toward_camera, away_from_camera, mixed, static.
Allowed composition.shot_size: extreme_close_up, close_up, medium, wide, screen_capture, mixed.
Allowed safe-caption regions: top, upper_middle, center, lower_middle, bottom.
Allowed camera_motion: static, pan, tilt, zoom, push_in, pull_out, handheld, mixed."""


SOURCE_REVIEW_PROMPT = """You are the source-level visual continuity reviewer for short-form product ads.
Review every supplied shot from one source together, in chronological order. Each shot includes its first-pass
annotation, exact source time range, ASR text, OCR text, and one contact sheet. Each contact-sheet panel has the
shot_id and EARLY/MIDDLE/LATE visibly written above it. Never associate a panel with a different shot_id. Use the
three labelled panels to re-evaluate action phase and before/after state, and use neighboring shots only to resolve
genuine chronology or identity ambiguity. Keep uncertainty explicit with conservative labels and low confidence.
Do not invent facts and do not omit or add shot IDs.

Every description, semantic tag, person, product, and action must describe only the current shot's own frames.
Never copy a fact or semantic tag from another shot merely because it belongs to the same source or product story.
In particular, photo_selection requires visibly selecting/browsing a photo in this shot; text_customization requires
visibly typing/editing/previewing custom text in this shot; wearing_action requires visibly putting on or fastening
the product in this shot. A finished charm containing a photo or text is not by itself evidence of photo_selection
or text_customization. Preserve a first-pass tag unless the current shot's frames provide evidence to correct it.
Do not guess app names, identities, readable text, accessories, or facial details when they are unclear.

Return one JSON object and no prose: {"shots":[...]}. Each input shot must appear exactly once in the same order.
Each item must contain exactly: shot_id, description, semantic_tags, people, product, action, model_confidence,
continuity. The semantic field shapes and enums are the same as the first-pass annotation. continuity is an array
of {"other_shot_id":"...","relation":"good_before","score":0.8,"reason":"visual evidence"} using only shot IDs
from this source. Allowed relations: must_precede, must_follow, good_before, good_after, duplicate, conflict.
Use an empty continuity array when the video does not provide enough evidence for a relation. Do not create a
continuity relation merely because two shots are adjacent, and do not build a complete adjacent-shot chain.
duplicate means materially the same visual and action, not merely the same person, room, product, or emotion.
conflict means the two shots cannot be presented as one continuous event; different customization variants are
not automatically conflicts. Every relation reason must cite concrete visual state, object, action, or composition
evidence visible in both related shots. Report a relationship only once: prefer good_before or must_precede on the
earlier shot, and never also emit the reciprocal good_after or must_follow on the later shot."""


class OpenAICompatibleSemanticProvider:
    """Strict JSON adapter for MiniMax and OpenAI-compatible chat endpoints."""

    prompt_version = "shot-card-v1"
    source_review_prompt_version = "source-shot-review-v3"

    def __init__(self, api_key: str, base_url: str | None, model: str, provider_name: str) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise SemanticProviderError("the openai package is required for multimodal analysis") from exc
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self.name = provider_name
        self.model = model

    def analyze_shot(self, context: dict[str, Any], keyframes: Sequence[Path]) -> dict[str, Any]:
        if not keyframes:
            raise SemanticProviderError(f"{context.get('shot_id', 'shot')} has no keyframes")
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Annotate this source shot. The three images are ordered early, middle, late. "
                    f"Context: {json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
                ),
            }
        ]
        for frame in keyframes:
            content.append({"type": "image_url", "image_url": {"url": _data_url(frame), "detail": "low"}})

        last_validation_error: SemanticProviderError | None = None
        for attempt in range(1, 3):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=1400,
                )
            except Exception as exc:
                raise SemanticProviderError(
                    f"{self.name}/{self.model} request failed for {context.get('shot_id', 'shot')}: {type(exc).__name__}: {exc}"
                ) from exc

            try:
                raw = _message_text(response.choices[0].message.content)
                payload = _parse_json_object(raw)
                if payload.get("error"):
                    raise SemanticProviderError(
                        f"{self.name}/{self.model} could not annotate {context.get('shot_id', 'shot')}: {payload['error']}"
                    )
                return validate_semantic_payload(payload)
            except (AttributeError, IndexError, TypeError, SemanticProviderError) as exc:
                if isinstance(exc, SemanticProviderError) and "could not annotate" in str(exc):
                    raise
                last_validation_error = (
                    exc
                    if isinstance(exc, SemanticProviderError)
                    else SemanticProviderError(f"invalid response object: {type(exc).__name__}")
                )
                if attempt == 1:
                    LOGGER.warning(
                        "%s/%s returned invalid structured data for %s; requesting one bounded correction",
                        self.name,
                        self.model,
                        context.get("shot_id", "shot"),
                    )
                    content.append(
                        {
                            "type": "text",
                            "text": (
                                "Correction: the previous response was not one contract-valid JSON object. "
                                "Return the required JSON object only, with exactly the requested fields and allowed enum values."
                            ),
                        }
                    )

        raise SemanticProviderError(
            f"{self.name}/{self.model} returned invalid structured data twice for "
            f"{context.get('shot_id', 'shot')}: {last_validation_error}"
        )

    def review_source_shots(
        self,
        source_context: dict[str, Any],
        shot_contexts: Sequence[dict[str, Any]],
        shot_frames: dict[str, Sequence[Path]],
    ) -> dict[str, Any]:
        expected_ids = [str(shot.get("shot_id", "")) for shot in shot_contexts]
        if not expected_ids or any(not shot_id for shot_id in expected_ids):
            raise SemanticProviderError("source review requires one or more shots with shot_id")
        if set(shot_frames) != set(expected_ids):
            raise SemanticProviderError("source review frame IDs must exactly match the shots")
        for shot_id in expected_ids:
            if not shot_frames[shot_id]:
                raise SemanticProviderError(f"source review requires frames for {shot_id}")

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Jointly review this entire source. Source context: "
                    f"{json.dumps(source_context, ensure_ascii=False, separators=(',', ':'))}"
                ),
            }
        ]
        for shot in shot_contexts:
            shot_id = str(shot["shot_id"])
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"Chronological shot {shot_id}: "
                        f"{json.dumps(shot, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                }
            )
            content.append(
                {
                    "type": "text",
                    "text": f"One labelled EARLY/MIDDLE/LATE contact sheet for {shot_id}:",
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _contact_sheet_data_url(shot_id, shot_frames[shot_id]), "detail": "low"},
                }
            )

        last_validation_error: SemanticProviderError | None = None
        for attempt in range(1, 3):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SOURCE_REVIEW_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=max(2400, len(expected_ids) * 900),
                )
            except Exception as exc:
                raise SemanticProviderError(
                    f"{self.name}/{self.model} source review request failed for "
                    f"{source_context.get('asset_id', 'source')}: {type(exc).__name__}: {exc}"
                ) from exc

            try:
                raw = _message_text(response.choices[0].message.content)
                payload = _parse_json_object(raw)
                if payload.get("error"):
                    raise SemanticProviderError(
                        f"{self.name}/{self.model} could not review "
                        f"{source_context.get('asset_id', 'source')}: {payload['error']}"
                    )
                review = validate_source_review_payload(payload, expected_ids)
                return validate_source_review_quality(review, shot_contexts)
            except (AttributeError, IndexError, TypeError, SemanticProviderError) as exc:
                if isinstance(exc, SemanticProviderError) and "could not review" in str(exc):
                    raise
                last_validation_error = (
                    exc
                    if isinstance(exc, SemanticProviderError)
                    else SemanticProviderError(f"invalid source review response object: {type(exc).__name__}")
                )
                if attempt == 1:
                    LOGGER.warning(
                        "%s/%s returned invalid source review data for %s; requesting one bounded correction",
                        self.name,
                        self.model,
                        source_context.get("asset_id", "source"),
                    )
                    content.append(
                        {
                            "type": "text",
                            "text": (
                                "Correction: return one contract-valid JSON object containing every requested "
                                "shot_id exactly once, with no extra fields or prose. Resolve this validation error: "
                                f"{last_validation_error}"
                            ),
                        }
                    )

        raise SemanticProviderError(
            f"{self.name}/{self.model} returned invalid source review data twice for "
            f"{source_context.get('asset_id', 'source')}: {last_validation_error}"
        )
