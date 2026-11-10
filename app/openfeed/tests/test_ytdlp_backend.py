from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import downloads, ytdlp


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "ytdlp_enabled": True,
        "ytdlp_binary": "yt-dlp",
        "ytdlp_timeout_seconds": 90,
        "ytdlp_cache_seconds": 240,
        "ytdlp_cookies_file": "",
        "ytdlp_extra_args": "",
        "download_dir": "/tmp/openfeed-test-downloads",
        "download_retention_seconds": 21600,
        "download_max_concurrent": 2,
        "download_max_height": 2160,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# A trimmed but structurally faithful `yt-dlp -J` payload: one progressive rendition,
# split video tracks above it, and a bare audio track to pair them with.
INFO = {
    "id": "vid1",
    "title": "Example Video",
    "duration": 610,
    "formats": [
        {"format_id": "18", "url": "https://x/18", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 10_000_000, "tbr": 600},
        {"format_id": "22", "url": "https://x/22", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "tbr": 1500},
        {"format_id": "137", "url": "https://x/137", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 90_000_000},
        {"format_id": "313", "url": "https://x/313", "ext": "webm", "height": 2160, "vcodec": "vp9", "acodec": "none"},
        {"format_id": "140", "url": "https://x/140", "ext": "m4a", "height": None, "vcodec": "none", "acodec": "mp4a", "filesize": 6_000_000},
    ],
}


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        ytdlp.clear_cache()

    def tearDown(self) -> None:
        ytdlp.clear_cache()

    def _patched(self, **kwargs: object):
        return patch.multiple(
            ytdlp,
            binary_path=lambda: "/usr/bin/yt-dlp",
            get_settings=lambda: _settings(**kwargs),
        )

    def test_metadata_only_never_asks_for_a_download(self) -> None:
        seen: dict[str, list[str]] = {}

        def _run(command, **kwargs):
            seen["command"] = command
            return _Completed(stdout=json.dumps(INFO))

        with self._patched(), patch.object(subprocess, "run", _run):
            ytdlp.probe("vid1")
        self.assertIn("-J", seen["command"])
        self.assertNotIn("-o", seen["command"])
        self.assertIn("--no-playlist", seen["command"])

    def test_a_second_call_is_served_from_cache(self) -> None:
        calls = {"n": 0}

        def _run(command, **kwargs):
            calls["n"] += 1
            return _Completed(stdout=json.dumps(INFO))

        with self._patched(), patch.object(subprocess, "run", _run):
            ytdlp.probe("vid1")
            ytdlp.probe("vid1")
        self.assertEqual(calls["n"], 1)

    def test_a_zero_ttl_disables_caching_because_urls_expire(self) -> None:
        calls = {"n": 0}

        def _run(command, **kwargs):
            calls["n"] += 1
            return _Completed(stdout=json.dumps(INFO))

        with self._patched(ytdlp_cache_seconds=0), patch.object(subprocess, "run", _run):
            ytdlp.probe("vid1")
            ytdlp.probe("vid1")
        self.assertEqual(calls["n"], 2)

    def test_a_nonzero_exit_surfaces_the_last_stderr_line(self) -> None:
        with self._patched(), patch.object(subprocess, "run", lambda *a, **k: _Completed(stderr="line one\nERROR: Video unavailable", returncode=1)):
            with self.assertRaises(ytdlp.YtdlpError) as caught:
                ytdlp.probe("vid1")
        self.assertIn("Video unavailable", str(caught.exception))

    def test_a_timeout_is_reported_not_hung_on(self) -> None:
        def _run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="yt-dlp", timeout=90)

        with self._patched(), patch.object(subprocess, "run", _run):
            with self.assertRaises(ytdlp.YtdlpError) as caught:
                ytdlp.probe("vid1")
        self.assertIn("timed out", str(caught.exception))

    def test_unparseable_output_is_an_error_not_a_crash(self) -> None:
        with self._patched(), patch.object(subprocess, "run", lambda *a, **k: _Completed(stdout="<html>")):
            with self.assertRaises(ytdlp.YtdlpError):
                ytdlp.probe("vid1")

    def test_a_missing_binary_is_reported_clearly(self) -> None:
        with patch.object(ytdlp, "binary_path", lambda: ""), patch.object(ytdlp, "get_settings", lambda: _settings()):
            with self.assertRaises(ytdlp.YtdlpError):
                ytdlp.probe("vid1")

    def test_a_cookies_file_is_passed_by_path_only(self) -> None:
        with patch.object(ytdlp, "get_settings", lambda: _settings(ytdlp_cookies_file="/secrets/cookies.txt")):
            args = ytdlp.base_args()
        self.assertEqual(args[args.index("--cookies") + 1], "/secrets/cookies.txt")

    def test_unparseable_extra_args_are_ignored_rather_than_crashing(self) -> None:
        with patch.object(ytdlp, "get_settings", lambda: _settings(ytdlp_extra_args="--flag 'unclosed")):
            self.assertNotIn("--flag", ytdlp.base_args())


class PlaybackSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        ytdlp.clear_cache()

    def test_only_streams_carrying_both_tracks_can_drive_a_video_element(self) -> None:
        streams = ytdlp.progressive_streams(INFO)
        self.assertEqual([fmt["format_id"] for fmt in streams], ["22", "18"])

    def test_the_best_progressive_rendition_wins(self) -> None:
        with patch.object(ytdlp, "probe", return_value=INFO):
            stream = ytdlp.playback_stream("vid1")
        self.assertEqual(stream["height"], 720)
        self.assertEqual(stream["stream_url"], "https://x/22")
        self.assertEqual(stream["source"], "yt-dlp")

    def test_a_video_with_only_split_tracks_raises(self) -> None:
        split_only = {"formats": [fmt for fmt in INFO["formats"] if fmt["acodec"] == "none"]}
        with patch.object(ytdlp, "probe", return_value=split_only):
            with self.assertRaises(ytdlp.YtdlpError):
                ytdlp.playback_stream("vid1")


class DownloadFormatTests(unittest.TestCase):
    def _formats(self, ffmpeg: bool = True, **kwargs: object) -> list[dict]:
        with patch.object(ytdlp, "probe", return_value=INFO), \
             patch.object(ytdlp, "ffmpeg_available", lambda: ffmpeg), \
             patch.object(ytdlp, "get_settings", lambda: _settings(**kwargs)):
            return ytdlp.download_formats("vid1")["formats"]

    def test_split_track_resolutions_are_offered_when_ffmpeg_can_merge(self) -> None:
        heights = [item["height"] for item in self._formats()]
        self.assertEqual(heights, [2160, 1080, 720, 360])

    def test_without_ffmpeg_only_already_muxed_renditions_survive(self) -> None:
        heights = [item["height"] for item in self._formats(ffmpeg=False)]
        self.assertEqual(heights, [720, 360])

    def test_merge_requirement_is_flagged_per_resolution(self) -> None:
        by_height = {item["height"]: item for item in self._formats()}
        self.assertTrue(by_height[2160]["needs_merge"])
        self.assertTrue(by_height[1080]["needs_merge"])
        self.assertFalse(by_height[720]["needs_merge"])

    def test_the_height_ceiling_is_respected(self) -> None:
        heights = [item["height"] for item in self._formats(download_max_height=1080)]
        self.assertEqual(heights, [1080, 720, 360])

    def test_progressive_heights_are_marked_so_the_ui_can_skip_the_merge(self) -> None:
        by_height = {item["height"]: item for item in self._formats()}
        self.assertTrue(by_height[720]["progressive"])
        self.assertFalse(by_height[1080]["progressive"])


