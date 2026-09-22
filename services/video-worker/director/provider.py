"""Strict text-model provider for evidence-grounded Edit Plan candidates."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any, Protocol


LOGGER = logging.getLogger(__name__)
SEMANTIC_TAGS = {
    "unboxing", "product_macro", "multiple_variants", "photo_selection", "text_customization",
    "wearing_action", "wearing_result", "touching_memory", "gift_box", "daily_life", "emotional_close",
    "product_detail", "product_display", "usage_demo", "usage_result", "packaging",
}
ENERGY_CURVES = {"flat", "gentle_rise", "fast_rise", "rise_and_fall", "sectional"}
CROP_STRATEGIES = {"fit", "fill_center", "track_subject", "blur_background"}
TRANSITION_TYPES = {"cut", "crossfade", "fade", "dip_to_black"}


class DirectorProviderError(RuntimeError):
    """Raised when the Director provider cannot return trustworthy candidates."""


class DirectorProvider(Protocol):
    name: str
    model: str
    prompt_version: str

    def generate_candidates(
        self,
        footage_index: dict[str, Any],
        candidate_count: int,
        target_duration_seconds: float,
        *,
        previous_payload: dict[str, Any] | None = None,
        validation_feedback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return a strict provider payload containing candidate decisions."""


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


def create_director_provider(provider: str = "auto", model: str | None = None) -> "OpenAICompatibleDirectorProvider":
    selected = provider.casefold()
    if selected not in {"auto", "minimax", "openai"}:
        raise DirectorProviderError(f"unsupported Director provider: {provider}")
    minimax_key = _credential("MINIMAX_API_KEY")
    minimax_url = _credential("MINIMAX_BASE_URL")
    openai_key = _credential("OPENAI_API_KEY")
    if selected == "auto":
        selected = "minimax" if minimax_key and minimax_url else "openai"
    if selected == "minimax":
        if not minimax_key or not minimax_url:
            raise DirectorProviderError("MiniMax is not configured")
        return OpenAICompatibleDirectorProvider(
            minimax_key,
            minimax_url,
            model or _credential("MINIMAX_MODEL") or "MiniMax-M3",
            "minimax",
        )
    if not openai_key:
        raise DirectorProviderError("OpenAI is not configured")
    return OpenAICompatibleDirectorProvider(
        openai_key,
        None,
        model or _credential("OPENAI_TEXT_MODEL") or "gpt-4o",
        "openai",
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(getattr(item, "text", ""))
            for item in content
        )
    return str(content or "")


def _parse_json_object(raw: str) -> dict[str, Any]:
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
        raise DirectorProviderError("Director response is not a single JSON object") from exc
    if not isinstance(payload, dict):
        raise DirectorProviderError("Director response must be a JSON object")
    return payload


