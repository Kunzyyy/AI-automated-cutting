from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "video-worker"))

from director.pipeline import DirectorConfig, DirectorValidationError, _build_document, generate_edit_plan
from director.provider import (
    DIRECTOR_TOOL_NAME,
    DirectorProviderError,
    OpenAICompatibleDirectorProvider,
    _compact_footage_index,
    _director_tool,
    validate_director_payload,
)
from validator import EditPlanValidationError, all_candidates_valid, validate_edit_plan


SEMANTIC_TAGS = [
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
]


def make_footage_index() -> dict:
    sources = [
        {"asset_id": "source_001", "duration_seconds": 30.0},
        {"asset_id": "source_002", "duration_seconds": 30.0},
    ]
    shots = []
    for number in range(1, 11):
        source_number = 1 if number <= 5 else 2
        source_shot_number = number - 1 if source_number == 1 else number - 6
        start = float(source_shot_number * 3)
        shot_id = f"shot_{number:03d}"
        shots.append(
            {
                "shot_id": shot_id,
                "source_asset_id": f"source_{source_number:03d}",
                "start_seconds": start,
                "end_seconds": start + 3.0,
                "description": f"evidence for {shot_id} memorial pin detail",
                "semantic_tags": list(SEMANTIC_TAGS),
                "product": {
                    "identity_label": "memorial pin",
                    "state_before": "pin visible",
                    "state_after": "pin visible",
                },
                "action": {
                    "label": "show memorial pin",
                    "state_before": "pin visible",
                    "state_after": "pin visible",
                },
                "composition": {"shot_size": "close_up", "subject_bbox": [0.2, 0.2, 0.6, 0.6]},
                "quality": {"sharpness": 0.9},
                "motion": {"camera_motion": "static"},
                "transition_fitness": {"good_entry": True, "good_exit": True},
                "evidence": {"ocr_text": "", "speech_text": ""},
                "continuity": [],
            }
        )
    shots[0]["continuity"] = [{"other_shot_id": "shot_002", "relation": "must_precede"}]
    return {
        "schema_version": "1.0.0",
        "job_id": "director-test-job",
        "sources": sources,
        "shots": shots,
    }


def make_clip(footage: dict, shot_number: int, role: str) -> dict:
    shot = footage["shots"][shot_number - 1]
    return {
        "shot_id": shot["shot_id"],
        "source_asset_id": shot["source_asset_id"],
        "source_in_seconds": shot["start_seconds"],
        "source_out_seconds": shot["end_seconds"],
        "role": role,
        "reason": f"Use visible evidence from {shot['shot_id']}",
        "caption_intent": "Describe only what is visible",
        "caption_evidence": f"evidence for {shot['shot_id']}",
        "crop_strategy": "track_subject",
        "transition": {"type": "cut", "duration_seconds": 0},
    }


def make_candidate(footage: dict, narrative: str, shot_numbers: list[int], roles: list[str]) -> dict:
    return {
        "narrative": narrative,
        "timeline": [make_clip(footage, number, role) for number, role in zip(shot_numbers, roles)],
        "music_direction": {
            "mood": "warm",
            "energy_curve": "gentle_rise",
            "vocal_policy": "instrumental_only",
            "beat_sync": True,
            "search_keywords": ["warm", "memory"],
        },
        "caption_direction": {
            "tone": "warm",
            "language": "zh-CN",
            "max_lines": 2,
            "evidence_required": True,
            "global_message": "Keep every caption grounded in the selected Shot Card.",
        },
    }


def make_payload(footage: dict) -> dict:
    return {
        "candidates": [
            make_candidate(
                footage,
                "Finished product hook, process, detail, and wearing result",
                [1, 2, 3, 4, 5],
                ["product_macro", "unboxing", "text_customization", "product_macro", "wearing_result"],
            ),
            make_candidate(
                footage,
                "Emotional hook, gift process, detail proof, daily use, and close",
                [6, 7, 8, 9, 10],
                ["touching_memory", "gift_box", "product_macro", "daily_life", "emotional_close"],
            ),
        ]
    }


class FakeDirectorProvider:
    name = "fake"
    model = "fake-director"
    prompt_version = "test-director-v1"

    def __init__(self, responses: list[dict | Exception]) -> None:
        self.responses = [copy.deepcopy(response) for response in responses]
        self.calls: list[dict] = []

    def generate_candidates(
        self,
        footage_index,
        candidate_count,
        target_duration_seconds,
        *,
        previous_payload=None,
        validation_feedback=None,
    ):
        self.calls.append(
            {
                "candidate_count": candidate_count,
                "target_duration_seconds": target_duration_seconds,
                "previous_payload": copy.deepcopy(previous_payload),
                "validation_feedback": copy.deepcopy(validation_feedback),
            }
        )
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def build_plan(footage: dict, payload: dict) -> dict:
    return _build_document(
        payload,
        footage,
        Path("footage_index.json"),
        Path("edit_plan.json"),
        FakeDirectorProvider([payload]),
        DirectorConfig(),
    )