# A portrait video: yt-dlp's `height` is the long edge, so the naive label is wrong.
PORTRAIT = {
    "id": "short1",
    "title": "Vertical clip",
    "formats": [
        {"format_id": "160", "url": "https://x/160", "ext": "mp4", "width": 144, "height": 256, "format_note": "144p", "vcodec": "avc1", "acodec": "none"},
        {"format_id": "18", "url": "https://x/18", "ext": "mp4", "width": 360, "height": 640, "format_note": "360p", "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "135", "url": "https://x/135", "ext": "mp4", "width": 480, "height": 854, "format_note": "480p", "vcodec": "avc1", "acodec": "none"},
        {"format_id": "787", "url": "https://x/787", "ext": "mp4", "width": 608, "height": 1080, "format_note": "480p", "vcodec": "avc1", "acodec": "none"},
        {"format_id": "137", "url": "https://x/137", "ext": "mp4", "width": 1080, "height": 1920, "format_note": "1080p", "vcodec": "avc1", "acodec": "none"},
        {"format_id": "140", "url": "https://x/140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
    ],
}


class ResolutionLabelTests(unittest.TestCase):
    """A 1080x1920 short must be offered as 1080p, not 1920p."""

    def test_the_format_note_is_trusted_when_it_names_a_resolution(self) -> None:
        self.assertEqual(ytdlp.resolution_label({"format_note": "1080p", "width": 1080, "height": 1920}), ("1080p", 1080))

    def test_a_frame_rate_suffix_survives(self) -> None:
        self.assertEqual(ytdlp.resolution_label({"format_note": "1080p60", "height": 1080}), ("1080p60", 1080))

    def test_without_a_note_the_short_edge_is_used(self) -> None:
        self.assertEqual(ytdlp.resolution_label({"width": 1080, "height": 1920}), ("1080p", 1080))

    def test_landscape_is_unaffected(self) -> None:
        self.assertEqual(ytdlp.resolution_label({"width": 1920, "height": 1080}), ("1080p", 1080))

    def test_a_non_resolution_note_is_not_mistaken_for_one(self) -> None:
        self.assertEqual(ytdlp.resolution_label({"format_note": "tiny", "width": 256, "height": 144}), ("144p", 144))


class PortraitFormatTests(unittest.TestCase):
    def _formats(self, **kwargs: object) -> list[dict]:
        with patch.object(ytdlp, "probe", return_value=PORTRAIT), \
             patch.object(ytdlp, "ffmpeg_available", lambda: True), \
             patch.object(ytdlp, "get_settings", lambda: _settings(**kwargs)):
            return ytdlp.download_formats("short1")["formats"]

    def test_portrait_renditions_are_labelled_the_way_youtube_names_them(self) -> None:
        self.assertEqual([item["quality"] for item in self._formats()], ["1080p", "480p", "360p", "144p"])

    def test_the_selector_height_stays_the_real_pixel_height(self) -> None:
        # Passing the label to `-f bv*[height<=N]` would cap a 1080x1920 short at 1080
        # pixels tall and quietly hand back a much smaller rendition.
        by_label = {item["label_height"]: item for item in self._formats()}
        self.assertEqual(by_label[1080]["height"], 1920)
        self.assertEqual(by_label[360]["height"], 640)

    def test_one_label_covering_several_heights_collapses_to_the_tallest(self) -> None:
        # YouTube ships both 480x854 and 608x1080 as "480p"; the cap must cover both.
        by_label = {item["label_height"]: item for item in self._formats()}
        self.assertEqual(by_label[480]["height"], 1080)
        self.assertEqual(sum(1 for item in self._formats() if item["quality"] == "480p"), 1)

    def test_the_ceiling_compares_against_the_label_not_the_long_edge(self) -> None:
        # A 1080p short would otherwise be filtered out by a 2160 ceiling reading 1920.
        labels = [item["quality"] for item in self._formats(download_max_height=720)]
        self.assertEqual(labels, ["480p", "360p", "144p"])


# Three 1080p renditions of one video, as YouTube actually ships them: the h264 track is
# more than twice the size of the av1 one that `bv*[ext=mp4]` will actually choose.
CODECS = {
    "id": "vid2",
    "title": "Codec spread",
    "formats": [
        {"format_id": "137", "url": "https://x/137", "ext": "mp4", "width": 1920, "height": 1080, "format_note": "1080p", "vcodec": "avc1.640028", "acodec": "none", "tbr": 3927, "filesize": 2_590_000_000},
        {"format_id": "248", "url": "https://x/248", "ext": "webm", "width": 1920, "height": 1080, "format_note": "1080p", "vcodec": "vp9", "acodec": "none", "tbr": 1848, "filesize": 1_220_000_000},
        {"format_id": "399", "url": "https://x/399", "ext": "mp4", "width": 1920, "height": 1080, "format_note": "1080p", "vcodec": "av01.0.08M.08", "acodec": "none", "tbr": 1803, "filesize": 1_190_000_000},
        {"format_id": "139", "url": "https://x/139", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.5", "tbr": 48, "filesize": 32_200_000},
        {"format_id": "140", "url": "https://x/140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 129, "filesize": 85_500_000},
        {"format_id": "251", "url": "https://x/251", "ext": "webm", "vcodec": "none", "acodec": "opus", "tbr": 129, "filesize": 85_500_000},
    ],
}


class SizingTests(unittest.TestCase):
    """The quoted size must describe the rendition that actually gets downloaded."""

    def _formats(self) -> list[dict]:
        with patch.object(ytdlp, "probe", return_value=CODECS), \
             patch.object(ytdlp, "ffmpeg_available", lambda: True), \
             patch.object(ytdlp, "get_settings", lambda: _settings()):
            return ytdlp.download_formats("vid2")["formats"]

    def test_the_codec_the_selector_prefers_is_the_one_that_gets_sized(self) -> None:
        # Quoting h264 while downloading av1 overstated 1080p by more than 2x.
        self.assertEqual(ytdlp.selector_pick(CODECS["formats"][:3])["format_id"], "399")

    def test_webm_loses_to_mp4_because_the_selector_tries_mp4_first(self) -> None:
        self.assertEqual(ytdlp.selector_pick([CODECS["formats"][1]])["format_id"], "248")
        self.assertEqual(ytdlp.selector_pick(CODECS["formats"][:3])["ext"], "mp4")

    def test_the_best_m4a_track_is_the_one_paired_for_a_merge(self) -> None:
        self.assertEqual(ytdlp.best_audio(CODECS)["format_id"], "140")

    def test_the_audio_track_is_included_in_a_merged_size(self) -> None:
        # Video alone understated every merged rendition by the whole audio track.
        self.assertEqual(self._formats()[0]["size_bytes"], 1_190_000_000 + 85_500_000)

    def test_a_merged_size_is_flagged_as_an_estimate(self) -> None:
        self.assertTrue(self._formats()[0]["size_estimated"])

    def test_a_progressive_rendition_is_sized_exactly_with_no_audio_added(self) -> None:
        with patch.object(ytdlp, "probe", return_value=INFO), \
             patch.object(ytdlp, "ffmpeg_available", lambda: True), \
             patch.object(ytdlp, "get_settings", lambda: _settings()):
            options = {item["label_height"]: item for item in ytdlp.download_formats("vid1")["formats"]}
        self.assertEqual(options[360]["size_bytes"], 10_000_000)
        self.assertFalse(options[360]["size_estimated"])


class ProgressParsingTests(unittest.TestCase):
    """Progress comes from a fixed template, not from yt-dlp's human-readable output."""

    def _job(self) -> downloads.DownloadJob:
        return downloads.DownloadJob(id="j1", youtube_video_id="vid1", height=1080, container="mp4")

    def _apply(self, line: str) -> downloads.DownloadJob:
        job = self._job()
        downloads.DownloadManager()._apply_progress(job, line)
        return job

    def test_a_normal_tick_updates_bytes_and_percent(self) -> None:
        job = self._apply("OPENFEED|downloading|5000000|10000000|NA|1200000.0|20")
        self.assertEqual(job.downloaded_bytes, 5_000_000)
        self.assertEqual(job.total_bytes, 10_000_000)
        self.assertAlmostEqual(job.percent, 50.0)
        self.assertEqual(job.eta_seconds, 20)

    def test_an_estimate_is_used_when_the_exact_size_is_unknown(self) -> None:
        job = self._apply("OPENFEED|downloading|2000000|NA|8000000|500000.0|10")
        self.assertEqual(job.total_bytes, 8_000_000)
        self.assertAlmostEqual(job.percent, 25.0)

    def test_finished_means_the_merge_is_still_ahead(self) -> None:
        # yt-dlp reports "finished" once per track, so this is not the end of the job.
        job = self._apply("OPENFEED|finished|10000000|10000000|NA|NA|NA")
        self.assertEqual(job.status, "merging")
        self.assertEqual(job.percent, 99.0)

    def test_percent_never_reaches_100_before_the_file_exists(self) -> None:
        job = self._apply("OPENFEED|downloading|10000000|10000000|NA|NA|0")
        self.assertLess(job.percent, 100.0)

    def test_a_malformed_line_is_ignored(self) -> None:
        job = self._apply("OPENFEED|downloading|oops")
        self.assertEqual(job.downloaded_bytes, 0)
        self.assertEqual(job.status, "queued")


class FormatSelectorTests(unittest.TestCase):
    def test_the_selector_prefers_a_pair_that_remuxes_without_re_encoding(self) -> None:
        selector = downloads.DownloadManager()._format_selector(1080)
        self.assertTrue(selector.startswith("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"))

    def test_it_falls_back_to_any_pair_then_to_a_single_file(self) -> None:
        selector = downloads.DownloadManager()._format_selector(1080)
        self.assertIn("bv*[height<=1080]+ba/", selector)
        self.assertTrue(selector.endswith("b[height<=1080]"))

    def test_no_height_means_no_cap(self) -> None:
        self.assertEqual(downloads.DownloadManager()._format_selector(0), "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b")

    def test_the_command_is_metadata_free_and_writes_where_told(self) -> None:
        from pathlib import Path

        job = downloads.DownloadJob(id="j1", youtube_video_id="vid1", height=1080, container="mkv")
        with patch.object(ytdlp, "binary_path", lambda: "/usr/bin/yt-dlp"), \
             patch.object(ytdlp, "get_settings", lambda: _settings()):
            command = downloads.DownloadManager()._command(job, Path("/tmp/j1.%(ext)s"))
        self.assertEqual(command[command.index("--merge-output-format") + 1], "mkv")
        self.assertEqual(command[command.index("-o") + 1], "/tmp/j1.%(ext)s")
        self.assertIn("--progress-template", command)


class StreamProxyTests(unittest.TestCase):
    """A redirect handed back to the browser would silently un-proxy the stream."""

    def test_the_stream_proxy_follows_redirects_server_side(self) -> None:
        from openfeed import main

        seen: dict[str, object] = {}

        class _Upstream:
            status_code = 200
            headers = {"content-type": "video/mp4", "content-length": "10"}

            def iter_bytes(self):
                yield b"0123456789"

            def close(self):
                pass

        class _Client:
            def build_request(self, *args, **kwargs):
                return object()

            def send(self, request, **kwargs):
                seen.update(kwargs)
                return _Upstream()

        request = SimpleNamespace(headers={})
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main.ytdlp, "playback_stream", return_value={"stream_url": "https://x/1", "stream_type": "video/mp4"}), \
             patch.object(main.outbound, "media_client", return_value=_Client()), \
             patch.object(main, "mark_source_result", lambda *a, **k: None), \
             patch.object(main.netguard, "assert_public_url", lambda url, **k: url):
            main.api_ytdlp_stream(request, "vid1")
        # googlevideo answers with a 302 to another host; passing it through would send
        # the browser to Google directly and leak the address the proxy hides.
        self.assertTrue(seen.get("follow_redirects"))
        self.assertTrue(seen.get("stream"))

    def test_a_range_header_is_forwarded_so_seeking_works(self) -> None:
        from openfeed import main

        captured: dict[str, object] = {}

        class _Upstream:
            status_code = 206
            headers = {"content-range": "bytes 0-9/100", "content-type": "video/mp4"}

            def iter_bytes(self):
                yield b"0123456789"

            def close(self):
                pass

        class _Client:
            def build_request(self, method, url, headers=None, **kwargs):
                captured["headers"] = headers or {}
                return object()

            def send(self, request, **kwargs):
                return _Upstream()

        request = SimpleNamespace(headers={"range": "bytes=0-9"})
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main.ytdlp, "playback_stream", return_value={"stream_url": "https://x/1", "stream_type": "video/mp4"}), \
             patch.object(main.outbound, "media_client", return_value=_Client()), \
             patch.object(main, "mark_source_result", lambda *a, **k: None), \
             patch.object(main.netguard, "assert_public_url", lambda url, **k: url):
            response = main.api_ytdlp_stream(request, "vid1")
        self.assertEqual(captured["headers"].get("range"), "bytes=0-9")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers.get("accept-ranges"), "bytes")


class FilenameTests(unittest.TestCase):
    def test_path_separators_cannot_survive(self) -> None:
        name = downloads.safe_filename("AC/DC: ../etc/passwd", "abc123", "mp4")
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith("[abc123].mp4"))

    def test_an_empty_title_falls_back_to_the_video_id(self) -> None:
        self.assertEqual(downloads.safe_filename("", "xyz", "mkv"), "xyz [xyz].mkv")