def _check_fields(value: dict[str, Any], field: str, required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    unexpected = sorted(set(value) - required - optional)
    if missing:
        raise DirectorProviderError(f"{field} missing fields: {', '.join(missing)}")
    if unexpected:
        raise DirectorProviderError(f"{field} contains unexpected fields: {', '.join(unexpected)}")


def validate_director_payload(payload: Any, candidate_count: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DirectorProviderError("Director payload must be an object")
    _check_fields(payload, "Director payload", {"candidates"})
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != candidate_count:
        raise DirectorProviderError(f"Director must return exactly {candidate_count} candidate(s)")
    for candidate_index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise DirectorProviderError(f"candidate {candidate_index} must be an object")
        _check_fields(
            candidate,
            f"candidate {candidate_index}",
            {"narrative", "timeline", "music_direction", "caption_direction"},
        )
        if not isinstance(candidate["narrative"], str) or not candidate["narrative"].strip():
            raise DirectorProviderError(f"candidate {candidate_index} narrative must be non-empty")
        if not isinstance(candidate["timeline"], list) or not candidate["timeline"]:
            raise DirectorProviderError(f"candidate {candidate_index} timeline must be non-empty")
        for clip_index, clip in enumerate(candidate["timeline"], start=1):
            if not isinstance(clip, dict):
                raise DirectorProviderError(f"candidate {candidate_index} clip {clip_index} must be an object")
            _check_fields(
                clip,
                f"candidate {candidate_index} clip {clip_index}",
                {
                    "shot_id", "source_asset_id", "source_in_seconds", "source_out_seconds", "role",
                    "reason", "caption_intent", "caption_evidence", "crop_strategy", "transition",
                },
                {"speed"},
            )
            if clip["role"] not in SEMANTIC_TAGS:
                raise DirectorProviderError(f"unsupported clip role: {clip['role']}")
            for field in ("shot_id", "source_asset_id", "reason", "caption_intent", "caption_evidence"):
                if not isinstance(clip[field], str) or not clip[field].strip():
                    raise DirectorProviderError(f"candidate {candidate_index} clip {clip_index} {field} is empty")
            for field in ("source_in_seconds", "source_out_seconds"):
                if isinstance(clip[field], bool) or not isinstance(clip[field], (int, float)):
                    raise DirectorProviderError(f"candidate {candidate_index} clip {clip_index} {field} must be numeric")
            source_in = float(clip["source_in_seconds"])
            source_out = float(clip["source_out_seconds"])
            if source_in < 0 or source_out <= source_in:
                raise DirectorProviderError(
                    f"candidate {candidate_index} clip {clip_index} must have a non-negative increasing source range"
                )
            if "speed" in clip and (
                isinstance(clip["speed"], bool)
                or not isinstance(clip["speed"], (int, float))
                or not 0.5 <= float(clip["speed"]) <= 2.0
            ):
                raise DirectorProviderError(f"candidate {candidate_index} clip {clip_index} speed is outside 0.5-2.0")
            if clip["crop_strategy"] not in CROP_STRATEGIES:
                raise DirectorProviderError("unsupported crop_strategy")
            transition = clip["transition"]
            if not isinstance(transition, dict):
                raise DirectorProviderError("transition must be an object")
            _check_fields(transition, "transition", {"type", "duration_seconds"})
            if transition["type"] not in TRANSITION_TYPES:
                raise DirectorProviderError("unsupported transition type")
            transition_duration = transition["duration_seconds"]
            if (
                isinstance(transition_duration, bool)
                or not isinstance(transition_duration, (int, float))
                or not 0 <= float(transition_duration) <= 2.0
            ):
                raise DirectorProviderError("transition duration_seconds must be numeric and between 0 and 2")
        music = candidate["music_direction"]
        captions = candidate["caption_direction"]
        if not isinstance(music, dict) or not isinstance(captions, dict):
            raise DirectorProviderError("music_direction and caption_direction must be objects")
        _check_fields(music, "music_direction", {"mood", "energy_curve", "vocal_policy", "beat_sync"}, {"search_keywords"})
        _check_fields(captions, "caption_direction", {"tone", "language", "max_lines", "evidence_required"}, {"global_message"})
        if not isinstance(music["mood"], str) or not music["mood"].strip():
            raise DirectorProviderError("music mood must be non-empty")
        if music["energy_curve"] not in ENERGY_CURVES:
            raise DirectorProviderError("unsupported music energy_curve")
        if music.get("vocal_policy") != "instrumental_only":
            raise DirectorProviderError("music vocal_policy must be instrumental_only")
        if not isinstance(music["beat_sync"], bool):
            raise DirectorProviderError("music beat_sync must be boolean")
        if "search_keywords" in music and (
            not isinstance(music["search_keywords"], list)
            or any(not isinstance(item, str) for item in music["search_keywords"])
            or len(music["search_keywords"]) != len(set(music["search_keywords"]))
        ):
            raise DirectorProviderError("music search_keywords must be a unique string array")
        if not isinstance(captions["tone"], str) or not captions["tone"].strip():
            raise DirectorProviderError("caption tone must be non-empty")
        if not isinstance(captions["language"], str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", captions["language"]):
            raise DirectorProviderError("caption language must be a language tag such as zh-CN")
        if isinstance(captions["max_lines"], bool) or not isinstance(captions["max_lines"], int) or not 1 <= captions["max_lines"] <= 3:
            raise DirectorProviderError("caption max_lines must be an integer from 1 to 3")
        if captions.get("evidence_required") is not True:
            raise DirectorProviderError("caption evidence_required must be true")
    return payload


SYSTEM_PROMPT = """You are the AI Director for a 15-second vertical memorial-product advertisement.
Select and order clips only from the supplied Shot Cards. Return exactly the requested number of candidates as one
JSON object and no prose. Never invent a source, shot, visual fact, product claim, readable text, identity, discount,
material, shipping promise, or feature. A clip role must be one of that Shot Card's semantic_tags.

Candidate 1 narrative: finished product hook -> unboxing -> customization or memorial text/photo detail -> product
detail -> wearing/use result. Candidate 2 narrative: emotion or memorial meaning hook -> making/unboxing process ->
detail proof -> wearing result -> complete product close. The candidates must have meaningfully different shot
sequences, not merely different captions.

The body target is about 15 seconds and excludes the fixed trailer, which Renderer adds later. Avoid repeated shots,
respect must_precede/must_follow/conflict/duplicate relations, and keep action states understandable. Product should
appear early. Every candidate must include product_macro, customization or a core selling-point role, and
wearing_result or daily_life.

caption_evidence must be a short exact substring copied verbatim from that Shot Card's description, OCR text,
speech text, product identity/state, or action label/state. caption_intent may be creative but cannot claim more than
the exact evidence supports. Use cut with duration_seconds 0 unless a clearly justified short transition is needed.
All source_in_seconds, source_out_seconds, transition duration_seconds, and optional speed values must be JSON
numbers, never quoted strings and never values with unit suffixes. Every function argument must match its JSON type.

Required JSON shape:
{"candidates":[{"narrative":"...","timeline":[{"shot_id":"...","source_asset_id":"...",
"source_in_seconds":0.0,"source_out_seconds":2.0,"role":"product_macro","reason":"...",
"caption_intent":"...","caption_evidence":"exact source substring","crop_strategy":"track_subject",
"transition":{"type":"cut","duration_seconds":0}}],"music_direction":{"mood":"warm",
"energy_curve":"gentle_rise","vocal_policy":"instrumental_only","beat_sync":true,
"search_keywords":["warm","memory"]},"caption_direction":{"tone":"warm","language":"zh-CN",
"max_lines":2,"evidence_required":true,"global_message":"..."}}]}"""


DIRECTOR_TOOL_NAME = "submit_edit_plan_candidates"
DIRECTOR_TOOL = {
    "type": "function",
    "function": {
        "name": DIRECTOR_TOOL_NAME,
        "description": (
            "Submit the complete evidence-grounded Edit Plan candidate decisions. All time and speed values must "
            "be JSON numbers, not quoted strings or values with unit suffixes."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["narrative", "timeline", "music_direction", "caption_direction"],
                        "properties": {
                            "narrative": {"type": "string", "minLength": 1},
                            "timeline": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "shot_id", "source_asset_id", "source_in_seconds", "source_out_seconds",
                                        "role", "reason", "caption_intent", "caption_evidence", "crop_strategy",
                                        "transition",
                                    ],
                                    "properties": {
                                        "shot_id": {"type": "string", "minLength": 1},
                                        "source_asset_id": {"type": "string", "minLength": 1},
                                        "source_in_seconds": {"type": "number", "minimum": 0},
                                        "source_out_seconds": {"type": "number", "minimum": 0},
                                        "role": {"type": "string", "enum": sorted(SEMANTIC_TAGS)},
                                        "reason": {"type": "string", "minLength": 1},
                                        "caption_intent": {"type": "string", "minLength": 1},
                                        "caption_evidence": {"type": "string", "minLength": 1},
                                        "crop_strategy": {"type": "string", "enum": sorted(CROP_STRATEGIES)},
                                        "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
                                        "transition": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["type", "duration_seconds"],
                                            "properties": {
                                                "type": {"type": "string", "enum": sorted(TRANSITION_TYPES)},
                                                "duration_seconds": {"type": "number", "minimum": 0, "maximum": 2},
                                            },
                                        },
                                    },
                                },
                            },
                            "music_direction": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["mood", "energy_curve", "vocal_policy", "beat_sync"],
                                "properties": {
                                    "mood": {"type": "string", "minLength": 1},
                                    "energy_curve": {"type": "string", "enum": sorted(ENERGY_CURVES)},
                                    "vocal_policy": {"type": "string", "enum": ["instrumental_only"]},
                                    "beat_sync": {"type": "boolean"},
                                    "search_keywords": {
                                        "type": "array",
                                        "uniqueItems": True,
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                            "caption_direction": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["tone", "language", "max_lines", "evidence_required"],
                                "properties": {
                                    "tone": {"type": "string", "minLength": 1},
                                    "language": {"type": "string"},
                                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 3},
                                    "evidence_required": {"type": "boolean", "enum": [True]},
                                    "global_message": {"type": "string"},
                                },
                            },
                        },
                    },
                }
            },
        },
    },
}


