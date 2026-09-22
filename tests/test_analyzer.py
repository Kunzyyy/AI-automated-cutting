from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "video-worker"))

from analyzer.frames import cache_shot_frames, sample_times
from analyzer.models import CachedFrames, SceneRange, VideoMetadata
from analyzer.metrics import measure_frames
from analyzer.cli import build_parser
from analyzer.pipeline import analyze_directory
from analyzer.probe import ProbeError, parse_frame_rate, parse_probe_payload
from analyzer.semantic import (
    OpenAICompatibleSemanticProvider,
    SemanticProviderError,
    _parse_json_object,
    validate_source_review_payload,
    validate_source_review_quality,
    validate_semantic_payload,
)
from analyzer.speech import speech_text_for_range


class FakeSemanticProvider:
    name = "fake-multimodal"
    model = "fake-vision-1"
    prompt_version = "test-shot-card-v1"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.review_calls: list[dict] = []

    def analyze_shot(self, context, keyframes):
        self.calls.append({"context": context, "keyframes": list(keyframes)})
        return {
            "description": f"Product shown in {context['shot_id']}",
            "semantic_tags": ["product_macro"],
            "people": {"visible": False, "identity_labels": [], "clothing_labels": [], "emotion": "unknown"},
            "product": {
                "visible": True,
                "identity_label": "pin-a",
                "visibility_score": 0.9,
                "state_before": "on table",
                "state_after": "on table",
            },
            "action": {
                "label": "show product",
                "phase": "result",
                "state_before": "on table",
                "state_after": "on table",
                "direction": "static",
            },
            "composition": {
                "shot_size": "close_up",
                "subject_bbox": [0.2, 0.2, 0.6, 0.6],
                "safe_caption_regions": ["top"],
            },
            "transition_fitness": {"good_entry": True, "good_exit": True, "notes": ""},
            "camera_motion": "static",
            "ocr_text": "",
            "model_confidence": 0.95,
        }

    def review_source_shots(self, source_context, shot_contexts, shot_frames):
        self.review_calls.append(
            {
                "source_context": source_context,
                "shot_contexts": list(shot_contexts),
                "shot_frames": {shot_id: list(paths) for shot_id, paths in shot_frames.items()},
            }
        )
        ids = [shot["shot_id"] for shot in shot_contexts]
        revisions = []
        for index, shot in enumerate(shot_contexts):
            payload = semantic_payload()
            revisions.append(
                {
                    "shot_id": shot["shot_id"],
                    "description": f"Jointly reviewed {shot['shot_id']}",
                    "semantic_tags": payload["semantic_tags"],
                    "people": payload["people"],
                    "product": payload["product"],
                    "action": payload["action"],
                    "model_confidence": 0.88,
                    "continuity": (
                        [{"other_shot_id": ids[index + 1], "relation": "good_before", "score": 0.8, "reason": "fake AI evidence"}]
                        if index + 1 < len(ids)
                        else []
                    ),
                }
            )
        return {"shots": revisions}


class FakeSpeechProvider:
    name = "fake-speech"
    model = "fake-asr-1"
    version = "1"

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, source: Path):
        self.calls.append(source)
        return {
            "language": "en",
            "language_probability": 0.99,
            "segments": [
                {
                    "start_seconds": 0.2,
                    "end_seconds": 3.0,
                    "text": "first second",
                    "words": [
                        {"start_seconds": 0.2, "end_seconds": 0.8, "text": " first", "probability": 0.9},
                        {"start_seconds": 2.2, "end_seconds": 2.8, "text": " second", "probability": 0.9},
                    ],
                }
            ],
        }


def semantic_payload() -> dict:
    return FakeSemanticProvider().analyze_shot(
        {"shot_id": "shot-test", "source_uri": "file:///test.mp4"},
        [Path("frame.jpg")],
    )


