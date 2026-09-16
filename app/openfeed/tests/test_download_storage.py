from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import db, downloads, ytdlp


class RetainedDownloadStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"OPENFEED_DOWNLOAD_DIR": str(self.directory)}))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        self.enterContext(patch.object(ytdlp, "available", return_value=True))
        self.manager = downloads.DownloadManager()
        self.enterContext(patch.object(self.manager, "_command", side_effect=lambda job, target: ["yt-dlp", "-o", str(target)]))

    def produce(self, command, **kwargs):
        path = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        path.write_bytes(path.stem.encode())
        return SimpleNamespace(stdout=[], wait=lambda: 0)

    def job(self, job_id: str, owner: str) -> downloads.DownloadJob:
        return downloads.DownloadJob(id=job_id, youtube_video_id="same-video", title="Same title",
                                     height=720, container="mp4", destination="server", user_key=owner)

    def test_two_owners_and_repeat_owner_jobs_never_overwrite_each_other_or_legacy_file(self) -> None:
        filename = downloads.safe_filename("Same title", "same-video", "mp4")
        legacy = self.directory / filename
        legacy.write_bytes(b"legacy-shared-copy")
        jobs = [self.job("first-job", "owner-1"), self.job("second-job", "owner-2"),
                self.job("replacement-job", "owner-1")]
        with patch.object(downloads.subprocess, "Popen", side_effect=self.produce), \
             patch.object(db, "register_download", return_value={}) as register:
            for job in jobs:
                self.manager._run(job)
        self.assertEqual(len({job.path for job in jobs}), 3)
        self.assertEqual(register.call_count, len(jobs))
        for job, call in zip(jobs, register.call_args_list):
            self.assertEqual(job.status, "done")
            self.assertEqual(job.filename, filename)
            self.assertEqual(Path(job.path).read_bytes(), job.id.encode())
            self.assertEqual(call.args[2:4], (filename, job.path))
            self.assertEqual(call.kwargs["user_key"], job.user_key)
            self.assertEqual(downloads.resolve_completed_path(job), Path(job.path))
        self.assertEqual(legacy.read_bytes(), b"legacy-shared-copy")

    def test_uncertain_registration_failure_does_not_discard_a_referenced_job_file(self) -> None:
        job = self.job("possibly-committed-job", "owner-1")
        path = self.directory / f"{job.id}.mp4"

        @contextmanager
        def references():
            yield [str(path)]

        with patch.object(downloads.subprocess, "Popen", side_effect=self.produce), \
             patch.object(db, "register_download", side_effect=RuntimeError("test connection lost after commit")), \
             patch.object(db, "locked_download_paths", references):
            self.manager._run(job)
        self.assertEqual(job.status, "error")
        self.assertEqual(path.read_bytes(), job.id.encode())

    def test_uncertain_reference_lookup_retains_file(self) -> None:
        path = self.directory / "retained.mp4"
        path.write_bytes(b"retained")
        with patch.object(db, "locked_download_paths", side_effect=RuntimeError("test database unavailable")):
            self.assertFalse(downloads.unlink_unreferenced_download(path))
        self.assertEqual(path.read_bytes(), b"retained")

    def test_path_alias_reference_prevents_unlink_and_lock_spans_cleanup(self) -> None:
        path = self.directory / "retained.mp4"
        path.write_bytes(b"retained")
        alias = self.directory / "alias.mp4"
        alias.symlink_to(path)

        @contextmanager
        def referenced():
            yield [str(alias)]

        with patch.object(db, "locked_download_paths", referenced):
            self.assertFalse(downloads.unlink_unreferenced_download(path))
        self.assertTrue(path.is_file())

        @contextmanager
        def unreferenced():
            self.assertTrue(path.is_file())
            yield []
            # File removal must finish before the reference lock is released.
            self.assertFalse(path.exists())

        with patch.object(db, "locked_download_paths", unreferenced):
            self.assertTrue(downloads.unlink_unreferenced_download(path))


if __name__ == "__main__":
    unittest.main()