def _director_tool(footage_index: dict[str, Any]) -> dict[str, Any]:
    """Bind tool clip choices to exact Shot Card IDs, ranges, sources, and roles."""
    tool = copy.deepcopy(DIRECTOR_TOOL)
    timeline_items = tool["function"]["parameters"]["properties"]["candidates"]["items"]["properties"][
        "timeline"
    ]["items"]
    shot_options: list[dict[str, Any]] = []
    for shot in footage_index.get("shots", []):
        option = copy.deepcopy(timeline_items)
        start = float(shot["start_seconds"])
        end = float(shot["end_seconds"])
        supported_roles = sorted(set(shot.get("semantic_tags") or []).intersection(SEMANTIC_TAGS))
        option["properties"]["shot_id"] = {"type": "string", "const": shot["shot_id"]}
        option["properties"]["source_asset_id"] = {
            "type": "string",
            "const": shot["source_asset_id"],
        }
        option["properties"]["source_in_seconds"] = {
            "type": "number",
            "minimum": start,
            "maximum": end,
        }
        option["properties"]["source_out_seconds"] = {
            "type": "number",
            "minimum": start,
            "maximum": end,
        }
        option["properties"]["role"] = {"type": "string", "enum": supported_roles}
        shot_options.append(option)
    timeline_items.clear()
    timeline_items["oneOf"] = shot_options
    return tool