def source_review_payload(shot_ids: list[str]) -> dict:
    semantic = semantic_payload()
    return {
        "shots": [
            {
                "shot_id": shot_id,
                "description": f"Jointly reviewed {shot_id}",
                "semantic_tags": semantic["semantic_tags"],
                "people": semantic["people"],
                "product": semantic["product"],
                "action": semantic["action"],
                "model_confidence": semantic["model_confidence"],
                "continuity": [],
            }
            for shot_id in shot_ids
        ]
    }


class ProbeTests(unittest.TestCase):
    def test_cli_uses_real_material_scene_threshold(self) -> None:
        args = build_parser().parse_args(["material"])
        self.assertEqual(args.scene_threshold, 20.0)

    def test_parse_frame_rate(self) -> None:
        self.assertAlmostEqual(parse_frame_rate("30000/1001"), 29.97002997)
        self.assertEqual(parse_frame_rate("0/0"), 0.0)

    def test_parse_probe_payload(self) -> None:
        metadata = parse_probe_payload(
            {
                "format": {"duration": "4.25", "format_name": "mov,mp4", "bit_rate": "1200000"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920, "avg_frame_rate": "30/1"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
        )
        self.assertEqual(metadata.duration, 4.25)
        self.assertEqual((metadata.width, metadata.height), (1080, 1920))
        self.assertTrue(metadata.has_audio)

    def test_probe_payload_rejects_audio_only(self) -> None:
        with self.assertRaises(ProbeError):
            parse_probe_payload({"format": {"duration": "1"}, "streams": [{"codec_type": "audio"}]})

    def test_metrics_support_unicode_windows_paths(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as temp_dir:
            frame_path = Path(temp_dir) / "中文关键帧.jpg"
            ok, encoded = cv2.imencode(".jpg", np.full((32, 32, 3), 128, dtype=np.uint8))
            self.assertTrue(ok)
            frame_path.write_bytes(encoded.tobytes())
            metrics = measure_frames([frame_path])
        self.assertAlmostEqual(metrics["brightness"], 128 / 255, places=2)
        self.assertNotIn("no_frames_for_metrics", metrics["warnings"])


class FrameCacheTests(unittest.TestCase):
    def test_existing_nonempty_keyframes_and_proxy_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "material.mp4"
            source.write_bytes(b"source")
            scene = SceneRange(0.0, 2.0, "test")
            shot_id = "source_001_shot_001"
            shot_dir = root / "cache" / shot_id
            shot_dir.mkdir(parents=True)
            expected_frames = []
            for index, timestamp in enumerate(sample_times(scene), start=1):
                frame = shot_dir / f"keyframe_{index:02d}_{timestamp:.3f}s.jpg"
                frame.write_bytes(f"existing-{index}".encode())
                expected_frames.append(frame)
            proxy = shot_dir / "proxy.mp4"
            proxy.write_bytes(b"existing-proxy")

            with (
                patch("analyzer.frames.extract_frame") as extract_frame_mock,
                patch("analyzer.frames.extract_proxy_clip") as extract_proxy_mock,
            ):
                cached = cache_shot_frames(source, scene, shot_id, root / "cache")

            self.assertEqual(cached.keyframes, expected_frames)
            self.assertEqual(cached.proxy, proxy)
            self.assertEqual(cached.warnings, [])
            extract_frame_mock.assert_not_called()
            extract_proxy_mock.assert_not_called()
            self.assertEqual(expected_frames[0].read_bytes(), b"existing-1")
            self.assertEqual(proxy.read_bytes(), b"existing-proxy")


class PipelineTests(unittest.TestCase):
    def test_analyze_directory_writes_contract_valid_shot_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "material.mp4"
            source.write_bytes(b"test-placeholder")
            output = root / "result" / "footage_index.json"

            metadata = VideoMetadata(4.0, 1080, 1920, 30.0, True, "h264", "aac", "mp4", 1000)
            scenes = [SceneRange(0.0, 2.0, "test"), SceneRange(2.0, 4.0, "test")]

            def fake_cache(video_path, scene, shot_id, cache_root, **kwargs):
                frame = cache_root / shot_id / "keyframe.jpg"
                frame.parent.mkdir(parents=True, exist_ok=True)
                frame.write_bytes(b"fake-jpeg")
                proxy = cache_root / shot_id / "proxy.mp4"
                proxy.write_bytes(b"fake-proxy")
                return CachedFrames([frame], proxy, [])

            metrics = {"sharpness": 0.8, "brightness": 0.5, "contrast": 0.4, "motion_score": 0.1, "warnings": []}
            provider = FakeSemanticProvider()
            speech_provider = FakeSpeechProvider()
            with (
                patch("analyzer.pipeline.probe_video", return_value=metadata),
                patch("analyzer.pipeline.detect_scenes", return_value=(scenes, None)),
                patch("analyzer.pipeline.cache_shot_frames", side_effect=fake_cache),
                patch("analyzer.pipeline.measure_frames", return_value=metrics),
            ):
                manifest = analyze_directory(
                    root,
                    output,
                    job_id="pin-job-test",
                    semantic_provider=provider,
                    speech_provider=speech_provider,
                )

            self.assertTrue(output.exists())
            on_disk = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["schema_version"], "1.0.0")
            self.assertEqual(on_disk["job_id"], "pin-job-test")
            self.assertEqual(len(on_disk["sources"]), 1)
            self.assertEqual(len(on_disk["shots"]), 2)
            self.assertEqual(manifest["shots"][0]["source_asset_id"], "source_001")
            self.assertEqual(manifest["shots"][1]["start_seconds"], 2.0)
            self.assertEqual(manifest["shots"][0]["description"], "Jointly reviewed source_001_shot_001")
            self.assertFalse(Path(manifest["shots"][0]["evidence"]["keyframe_uris"][0]).is_absolute())
            self.assertEqual(manifest["shots"][0]["continuity"][0]["other_shot_id"], "source_001_shot_002")
            self.assertEqual(manifest["shots"][0]["continuity"][0]["reason"], "fake AI evidence")
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(len(provider.review_calls), 1)
            self.assertEqual(len(provider.review_calls[0]["shot_contexts"]), 2)
            self.assertEqual(set(provider.review_calls[0]["shot_frames"]), {"source_001_shot_001", "source_001_shot_002"})
            self.assertEqual(len(speech_provider.calls), 1)
            self.assertEqual(provider.calls[0]["context"]["keyframe_timestamps_seconds"], [0.15])
            self.assertEqual(manifest["shots"][0]["evidence"]["speech_text"], "first")
            self.assertEqual(manifest["shots"][1]["evidence"]["speech_text"], "second")

    def test_provider_failure_does_not_write_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "material.mp4").write_bytes(b"test-placeholder")
            output = root / "footage_index.json"
            with patch(
                "analyzer.pipeline.create_semantic_provider",
                side_effect=SemanticProviderError("provider unavailable"),
            ):
                with self.assertRaisesRegex(SemanticProviderError, "provider unavailable"):
                    analyze_directory(root, output)
            self.assertFalse(output.exists())

    def test_existing_ai_caches_only_call_missing_source_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "material.mp4"
            source.write_bytes(b"test-placeholder")
            cache_root = root / "cache"
            metadata = VideoMetadata(4.0, 1080, 1920, 30.0, True, "h264", "aac", "mp4", 1000)
            scenes = [SceneRange(0.0, 2.0, "test"), SceneRange(2.0, 4.0, "test")]

            def fake_cache(video_path, scene, shot_id, cache_dir, **kwargs):
                frame = cache_dir / shot_id / "keyframe.jpg"
                frame.parent.mkdir(parents=True, exist_ok=True)
                frame.write_bytes(b"fake-jpeg")
                proxy = cache_dir / shot_id / "proxy.mp4"
                proxy.write_bytes(b"fake-proxy")
                return CachedFrames([frame], proxy, [])

            metrics = {"sharpness": 0.8, "brightness": 0.5, "contrast": 0.4, "motion_score": 0.1, "warnings": []}
            first_provider = FakeSemanticProvider()
            first_speech = FakeSpeechProvider()
            with (
                patch("analyzer.pipeline.probe_video", return_value=metadata),
                patch("analyzer.pipeline.detect_scenes", return_value=(scenes, None)),
                patch("analyzer.pipeline.cache_shot_frames", side_effect=fake_cache),
                patch("analyzer.pipeline.measure_frames", return_value=metrics),
            ):
                analyze_directory(
                    root,
                    root / "first.json",
                    cache_root,
                    semantic_provider=first_provider,
                    speech_provider=first_speech,
                )

            class V2SemanticProvider(FakeSemanticProvider):
                source_review_prompt_version = "source-shot-review-v2"

            second_provider = V2SemanticProvider()
            second_speech = FakeSpeechProvider()
            with (
                patch("analyzer.pipeline.probe_video", return_value=metadata),
                patch("analyzer.pipeline.detect_scenes", return_value=(scenes, None)),
                patch("analyzer.pipeline.cache_shot_frames", side_effect=fake_cache),
                patch("analyzer.pipeline.measure_frames", return_value=metrics),
            ):
                analyze_directory(
                    root,
                    root / "reviewed.json",
                    cache_root,
                    semantic_provider=second_provider,
                    speech_provider=second_speech,
                )

            self.assertEqual(second_provider.calls, [])
            self.assertEqual(second_speech.calls, [])
            self.assertEqual(len(second_provider.review_calls), 1)
            self.assertEqual(len(second_provider.review_calls[0]["shot_contexts"]), 2)
            self.assertTrue((cache_root / "source_001" / "semantic_review.json").is_file())
            self.assertTrue(
                (cache_root / "source_001" / "semantic_review_source-shot-review-v2.json").is_file()
            )

    def test_source_review_failure_does_not_write_manifest(self) -> None:
        class FailingReviewProvider(FakeSemanticProvider):
            def review_source_shots(self, source_context, shot_contexts, shot_frames):
                raise SemanticProviderError("source review unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "material.mp4").write_bytes(b"test-placeholder")
            output = root / "footage_index.json"
            metadata = VideoMetadata(2.0, 1080, 1920, 30.0, False, "h264", None, "mp4", 1000)
            scenes = [SceneRange(0.0, 2.0, "test")]

            def fake_cache(video_path, scene, shot_id, cache_root, **kwargs):
                frame = cache_root / shot_id / "keyframe.jpg"
                frame.parent.mkdir(parents=True, exist_ok=True)
                frame.write_bytes(b"fake-jpeg")
                return CachedFrames([frame], None, [])

            metrics = {"sharpness": 0.8, "brightness": 0.5, "contrast": 0.4, "motion_score": 0.1, "warnings": []}
            with (
                patch("analyzer.pipeline.probe_video", return_value=metadata),
                patch("analyzer.pipeline.detect_scenes", return_value=(scenes, None)),
                patch("analyzer.pipeline.cache_shot_frames", side_effect=fake_cache),
                patch("analyzer.pipeline.measure_frames", return_value=metrics),
            ):
                with self.assertRaisesRegex(SemanticProviderError, "source review unavailable"):
                    analyze_directory(root, output, semantic_provider=FailingReviewProvider())

            self.assertFalse(output.exists())


class SemanticValidationTests(unittest.TestCase):
    def test_source_review_requires_exact_shot_id_set(self) -> None:
        semantic = semantic_payload()
        item = {
            "shot_id": "shot-001",
            "description": semantic["description"],
            "semantic_tags": semantic["semantic_tags"],
            "people": semantic["people"],
            "product": semantic["product"],
            "action": semantic["action"],
            "model_confidence": semantic["model_confidence"],
            "continuity": [],
        }
        with self.assertRaisesRegex(SemanticProviderError, "shot_id set mismatch"):
            validate_source_review_payload({"shots": [item]}, ["shot-001", "shot-002"])

    def test_source_review_rejects_duplicate_shot_id(self) -> None:
        semantic = semantic_payload()
        item = {
            "shot_id": "shot-001",
            "description": semantic["description"],
            "semantic_tags": semantic["semantic_tags"],
            "people": semantic["people"],
            "product": semantic["product"],
            "action": semantic["action"],
            "model_confidence": semantic["model_confidence"],
            "continuity": [],
        }
        with self.assertRaisesRegex(SemanticProviderError, "duplicate shot_id"):
            validate_source_review_payload({"shots": [item, dict(item)]}, ["shot-001"])

    def test_source_review_quality_rejects_broad_tag_drift(self) -> None:
        shot_ids = ["shot-001", "shot-002", "shot-003"]
        review = source_review_payload(shot_ids)
        review["shots"][0]["semantic_tags"] = ["photo_selection"]
        review["shots"][1]["semantic_tags"] = ["text_customization"]
        contexts = [
            {"shot_id": shot_id, "semantic_tags": ["product_macro"], "action": {"phase": "result"}}
            for shot_id in shot_ids
        ]
        with self.assertRaisesRegex(SemanticProviderError, "changed semantic tags for 2/3 shots"):
            validate_source_review_quality(review, contexts)

    def test_source_review_quality_rejects_reciprocal_relations(self) -> None:
        shot_ids = ["shot-001", "shot-002"]
        review = source_review_payload(shot_ids)
        review["shots"][0]["continuity"] = [
            {"other_shot_id": "shot-002", "relation": "good_before", "score": 0.8, "reason": "same object state"}
        ]
        review["shots"][1]["continuity"] = [
            {"other_shot_id": "shot-001", "relation": "good_after", "score": 0.8, "reason": "same object state"}
        ]
        contexts = [
            {"shot_id": shot_id, "semantic_tags": ["product_macro"], "action": {"phase": "result"}}
            for shot_id in shot_ids
        ]
        with self.assertRaisesRegex(SemanticProviderError, "redundant reciprocal continuity"):
            validate_source_review_quality(review, contexts)

    def test_source_review_quality_rejects_complete_weak_adjacent_chain(self) -> None:
        shot_ids = ["shot-001", "shot-002", "shot-003", "shot-004"]
        review = source_review_payload(shot_ids)
        for index in range(len(shot_ids) - 1):
            review["shots"][index]["continuity"] = [
                {
                    "other_shot_id": shot_ids[index + 1],
                    "relation": "good_before",
                    "score": 0.8,
                    "reason": "specific shared visual state",
                }
            ]
        contexts = [
            {"shot_id": shot_id, "semantic_tags": ["product_macro"], "action": {"phase": "result"}}
            for shot_id in shot_ids
        ]
        with self.assertRaisesRegex(SemanticProviderError, "complete weak relation chain"):
            validate_source_review_quality(review, contexts)

    def test_provider_retries_invalid_structured_data_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.jpg"
            frame.write_bytes(b"fake-jpeg")
            valid = json.dumps(semantic_payload())

            class FakeCompletions:
                def __init__(self) -> None:
                    self.contents = ["not-json", valid]
                    self.call_count = 0

                def create(self, **kwargs):
                    content = self.contents[self.call_count]
                    self.call_count += 1
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                    )

            completions = FakeCompletions()
            provider = OpenAICompatibleSemanticProvider.__new__(OpenAICompatibleSemanticProvider)
            provider.name = "fake"
            provider.model = "fake-model"
            provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            result = provider.analyze_shot({"shot_id": "retry-shot"}, [frame])

        self.assertEqual(result["semantic_tags"], ["product_macro"])
        self.assertEqual(completions.call_count, 2)

    def test_provider_fails_after_second_invalid_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.jpg"
            frame.write_bytes(b"fake-jpeg")

            class FakeCompletions:
                call_count = 0

                def create(self, **kwargs):
                    self.call_count += 1
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))]
                    )

            completions = FakeCompletions()
            provider = OpenAICompatibleSemanticProvider.__new__(OpenAICompatibleSemanticProvider)
            provider.name = "fake"
            provider.model = "fake-model"
            provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            with self.assertRaisesRegex(SemanticProviderError, "invalid structured data twice"):
                provider.analyze_shot({"shot_id": "retry-shot"}, [frame])

        self.assertEqual(completions.call_count, 2)

    def test_source_review_retries_invalid_structured_data_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import cv2
            import numpy as np

            frames = []
            for index in range(3):
                frame = Path(temp_dir) / f"frame-{index}.jpg"
                ok, encoded = cv2.imencode(".jpg", np.full((48, 32, 3), 60 + index * 60, dtype=np.uint8))
                self.assertTrue(ok)
                frame.write_bytes(encoded.tobytes())
                frames.append(frame)
            valid = json.dumps(source_review_payload(["shot-001"]))

            class FakeCompletions:
                def __init__(self) -> None:
                    self.contents = ["not-json", valid]
                    self.call_count = 0
                    self.kwargs: list[dict] = []

                def create(self, **kwargs):
                    self.kwargs.append(kwargs)
                    content = self.contents[self.call_count]
                    self.call_count += 1
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                    )

            completions = FakeCompletions()
            provider = OpenAICompatibleSemanticProvider.__new__(OpenAICompatibleSemanticProvider)
            provider.name = "fake"
            provider.model = "fake-model"
            provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            original = semantic_payload()
            result = provider.review_source_shots(
                {"asset_id": "source_001"},
                [
                    {
                        "shot_id": "shot-001",
                        "semantic_tags": original["semantic_tags"],
                        "action": original["action"],
                    }
                ],
                {"shot-001": frames},
            )

        self.assertEqual(result["shots"][0]["shot_id"], "shot-001")
        self.assertEqual(completions.call_count, 2)
        first_content = completions.kwargs[0]["messages"][1]["content"]
        self.assertEqual(sum(item.get("type") == "image_url" for item in first_content), 1)
        image_item = next(item for item in first_content if item.get("type") == "image_url")
        self.assertTrue(image_item["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_known_json_wrappers_are_accepted(self) -> None:
        self.assertEqual(_parse_json_object('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(_parse_json_object('<think>private reasoning</think>\n{"ok": true}'), {"ok": True})

    def test_prose_around_json_is_rejected(self) -> None:
        with self.assertRaisesRegex(SemanticProviderError, "single JSON object"):
            _parse_json_object('Here is the result: {"ok": true}')

    def test_unknown_and_low_confidence_are_preserved(self) -> None:
        payload = semantic_payload()
        payload["people"]["emotion"] = "unknown"
        payload["model_confidence"] = 0.2
        normalized = validate_semantic_payload(payload)
        self.assertEqual(normalized["people"]["emotion"], "unknown")
        self.assertEqual(normalized["model_confidence"], 0.2)

    def test_unexpected_model_field_is_rejected(self) -> None:
        payload = semantic_payload()
        payload["unsupported_claim"] = "waterproof"
        with self.assertRaisesRegex(SemanticProviderError, "unexpected field"):
            validate_semantic_payload(payload)

    def test_invalid_semantic_tag_is_rejected(self) -> None:
        payload = semantic_payload()
        payload["semantic_tags"] = ["invented_role"]
        with self.assertRaisesRegex(SemanticProviderError, "unsupported value"):
            validate_semantic_payload(payload)


class SpeechMappingTests(unittest.TestCase):
    def test_words_are_assigned_by_midpoint_without_duplication(self) -> None:
        transcript = {
            "segments": [
                {
                    "start_seconds": 0.5,
                    "end_seconds": 2.5,
                    "text": "one two",
                    "words": [
                        {"start_seconds": 0.5, "end_seconds": 1.0, "text": " one"},
                        {"start_seconds": 2.0, "end_seconds": 2.5, "text": " two"},
                    ],
                }
            ]
        }
        self.assertEqual(speech_text_for_range(transcript, 0.0, 2.0), "one")
        self.assertEqual(speech_text_for_range(transcript, 2.0, 3.0), "two")


if __name__ == "__main__":
    unittest.main()