class _NoopThread:
    """Stands in for the worker thread so start() can be tested without running yt-dlp."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass


class JobStartTests(unittest.TestCase):
    def _manager(self, **kwargs: object):
        return patch.multiple(
            downloads,
            get_settings=lambda: _settings(**kwargs),
        ), patch.object(downloads.threading, "Thread", _NoopThread)

    def test_an_unknown_container_is_coerced_to_mp4(self) -> None:
        settings_patch, thread_patch = self._manager()
        with settings_patch, thread_patch:
            job = downloads.DownloadManager().start("vid1", 1080, "exe", "Title")
        self.assertEqual(job.container, "mp4")

    def test_mkv_is_honoured(self) -> None:
        settings_patch, thread_patch = self._manager()
        with settings_patch, thread_patch:
            job = downloads.DownloadManager().start("vid1", 2160, "mkv", "Title")
        self.assertEqual(job.container, "mkv")

    def test_concurrency_is_capped(self) -> None:
        manager = downloads.DownloadManager()
        settings_patch, thread_patch = self._manager(download_max_concurrent=1)
        with settings_patch, thread_patch:
            manager.start("vid1", 1080, "mp4", "One")
            with self.assertRaises(RuntimeError):
                manager.start("vid2", 1080, "mp4", "Two")

    def test_job_ids_are_unguessable(self) -> None:
        # The file endpoint is reachable by job id, so it must not be enumerable.
        settings_patch, thread_patch = self._manager()
        with settings_patch, thread_patch:
            manager = downloads.DownloadManager()
            first = manager.start("vid1", 720, "mp4", "One").id
            second = manager.start("vid2", 720, "mp4", "Two").id
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 12)

    def test_a_finished_job_reports_itself_as_ready(self) -> None:
        job = downloads.DownloadJob(id="j1", youtube_video_id="v", height=1080, container="mp4")
        self.assertFalse(job.public()["ready"])
        job.status = "done"
        self.assertTrue(job.public()["ready"])


class CompletedPathTests(unittest.TestCase):
    """The file endpoint must never serve something outside the download directory."""

    def test_a_path_outside_the_download_directory_is_refused(self) -> None:
        job = downloads.DownloadJob(id="j1", youtube_video_id="v", height=720, container="mp4")
        job.status = "done"
        job.path = "/etc/passwd"
        with patch.object(downloads, "get_settings", lambda: _settings(download_dir="/tmp/openfeed-test-downloads")):
            self.assertIsNone(downloads.resolve_completed_path(job))

    def test_an_unfinished_job_has_no_file(self) -> None:
        job = downloads.DownloadJob(id="j1", youtube_video_id="v", height=720, container="mp4")
        job.path = "/tmp/openfeed-test-downloads/j1.mp4"
        with patch.object(downloads, "get_settings", lambda: _settings()):
            self.assertIsNone(downloads.resolve_completed_path(job))


if __name__ == "__main__":
    unittest.main()


class DownloadDestinationTests(unittest.TestCase):
    """A kept download is a library item; a local one is a handoff to the browser."""

    def _job(self, destination: str, status: str = "done") -> downloads.DownloadJob:
        job = downloads.DownloadJob(id="j1", youtube_video_id="v", height=1080, container="mp4", destination=destination)
        job.status = status
        job.finished_at = 1.0
        return job

    def test_the_default_is_a_local_download(self) -> None:
        settings_patch = patch.object(downloads, "get_settings", lambda: _settings())
        with settings_patch, patch.object(downloads.threading, "Thread", _NoopThread):
            job = downloads.DownloadManager().start("vid1", 1080, "mp4", "T")
        self.assertEqual(job.destination, "local")

    def test_an_unknown_destination_falls_back_to_local(self) -> None:
        settings_patch = patch.object(downloads, "get_settings", lambda: _settings())
        with settings_patch, patch.object(downloads.threading, "Thread", _NoopThread):
            job = downloads.DownloadManager().start("vid1", 1080, "mp4", "T", destination="elsewhere")
        self.assertEqual(job.destination, "local")

    def test_server_is_honoured(self) -> None:
        settings_patch = patch.object(downloads, "get_settings", lambda: _settings())
        with settings_patch, patch.object(downloads.threading, "Thread", _NoopThread):
            job = downloads.DownloadManager().start("vid1", 1080, "mp4", "T", destination="server")
        self.assertEqual(job.destination, "server")

    def test_a_kept_download_is_never_discarded(self) -> None:
        # Sweeping it would delete the file the user explicitly asked to keep.
        job = self._job("server")
        job.path = "/data/downloads/x.mp4"
        with patch.object(downloads, "get_settings", lambda: _settings()):
            downloads.DownloadManager()._discard_file(job)
        self.assertEqual(job.path, "/data/downloads/x.mp4")

    def test_a_failed_server_download_is_still_cleaned_up(self) -> None:
        # Only a *finished* keep is protected; a half-written file is just rubbish.
        job = self._job("server", status="error")
        job.path = "/data/downloads/x.mp4"
        with patch.object(downloads, "get_settings", lambda: _settings(download_dir="/nonexistent-openfeed-test")):
            downloads.DownloadManager()._discard_file(job)
        self.assertEqual(job.path, "/data/downloads/x.mp4")  # directory missing, nothing to unlink

    def test_the_sweeper_skips_kept_downloads(self) -> None:
        manager = downloads.DownloadManager()
        keep = self._job("server")
        keep.id = "keep"
        drop = self._job("local")
        drop.id = "drop"
        # Keyed by id, because that is what cleanup() pops by.
        manager._jobs = {keep.id: keep, drop.id: drop}
        with patch.object(downloads, "get_settings", lambda: _settings(download_retention_seconds=60)), \
             patch.object(manager, "_discard_file", lambda job: None):
            manager.cleanup()
        self.assertIn("keep", manager._jobs)
        self.assertNotIn("drop", manager._jobs)

    def test_the_public_payload_reports_the_destination(self) -> None:
        self.assertEqual(self._job("server").public()["destination"], "server")