def _compact_footage_index(footage_index: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": footage_index.get("job_id"),
        "sources": [
            {
                "asset_id": source.get("asset_id"),
                "duration_seconds": source.get("duration_seconds"),
                "width": source.get("width"),
                "height": source.get("height"),
                "fps": source.get("fps"),
                "has_audio": source.get("has_audio"),
            }
            for source in footage_index.get("sources", [])
        ],
        "shots": [
            {
                "shot_id": shot["shot_id"],
                "source_asset_id": shot["source_asset_id"],
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
                "description": shot["description"],
                "semantic_tags": shot["semantic_tags"],
                "product": shot["product"],
                "action": shot["action"],
                "composition": shot["composition"],
                "quality": shot["quality"],
                "motion": shot["motion"],
                "transition_fitness": shot["transition_fitness"],
                "continuity": shot.get("continuity", []),
                "ocr_text": shot["evidence"].get("ocr_text", ""),
                "speech_text": shot["evidence"].get("speech_text", ""),
            }
            for shot in footage_index.get("shots", [])
        ],
    }


class OpenAICompatibleDirectorProvider:
    prompt_version = "edit-plan-director-v1"

    def __init__(self, api_key: str, base_url: str | None, model: str, provider_name: str) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise DirectorProviderError("the openai package is required for AI Director") from exc
        # The pipeline owns the single correction budget. Disable SDK-level
        # retries so one logical attempt cannot silently become several paid calls.
        kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self.name = provider_name
        self.model = model

    def generate_candidates(
        self,
        footage_index: dict[str, Any],
        candidate_count: int,
        target_duration_seconds: float,
        *,
        previous_payload: dict[str, Any] | None = None,
        validation_feedback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        user_content: list[dict[str, str]] = [
            {
                "type": "text",
                "text": (
                    f"Generate exactly {candidate_count} candidate(s), each targeting {target_duration_seconds:.1f}s. "
                    "Footage Index: "
                    + json.dumps(_compact_footage_index(footage_index), ensure_ascii=False, separators=(",", ":"))
                ),
            }
        ]
        if validation_feedback:
            previous_context = (
                f"Previous payload: {json.dumps(previous_payload, ensure_ascii=False, separators=(',', ':'))}. "
                if previous_payload is not None
                else "The previous response was structurally invalid, so no payload is available. "
            )
            user_content.append(
                {
                    "type": "text",
                    "text": (
                        "This is the single allowed correction. Use only these validation errors. "
                        + previous_context
                        + f"Errors: {json.dumps(validation_feedback, ensure_ascii=False, separators=(',', ':'))}."
                    ),
                }
            )

        if self.name == "minimax":
            user_content.append(
                {
                    "type": "text",
                    "text": (
                        f"Call {DIRECTOR_TOOL_NAME} exactly once with the complete candidate payload. "
                        "Do not return the payload as ordinary text. Before calling it, calculate each candidate's "
                        "sum of (source_out_seconds - source_in_seconds), use enough distinct clips, and keep the "
                        "total between 13 and 17 seconds. Obey the selected Shot Card branch's exact source, time "
                        "bounds, and role enum."
                    ),
                }
            )

        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
            "temperature": 0,
        }
        if self.name == "minimax":
            request.update(
                {
                    "tools": [_director_tool(footage_index)],
                    "extra_body": {"reasoning_split": True, "thinking": {"type": "adaptive"}},
                    "max_completion_tokens": 7000,
                }
            )
        else:
            request.update({"response_format": {"type": "json_object"}, "max_tokens": 7000})

        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            status_suffix = f" (HTTP {status_code})" if isinstance(status_code, int) else ""
            raise DirectorProviderError(
                f"{self.name}/{self.model} Director request failed: {type(exc).__name__}{status_suffix}"
            ) from exc
        try:
            message = response.choices[0].message
            if self.name == "minimax":
                tool_calls = getattr(message, "tool_calls", None)
                if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                    raise DirectorProviderError("MiniMax Director must return exactly one tool call")
                function = getattr(tool_calls[0], "function", None)
                function_name = function.get("name") if isinstance(function, dict) else getattr(function, "name", None)
                if function_name != DIRECTOR_TOOL_NAME:
                    raise DirectorProviderError("MiniMax Director returned the wrong tool name")
                arguments = (
                    function.get("arguments") if isinstance(function, dict) else getattr(function, "arguments", None)
                )
                if not isinstance(arguments, str):
                    raise DirectorProviderError("MiniMax Director tool arguments must be a JSON string")
                raw = arguments
            else:
                raw = _message_text(message.content)
            return validate_director_payload(_parse_json_object(raw), candidate_count)
        except (AttributeError, IndexError, TypeError, DirectorProviderError) as exc:
            if isinstance(exc, DirectorProviderError):
                raise
            raise DirectorProviderError(f"Director response shape is invalid: {type(exc).__name__}") from exc
