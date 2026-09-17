from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import db, main
from openfeed.playback import select_preview_stream


def _stream(itag: str, height: int, container: str = "mp4", url: str | None = None) -> dict[str, object]:
    return {
        "itag": itag,
        "url": url if url is not None else f"https://stream.example/{itag}",
        "qualityLabel": f"{height}p",
        "type": f'video/{container}; codecs="avc1"',
    }


class SelectPreviewStreamTests(unittest.TestCase):
    def test_prefers_360p_mp4_over_better_renditions(self) -> None:
        video = {"formatStreams": [_stream("22", 720), _stream("43", 360, "webm"), _stream("18", 360)]}
        self.assertEqual(select_preview_stream(video)["itag"], "18")

    def test_prefers_mp4_when_nothing_is_small(self) -> None:
        video = {"formatStreams": [_stream("45", 720, "webm"), _stream("22", 720)]}
        self.assertEqual(select_preview_stream(video)["itag"], "22")

    def test_falls_back_to_lowest_rendition(self) -> None:
        video = {"formatStreams": [_stream("38", 3072), _stream("22", 720)]}
        self.assertEqual(select_preview_stream(video)["itag"], "22")

    def test_ignores_streams_without_urls(self) -> None:
        video = {"formatStreams": [dict(_stream("18", 360), url=""), {"itag": "22"}]}
        self.assertIsNone(select_preview_stream(video))

    def test_none_without_format_streams(self) -> None:
        self.assertIsNone(select_preview_stream({}))


class PreviewEndpointTests(unittest.TestCase):
    """Exercise the real route with no startup, no Invidious, and no live database."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "false"}))
        # Any unmocked database access is a test failure, never a live connection.
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def _video_row(self, streams: list[dict[str, object]]) -> dict[str, object]:
        # Enrichment unwraps the payload when it stores it, so cached rows carry
        # the Invidious response directly.
        return {"youtube_video_id": "dQw4w9WgXcQ", "metadata": {"formatStreams": streams}}

    def _upstream(self, status: int = 200, body: bytes = b"0123") -> SimpleNamespace:
        return SimpleNamespace(
            status_code=status,
            headers={"content-type": "video/mp4", "content-length": str(len(body))},
            iter_bytes=lambda: iter([body]),
            close=lambda: None,
        )

    def test_rejects_malformed_ids_before_touching_the_db(self) -> None:
        self.assertEqual(self.client.get("/api/preview/not-a-video-id!!").status_code, 404)

    def test_serves_cached_light_stream(self) -> None:
        streams = [
            _stream("22", 720, url="https://signed.example/22"),
            _stream("18", 360, url="https://signed.example/18"),
        ]
        fetch = self.enterContext(patch.object(main, "_fetch_preview_upstream", return_value=self._upstream()))
        lookup = self.enterContext(patch.object(main.db, "get_video_by_youtube_id", return_value=self._video_row(streams)))

        response = self.client.get("/api/preview/dQw4w9WgXcQ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.content, b"0123")
        fetch.assert_called_once_with("https://signed.example/18", None)
        lookup.assert_called_once_with("dQw4w9WgXcQ")

    def test_reads_raw_wrapped_metadata(self) -> None:
        streams = [_stream("18", 360, url="https://signed.example/18")]
        fetch = self.enterContext(patch.object(main, "_fetch_preview_upstream", return_value=self._upstream()))
        self.enterContext(
            patch.object(
                main.db,
                "get_video_by_youtube_id",
                return_value={"youtube_video_id": "dQw4w9WgXcQ", "metadata": {"raw": {"formatStreams": streams}}},
            )
        )

        response = self.client.get("/api/preview/dQw4w9WgXcQ")

        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with("https://signed.example/18", None)

    def test_forwards_byte_ranges_for_seeking(self) -> None:
        upstream = self._upstream(status=206, body=b"01")
        upstream.headers["content-range"] = "bytes 0-1/4"
        fetch = self.enterContext(patch.object(main, "_fetch_preview_upstream", return_value=upstream))
        self.enterContext(
            patch.object(main.db, "get_video_by_youtube_id", return_value=self._video_row([_stream("18", 360)]))
        )

        response = self.client.get("/api/preview/dQw4w9WgXcQ", headers={"range": "bytes=0-1"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-1/4")
        fetch.assert_called_once_with("https://stream.example/18", "bytes=0-1")

    def test_refreshes_once_when_the_cached_signature_expired(self) -> None:
        lookup = self.enterContext(
            patch.object(
                main.db,
                "get_video_by_youtube_id",
                side_effect=[
                    self._video_row([_stream("18", 360, url="https://signed.example/stale")]),
                    self._video_row([_stream("18", 360, url="https://signed.example/fresh")]),
                ],
            )
        )
        refresh = self.enterContext(patch.object(main, "enrich_video", return_value={"ok": True}))
        fetch = self.enterContext(
            patch.object(
                main,
                "_fetch_preview_upstream",
                side_effect=[self._upstream(status=403, body=b"denied"), self._upstream()],
            )
        )

        response = self.client.get("/api/preview/dQw4w9WgXcQ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_args_list[-1].args[0], "https://signed.example/fresh")
        refresh.assert_called_once_with("dQw4w9WgXcQ")
        self.assertEqual(lookup.call_count, 2)

    def test_404_when_nothing_playable(self) -> None:
        self.enterContext(
            patch.object(main.db, "get_video_by_youtube_id", return_value=self._video_row([]))
        )
        self.enterContext(patch.object(main, "enrich_video", return_value={"ok": False}))

        self.assertEqual(self.client.get("/api/preview/dQw4w9WgXcQ").status_code, 404)