def error_codes(candidate: dict) -> set[str]:
    return {error["code"] for error in candidate["validation"]["errors"]}


class DirectorPipelineTests(unittest.TestCase):
    def test_two_valid_candidates_are_written_and_schema_valid(self):
        footage = make_footage_index()
        payload = make_payload(footage)
        provider = FakeDirectorProvider([payload])
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")

            result = generate_edit_plan(footage_path, output_path, provider=provider)

            self.assertTrue(output_path.is_file())
            self.assertEqual(result, json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual(len(result["candidates"]), 2)
            self.assertTrue(all_candidates_valid(result))
            self.assertEqual([candidate["status"] for candidate in result["candidates"]], ["validated", "validated"])
            self.assertEqual([clip["timeline_in_seconds"] for clip in result["candidates"][0]["timeline"]], [0, 3, 6, 9, 12])

    def test_existing_output_is_not_overwritten_or_sent_to_provider(self):
        footage = make_footage_index()
        payload = make_payload(footage)
        provider = FakeDirectorProvider([payload])
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")
            output_path.write_text("keep-existing-output", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                generate_edit_plan(footage_path, output_path, provider=provider)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep-existing-output")
            self.assertEqual(provider.calls, [])

    def test_first_invalid_plan_is_revised_once_then_written(self):
        footage = make_footage_index()
        valid_payload = make_payload(footage)
        invalid_payload = copy.deepcopy(valid_payload)
        invalid_payload["candidates"][0]["timeline"][0]["caption_evidence"] = "invented claim"
        provider = FakeDirectorProvider([invalid_payload, valid_payload])
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")

            result = generate_edit_plan(footage_path, output_path, provider=provider)

            self.assertTrue(all_candidates_valid(result))
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(provider.calls[1]["previous_payload"], invalid_payload)
            feedback_codes = {
                error["code"]
                for candidate in provider.calls[1]["validation_feedback"]
                for error in candidate["errors"]
            }
            self.assertIn("CAPTION_EVIDENCE_NOT_FOUND", feedback_codes)

    def test_structurally_invalid_response_uses_the_same_single_revision_budget(self):
        footage = make_footage_index()
        valid_payload = make_payload(footage)
        provider = FakeDirectorProvider([DirectorProviderError("response is not JSON"), valid_payload])
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")

            result = generate_edit_plan(footage_path, output_path, provider=provider)

            self.assertTrue(all_candidates_valid(result))
            self.assertEqual(len(provider.calls), 2)
            self.assertIsNone(provider.calls[1]["previous_payload"])
            self.assertEqual(
                provider.calls[1]["validation_feedback"][0]["errors"][0]["code"],
                "DIRECTOR_PAYLOAD_INVALID",
            )

    def test_two_structurally_invalid_responses_report_safe_reason_without_output(self):
        footage = make_footage_index()
        provider = FakeDirectorProvider(
            [
                DirectorProviderError("response is not a single JSON object"),
                DirectorProviderError("candidate 2 contains unexpected fields: unsupported"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")

            with self.assertRaises(DirectorValidationError) as raised:
                generate_edit_plan(footage_path, output_path, provider=provider)

            self.assertIn("candidate 2 contains unexpected fields", str(raised.exception))
            self.assertFalse(output_path.exists())

    def test_second_structural_failure_keeps_first_plan_validation_codes(self):
        footage = make_footage_index()
        invalid_payload = make_payload(footage)
        invalid_payload["candidates"][0]["timeline"][0]["caption_evidence"] = "invented claim"
        provider = FakeDirectorProvider(
            [invalid_payload, DirectorProviderError("candidate 1 clip 2 source_out_seconds must be numeric")]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")

            with self.assertRaises(DirectorValidationError) as raised:
                generate_edit_plan(footage_path, output_path, provider=provider)

            message = str(raised.exception)
            self.assertIn("source_out_seconds must be numeric", message)
            self.assertIn("CAPTION_EVIDENCE_NOT_FOUND", message)
            self.assertFalse(output_path.exists())

    def test_two_invalid_plans_do_not_write_final_or_temporary_file(self):
        footage = make_footage_index()
        invalid_payload = make_payload(footage)
        invalid_payload["candidates"][0]["timeline"][0]["caption_evidence"] = "invented claim"
        provider = FakeDirectorProvider([invalid_payload, invalid_payload])
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            footage_path = temporary / "footage_index.json"
            output_path = temporary / "edit_plan.json"
            footage_path.write_text(json.dumps(footage), encoding="utf-8")

            with self.assertRaises(DirectorValidationError):
                generate_edit_plan(footage_path, output_path, provider=provider)

            self.assertEqual(len(provider.calls), 2)
            self.assertFalse(output_path.exists())
            self.assertFalse(output_path.with_suffix(".json.tmp").exists())


class EditPlanValidatorTests(unittest.TestCase):
    def setUp(self):
        self.footage = make_footage_index()
        self.payload = make_payload(self.footage)

    def validate(self, plan: dict) -> dict:
        return validate_edit_plan(plan, self.footage)

    def test_unknown_shot_and_source_are_rejected(self):
        plan = build_plan(self.footage, self.payload)
        clip = plan["candidates"][0]["timeline"][0]
        clip["shot_id"] = "shot_missing"
        clip["source_asset_id"] = "source_missing"

        result = self.validate(plan)

        codes = error_codes(result["candidates"][0])
        self.assertIn("SHOT_NOT_FOUND", codes)
        self.assertIn("SOURCE_NOT_FOUND", codes)

    def test_out_of_range_and_duration_mismatch_are_rejected(self):
        plan = build_plan(self.footage, self.payload)
        clip = plan["candidates"][0]["timeline"][0]
        clip["source_out_seconds"] = 6.0

        result = self.validate(plan)

        codes = error_codes(result["candidates"][0])
        self.assertIn("CLIP_DURATION_MISMATCH", codes)
        self.assertIn("CLIP_OUTSIDE_SHOT", codes)

    def test_duplicate_shot_is_rejected(self):
        plan = build_plan(self.footage, self.payload)
        first, second = plan["candidates"][0]["timeline"][:2]
        second["shot_id"] = first["shot_id"]
        second["source_asset_id"] = first["source_asset_id"]
        second["source_in_seconds"] = first["source_in_seconds"]
        second["source_out_seconds"] = first["source_out_seconds"]
        second["caption_evidence"] = first["caption_evidence"]

        result = self.validate(plan)

        self.assertIn("DUPLICATE_SHOT", error_codes(result["candidates"][0]))

    def test_must_precede_relation_cannot_be_reversed(self):
        payload = copy.deepcopy(self.payload)
        payload["candidates"][0]["timeline"][:2] = reversed(payload["candidates"][0]["timeline"][:2])
        plan = build_plan(self.footage, payload)

        result = self.validate(plan)

        self.assertIn("MUST_PRECEDE_VIOLATION", error_codes(result["candidates"][0]))

    def test_product_customization_and_wearing_roles_are_required(self):
        payload = copy.deepcopy(self.payload)
        for clip in payload["candidates"][0]["timeline"]:
            clip["role"] = "touching_memory"
        plan = build_plan(self.footage, payload)

        result = self.validate(plan)

        codes = error_codes(result["candidates"][0])
        self.assertIn("PRODUCT_MACRO_REQUIRED", codes)
        self.assertIn("CUSTOMIZATION_REQUIRED", codes)
        self.assertIn("WEARING_RESULT_REQUIRED", codes)

    def test_caption_evidence_must_be_exact_shot_card_text(self):
        plan = build_plan(self.footage, self.payload)
        plan["candidates"][0]["timeline"][0]["caption_evidence"] = "unsupported discount claim"

        result = self.validate(plan)

        self.assertIn("CAPTION_EVIDENCE_NOT_FOUND", error_codes(result["candidates"][0]))

    def test_role_must_be_supported_by_the_shot_card(self):
        plan = build_plan(self.footage, self.payload)
        self.footage["shots"][0]["semantic_tags"] = ["touching_memory"]

        result = self.validate(plan)

        self.assertIn("ROLE_NOT_SUPPORTED", error_codes(result["candidates"][0]))

    def test_malformed_numeric_contract_is_reported_as_validation_error(self):
        plan = build_plan(self.footage, self.payload)
        plan["candidates"][0]["timeline"][0]["timeline_in_seconds"] = "not-a-number"

        with self.assertRaises(EditPlanValidationError):
            self.validate(plan)

    def test_two_candidates_must_have_distinct_narratives_and_sequences(self):
        payload = copy.deepcopy(self.payload)
        payload["candidates"][1] = copy.deepcopy(payload["candidates"][0])
        plan = build_plan(self.footage, payload)

        result = self.validate(plan)

        self.assertFalse(all_candidates_valid(result))
        for candidate in result["candidates"]:
            self.assertIn("CANDIDATES_NOT_DISTINCT", error_codes(candidate))


class DirectorProviderValidationTests(unittest.TestCase):
    def test_sdk_automatic_retries_are_disabled(self):
        with patch("openai.OpenAI") as client_factory:
            OpenAICompatibleDirectorProvider("fake-key", "https://example.invalid/v1", "fake-model", "fake")

        client_factory.assert_called_once_with(
            api_key="fake-key",
            base_url="https://example.invalid/v1",
            max_retries=0,
        )

    def test_numeric_bounds_are_rejected_before_plan_building(self):
        footage = make_footage_index()
        mutations = (
            ("negative source time", lambda clip: clip.__setitem__("source_in_seconds", -1)),
            ("backwards source time", lambda clip: clip.__setitem__("source_out_seconds", clip["source_in_seconds"])),
            ("invalid speed", lambda clip: clip.__setitem__("speed", 3)),
            ("invalid transition duration", lambda clip: clip["transition"].__setitem__("duration_seconds", "slow")),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                payload = make_payload(footage)
                mutate(payload["candidates"][0]["timeline"][0])
                with self.assertRaises(DirectorProviderError):
                    validate_director_payload(payload, 2)

    def test_compact_prompt_omits_local_uri_and_keeps_crop_evidence(self):
        footage = make_footage_index()
        footage["sources"][0]["uri"] = "file:///private/local/material.mp4"

        compact = _compact_footage_index(footage)

        self.assertNotIn("uri", compact["sources"][0])
        self.assertEqual(compact["shots"][0]["composition"], footage["shots"][0]["composition"])
        self.assertEqual(compact["shots"][0]["motion"], footage["shots"][0]["motion"])

    def test_network_exception_text_is_not_copied_into_error_or_logs(self):
        secret_marker = "MINIMAX_API_KEY=do-not-log-this"

        def fail_request(**_kwargs):
            raise RuntimeError(secret_marker)

        provider = object.__new__(OpenAICompatibleDirectorProvider)
        provider.name = "fake"
        provider.model = "fake-model"
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fail_request))
        )

        with self.assertRaises(DirectorProviderError) as raised:
            provider.generate_candidates(make_footage_index(), 2, 15.0)

        self.assertIn("RuntimeError", str(raised.exception))
        self.assertNotIn(secret_marker, str(raised.exception))

    def test_minimax_uses_official_tool_arguments_instead_of_response_format(self):
        footage = make_footage_index()
        payload = make_payload(footage)
        captured_request = {}

        def return_tool_call(**kwargs):
            captured_request.update(kwargs)
            function = SimpleNamespace(name=DIRECTOR_TOOL_NAME, arguments=json.dumps(payload))
            message = SimpleNamespace(tool_calls=[SimpleNamespace(function=function)], content="ignored")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        provider = object.__new__(OpenAICompatibleDirectorProvider)
        provider.name = "minimax"
        provider.model = "MiniMax-M3"
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=return_tool_call))
        )

        result = provider.generate_candidates(footage, 2, 15.0)

        self.assertEqual(result, payload)
        self.assertNotIn("response_format", captured_request)
        self.assertIn("tools", captured_request)
        self.assertEqual(captured_request["max_completion_tokens"], 7000)
        self.assertEqual(
            captured_request["extra_body"],
            {"reasoning_split": True, "thinking": {"type": "adaptive"}},
        )
        instruction = captured_request["messages"][1]["content"][-1]["text"]
        self.assertIn("between 13 and 17 seconds", instruction)

    def test_minimax_tool_schema_binds_each_shot_range_source_and_roles(self):
        footage = make_footage_index()
        footage["shots"][0]["semantic_tags"] = ["product_macro", "touching_memory"]

        tool = _director_tool(footage)
        options = tool["function"]["parameters"]["properties"]["candidates"]["items"]["properties"][
            "timeline"
        ]["items"]["oneOf"]
        first = options[0]["properties"]

        self.assertEqual(len(options), len(footage["shots"]))
        self.assertEqual(first["shot_id"]["const"], "shot_001")
        self.assertEqual(first["source_asset_id"]["const"], "source_001")
        self.assertEqual(first["source_in_seconds"]["minimum"], 0.0)
        self.assertEqual(first["source_out_seconds"]["maximum"], 3.0)
        self.assertEqual(first["role"]["enum"], ["product_macro", "touching_memory"])

    def test_minimax_rejects_missing_tool_call_without_parsing_content(self):
        footage = make_footage_index()

        def return_content_only(**_kwargs):
            message = SimpleNamespace(tool_calls=None, content=json.dumps(make_payload(footage)))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        provider = object.__new__(OpenAICompatibleDirectorProvider)
        provider.name = "minimax"
        provider.model = "MiniMax-M3"
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=return_content_only))
        )

        with self.assertRaisesRegex(DirectorProviderError, "exactly one tool call"):
            provider.generate_candidates(footage, 2, 15.0)


if __name__ == "__main__":
    unittest.main()
