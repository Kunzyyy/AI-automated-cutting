import unittest
from unittest.mock import patch

import run_workflow as workflow


class WorkflowTests(unittest.TestCase):
    def test_progressive_ass_events_do_not_overlap(self):
        events = workflow.progressive_ass_events("one two three", 1.0, 4.0)
        self.assertEqual([event[2] for event in events], ["one", "one two", "one two three"])
        self.assertEqual(events[0][0], 1.0)
        self.assertEqual(events[-1][1], 4.0)
        for current, following in zip(events, events[1:]):
            self.assertEqual(current[1], following[0])

    def test_ass_text_is_escaped(self):
        self.assertEqual(workflow.escape_ass_text("a{b}\\c"), r"a\{b\}\\c")

    def test_incomplete_ai_caption_uses_complete_fallback(self):
        fallback = "Upload a photo & custom text"
        self.assertEqual(
            workflow.normalize_ad_caption("Turn a precious photo into...", fallback),
            fallback,
        )
        self.assertEqual(
            workflow.normalize_ad_caption("A complete short caption.", fallback),
            "A complete short caption",
        )

    def test_fallback_story_matches_clip_count(self):
        clips = [
            workflow.Clip("material.mp4", index * 2.0, (index + 1) * 2.0, 2.0)
            for index in range(9)
        ]
        plan = workflow.CandidatePlan(1, clips, 18.0)
        segments = workflow.build_subtitle_segments(plan, 18.0, "别针")
        # 每个 clip 对应一个字幕段
        self.assertEqual(len(segments), 9)
        # 角色循环复用 SUBTITLE_ROLES
        self.assertEqual(segments[0].role, "emotion_hook")
        self.assertEqual(segments[4].role, "emotion_hook")  # 第5个clip，循环回来
        self.assertEqual(segments[0].start, 0.0)
        self.assertEqual(segments[-1].end, 18.0)
        self.assertTrue(all("..." not in segment.text and "…" not in segment.text for segment in segments))

    def test_long_caption_wraps_to_two_lines(self):
        wrapped = workflow.wrap_ad_caption(
            "Keep their memory alive wherever life takes you"
        )
        self.assertEqual(wrapped.count(r"\N"), 1)

    def test_whisper_evidence_selects_role_but_never_raw_copy(self):
        clips = [workflow.Clip("material.mp4", i * 2.0, (i + 1) * 2.0, 2.0) for i in range(8)]
        plan = workflow.CandidatePlan(1, clips, 16.0)
        segments = workflow.build_subtitle_segments(plan, 16.0, "别针")
        evidence = [{"start": 8.2, "end": 11.8, "text": "You can upload a photo and add custom text..."}]
        workflow.apply_transcript_role_evidence(segments, evidence, "别针")
        # 8个segments，evidence落在8-10s段，该段被标记为customization
        self.assertEqual(len(segments), 8)
        # 验证包含customization角色（被evidence触发）
        self.assertIn("customization", [s.role for s in segments])
        # 验证原始转写文本不上屏
        self.assertTrue(all("..." not in segment.text for segment in segments))
        self.assertNotIn("You can upload", " ".join(segment.text for segment in segments))

    def test_planner_targets_around_fifteen_seconds(self):
        video = workflow.VideoInfo("material.mp4", 20.0, 720, 1280, 30.0, True)
        scene = workflow.Scene(0, 0.0, 20.0, 20.0, 8.0, False)
        with patch.object(workflow, "detect_frame_glitch", return_value=False):
            plans = workflow.stage3_heuristic_planning([(video, [scene])], 2)
        self.assertEqual(len(plans), 2)
        for plan in plans:
            self.assertLessEqual(abs(plan.total_duration - workflow.TARGET_DURATION), 3.0)

    def test_planner_does_not_reject_short_material(self):
        video = workflow.VideoInfo("short.mp4", 8.0, 720, 1280, 30.0, True)
        scene = workflow.Scene(0, 0.0, 8.0, 8.0, 8.0, False)
        with patch.object(workflow, "detect_frame_glitch", return_value=False):
            plans = workflow.stage3_heuristic_planning([(video, [scene])], 2)
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(plan.total_duration < workflow.DURATION_SOFT_MIN for plan in plans))

if __name__ == "__main__":
    unittest.main()
