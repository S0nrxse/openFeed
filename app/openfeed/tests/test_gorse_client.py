from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import gorse_client


class GorseClientMappingTests(unittest.TestCase):
    def test_watch_progress_over_threshold_maps_to_positive_watch_feedback(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def fake_request(method: str, path: str, *, json=None, timeout: float = 4.0):
            calls.append((method, path, json))
            return {"ok": True}

        video = {"youtube_video_id": "abc123", "title": "Useful video", "topic_tokens": ["technology"]}
        with patch.object(gorse_client, "_request", side_effect=fake_request):
            result = gorse_client.sync_feedback_event("user-1", video, "watch_progress", {"percent_completed": 0.72})

        self.assertTrue(result["ok"])
        self.assertEqual(result["feedback_type"], "watch>=0.65")
        feedback_call = calls[-1]
        self.assertEqual(feedback_call[0], "POST")
        self.assertEqual(feedback_call[1], "/api/feedback")
        self.assertEqual(feedback_call[2][0]["FeedbackType"], "watch>=0.65")
        self.assertEqual(feedback_call[2][0]["UserId"], "user-1")
        self.assertEqual(feedback_call[2][0]["ItemId"], "abc123")

    def test_undo_not_interested_deletes_negative_feedback(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def fake_request(method: str, path: str, *, json=None, timeout: float = 4.0):
            calls.append((method, path, json))
            return {"RowAffected": 1}

        video = {"youtube_video_id": "abc123", "title": "Useful video"}
        with patch.object(gorse_client, "_request", side_effect=fake_request):
            result = gorse_client.sync_feedback_event("user-1", video, "undo_not_interested", {})

        self.assertTrue(result["ok"])
        delete_paths = [path for method, path, _ in calls if method == "DELETE"]
        self.assertIn("/api/feedback/not_interested/user-1/abc123", delete_paths)
        self.assertIn("/api/feedback/hide/user-1/abc123", delete_paths)


if __name__ == "__main__":
    unittest.main()
