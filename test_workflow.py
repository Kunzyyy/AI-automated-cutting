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

    def test_planner_never_falls_below_duration_floor(self):
        video = workflow.VideoInfo("material.mp4", 20.0, 720, 1280, 30.0, True)
        scene = workflow.Scene(0, 0.0, 20.0, 20.0, 8.0, False)
        with patch.object(workflow, "detect_frame_glitch", return_value=False):
            plans = workflow.stage3_heuristic_planning([(video, [scene])], 2)
        self.assertEqual(len(plans), 2)
        for plan in plans:
            self.assertGreaterEqual(plan.total_duration, workflow.TARGET_DURATION_MIN)
            self.assertLessEqual(plan.total_duration, workflow.TARGET_DURATION_MAX)

    def test_quality_exclusion_is_loaded(self):
        rules = workflow.load_quality_exclusions("别针")
        self.assertTrue(any(rule["file"] == "10288936485181-6f3214.mp4" for rule in rules))


if __name__ == "__main__":
    unittest.main()
