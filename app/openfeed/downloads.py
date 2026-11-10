"""Merged high-quality downloads, run as background jobs.

Anything above 720p only exists on YouTube as separate video and audio tracks, so a
download at those resolutions is a yt-dlp fetch of both plus an ffmpeg mux. That takes
long enough that it cannot happen inside a request, so each download is a job: the UI
starts one, polls it for progress, and fetches the finished file when it is ready.

Progress comes from yt-dlp's own --progress-template rather than from parsing its human
readable output, which is formatted for people and changes between releases.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ytdlp
from .config import get_settings

logger = logging.getLogger("openfeed.downloads")

# A machine-readable progress line, emitted once per yt-dlp progress tick. The fields are
# fixed here so a yt-dlp release cannot change the shape underneath us.
_PROGRESS_TEMPLATE = (
    "OPENFEED|%(progress.status)s|%(progress.downloaded_bytes)s"
    "|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s"
    "|%(progress.speed)s|%(progress.eta)s"
)
_PROGRESS_PREFIX = "OPENFEED|"
_SAFE_NAME = re.compile(r"[^\w\s.-]", re.UNICODE)


def _number(raw: str) -> float | None:
    """yt-dlp writes 'NA' for fields it does not know yet."""
    raw = raw.strip()
    if not raw or raw in {"NA", "None"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def safe_filename(title: str, video_id: str, extension: str) -> str:
    cleaned = _SAFE_NAME.sub("", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:120].strip(" .")
    return f"{cleaned or video_id} [{video_id}].{extension}"


@dataclass
class DownloadJob:
    id: str
    youtube_video_id: str
    height: int
    container: str
    title: str = ""
    # 'local' means the browser saves it and the server copy is temporary; 'server'
    # means it joins the library and is kept until the user deletes it.
    destination: str = "local"
    user_key: str = "local"
    # A member's storage quota for kept downloads (0 = none), and the account it counts
    # against: every profile of an account shares one quota.
    quota_bytes: int = 0
    quota_account: str = ""
    status: str = "queued"  # queued | downloading | merging | done | error | cancelled
    percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: int = 0
    filename: str = ""
    path: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    _process: subprocess.Popen | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "youtube_video_id": self.youtube_video_id,
            "height": self.height,
            "quality": f"{self.height}p" if self.height else "",
            "container": self.container,
            "destination": self.destination,
            "title": self.title,
            "status": self.status,
            "percent": round(self.percent, 1),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed_bps": round(self.speed_bps),
            "eta_seconds": self.eta_seconds,
            "filename": self.filename,
            "error": self.error,
            "ready": self.status == "done",
        }


class DownloadManager:
    """Owns every in-flight and finished download for this process."""

    def __init__(self) -> None:
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------

    def _directory(self) -> Path:
        path = Path(get_settings().download_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status in {"queued", "downloading", "merging"})

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[DownloadJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def start(
        self,
        youtube_video_id: str,
        height: int,
        container: str = "mp4",
        title: str = "",
        destination: str = "local",
        user_key: str = "local",
        quota_bytes: int = 0,
        quota_account: str = "",
    ) -> DownloadJob:
        settings = get_settings()
        if self.active_count() >= max(1, settings.download_max_concurrent):
            raise RuntimeError("Too many downloads are already running; try again when one finishes")
        job = DownloadJob(
            id=secrets.token_urlsafe(12),
            youtube_video_id=youtube_video_id,
            height=height,
            container=container if container in {"mp4", "mkv"} else "mp4",
            title=title,
            destination="server" if destination == "server" else "local",
            user_key=user_key,
            quota_bytes=max(0, int(quota_bytes or 0)),
            quota_account=quota_account or user_key.split(":p:", 1)[0],
        )
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job,), name=f"download-{job.id}", daemon=True)
        thread.start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status in {"done", "error", "cancelled"}:
            return False
        job.status = "cancelled"
        process = job._process
        if process and process.poll() is None:
            process.terminate()
        self._discard_file(job)
        return True

    # -- the worker --------------------------------------------------------------

    def _format_selector(self, height: int) -> str:
        """Prefer tracks that mux without re-encoding, then fall back to anything.

        The mp4/m4a pair remuxes into an MP4 in seconds; the bare `bv*+ba` fallback may
        pull VP9/Opus, which is why an MKV container is offered too.
        """
        cap = f"[height<={height}]" if height else ""
        return (
            f"bv*{cap}[ext=mp4]+ba[ext=m4a]/"
            f"bv*{cap}+ba/"
            f"b{cap}"
        )

    def _command(self, job: DownloadJob, target: Path) -> list[str]:
        return [
            ytdlp.binary_path(),
            *ytdlp.base_args(),
            "-f", self._format_selector(job.height),
            "--merge-output-format", job.container,
            "--newline",
            "--progress",
            "--progress-template", _PROGRESS_TEMPLATE,
            "--no-part",
            "-o", str(target),
            "--", ytdlp.watch_url(job.youtube_video_id),
        ]

    def _stall_watchdog(self, job: DownloadJob) -> None:
        """Terminate a download whose byte counter has stopped moving.

        A stalled peer connection can park yt-dlp inside the reader loop forever: while
        it waits on the socket it prints nothing, so `for line in process.stdout` never
        returns and the job sits in `downloading` indefinitely. This watchdog runs the
        other way around — it notices silence and ends the process. Merging counts as
        progress-free by design, so it is not policed here.
        """
        grace = max(0, int(get_settings().download_stall_seconds))
        if not grace:
            return
        last_bytes = -1
        last_change = time.monotonic()
        while True:
            time.sleep(min(15, max(2, grace // 8)))
            if job.status != "downloading":
                return
            if job.downloaded_bytes != last_bytes:
                last_bytes = job.downloaded_bytes
                last_change = time.monotonic()
                continue
            if time.monotonic() - last_change <= grace:
                continue
            process = job._process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except Exception:
                    process.kill()
            if job.status == "downloading":
                job.status = "error"
                job.error = f"Download stalled: no progress for {grace}s"
                self._discard_file(job)
            return

    def _apply_progress(self, job: DownloadJob, line: str) -> None:
        parts = line[len(_PROGRESS_PREFIX):].split("|")
        if len(parts) < 6:
            return
        status, downloaded, total, estimate, speed, eta = parts[:6]
        got = _number(downloaded)
        size = _number(total) or _number(estimate)
        if got is not None:
            job.downloaded_bytes = int(got)
        if size:
            job.total_bytes = int(size)
            job.percent = min(99.0, got / size * 100) if got else 0.0
        job.speed_bps = _number(speed) or 0.0
        job.eta_seconds = int(_number(eta) or 0)
        if status.strip() == "finished":
            # yt-dlp reports "finished" once per track, so the merge may still be ahead.
            job.status = "merging"
            job.percent = 99.0
        elif job.status == "queued":
            job.status = "downloading"

    def _run(self, job: DownloadJob) -> None:
        try:
            if not ytdlp.available():
                raise RuntimeError("yt-dlp is not installed in this container")
            directory = self._directory()
            # yt-dlp fills in the real extension after the merge, so the job id alone is
            # the stem and the finished file is found by globbing it back.
            target = directory / f"{job.id}.%(ext)s"
            job.status = "downloading"
            process = subprocess.Popen(
                self._command(job, target),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            job._process = process
            threading.Thread(
                target=self._stall_watchdog,
                args=(job,),
                name=f"download-stall-{job.id}",
                daemon=True,
            ).start()
            tail: list[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if line.startswith(_PROGRESS_PREFIX):
                    self._apply_progress(job, line)
                elif line.strip():
                    tail.append(line.strip())
                    del tail[:-12]
            code = process.wait()

            if job.status == "cancelled":
                self._discard_file(job)
                return
            if code != 0:
                raise RuntimeError(tail[-1] if tail else f"yt-dlp exited {code}")

            produced = sorted(directory.glob(f"{job.id}.*"))
            if not produced:
                raise RuntimeError("yt-dlp finished but produced no file")
            path = produced[0]
            extension = path.suffix.lstrip(".") or job.container
            job.filename = safe_filename(job.title or job.youtube_video_id, job.youtube_video_id, extension)

            # Keep the job-unique storage path for retained downloads too. A friendly
            # title/video filename is shared by different owners (and repeat jobs),
            # so renaming to it would overwrite another library copy. job.filename
            # remains the friendly Content-Disposition name, independent of storage.
            job.path = str(path)
            job.total_bytes = path.stat().st_size
            job.downloaded_bytes = job.total_bytes
            job.percent = 100.0
            job.status = "done"

            if job.destination == "server":
                from . import db  # local import keeps this module importable without a database

                if job.quota_bytes:
                    used = account_usage_bytes(job.quota_account, (job.user_key, job.youtube_video_id))
                    if used + job.total_bytes > job.quota_bytes:
                        raise RuntimeError(
                            "This would go over your storage quota on this server "
                            f"({_size_text(used)} of {_size_text(job.quota_bytes)} used, this file is {_size_text(job.total_bytes)}). "
                            "Delete some downloads, or download it to this device instead."
                        )
                # Identical bytes already kept for someone else share one file on disk.
                # Each owner still has their own row (and is charged the full size), and
                # deleting a row unlinks the file only once nothing references it.
                shared = identical_kept_file(path, job.youtube_video_id)
                if shared is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    path = shared
                    job.path = str(path)
                db.register_download(
                    job.youtube_video_id,
                    job.title or job.youtube_video_id,
                    job.filename,
                    str(path),
                    container=extension,
                    height=job.height,
                    quality=f"{job.height}p" if job.height else "",
                    size_bytes=job.total_bytes,
                    user_key=job.user_key,
                )
        except Exception as exc:  # noqa: BLE001 - the message is surfaced to the user
            logger.warning("download job %s failed: %s", job.id, exc)
            job.status = "error"
            job.error = str(exc)
            self._discard_file(job)
        finally:
            job.finished_at = time.time()
            job._process = None
            self.cleanup()

    # -- housekeeping ------------------------------------------------------------

    def _discard_file(self, job: DownloadJob) -> None:
        """Remove a job's working file. Never touches a kept download.

        A server-side download is the user's library copy — sweeping it on a timer would
        delete the thing they explicitly asked to keep.
        """
        if job.destination == "server" and job.status == "done":
            return
        directory = Path(get_settings().download_dir)
        if not directory.exists():
            return
        for path in directory.glob(f"{job.id}.*"):
            if job.destination == "server":
                # Registration may have committed before its connection failed. Never
                # discard a possibly registered library file on an ambiguous failure.
                unlink_unreferenced_download(path)
                continue
            try:
                path.unlink()
            except OSError:
                pass
        job.path = ""

    def cleanup(self) -> int:
        """Drop finished jobs past their retention window, files included.

        Only applies to `local` downloads: those are a handoff to the browser, and the
        copy on disk is scratch. Anything the user asked to keep on the server is a
        library item and is removed only when they delete it.
        """
        retention = max(60, get_settings().download_retention_seconds)
        cutoff = time.time() - retention
        removed = 0
        with self._lock:
            stale = [
                job for job in self._jobs.values()
                if job.finished_at and job.finished_at < cutoff
                and not (job.destination == "server" and job.status == "done")
            ]
        for job in stale:
            self._discard_file(job)
            with self._lock:
                self._jobs.pop(job.id, None)
            removed += 1
        return removed

    def purge_all(self) -> None:
        """Used by tests and by a clean shutdown."""
        for job in self.list_jobs():
            self.cancel(job.id)
            self._discard_file(job)
        with self._lock:
            self._jobs.clear()


manager = DownloadManager()


def unlink_unreferenced_download(path: Path) -> bool:
    """Unlink only after proving no library row references this file.

    Includes other owners and legacy path aliases. The database lock remains held
    through unlink so a concurrent library mutation cannot change the reference set.
    Uncertain cleanup leaves an orphan rather than destroying a library copy.
    """
    from . import db

    try:
        with db.locked_download_paths() as references:
            resolved = path.resolve()
            for reference in references:
                if not reference or Path(reference).resolve() == resolved:
                    return False
            path.unlink()
        return True
    except Exception as exc:  # noqa: BLE001 - DB or filesystem uncertainty must retain the file
        logger.warning("retaining download file %s: could not prove safe cleanup: %s", path, exc)
        return False


def disk_usage() -> dict[str, Any]:
    """What the download directory is currently costing, for the status page."""
    directory = Path(get_settings().download_dir)
    if not directory.exists():
        return {"files": 0, "bytes": 0, "path": str(directory), "free_bytes": None, "total_bytes": None}
    files = [path for path in directory.iterdir() if path.is_file()]
    try:
        disk = shutil.disk_usage(directory)
        free, total = disk.free, disk.total
    except OSError:
        free, total = None, None
    return {
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "path": str(directory),
        "free_bytes": free,
        "total_bytes": total,
    }


def ffmpeg_version() -> str:
    if not shutil.which("ffmpeg"):
        return ""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (result.stdout or "").splitlines()
    return first[0].strip() if first and result.returncode == 0 else ""


def resolve_completed_path(job: DownloadJob) -> Path | None:
    """The finished file, but only if it really sits inside the download directory.

    Job ids are generated here so a traversal is not reachable through the API, but the
    check is cheap and this function is what hands a path to a file response.
    """
    if job.status != "done" or not job.path:
        return None
    directory = Path(get_settings().download_dir).resolve()
    try:
        path = Path(job.path).resolve()
        path.relative_to(directory)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def account_usage_bytes(account_key: str, replacing: tuple[str, str] | None = None) -> int:
    """Bytes of kept downloads charged to an account: its own rows and its profiles'.

    replacing=(user_key, video id) leaves out the row a new download of that video will
    replace (register_download keeps one row per owner and video)."""
    from . import db

    key = str(account_key or "local").split(":p:", 1)[0]
    skip_owner, skip_video = replacing or ("", "")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) AS used FROM downloads
                WHERE (user_key = %s OR user_key LIKE %s)
                  AND NOT (user_key = %s AND youtube_video_id = %s)
                """,
                (key, db.escape_like_pattern(key) + ":p:%", skip_owner, skip_video),
            )
            row = cur.fetchone() or {}
    return int(row.get("used") or 0)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identical_kept_file(path: Path, youtube_video_id: str) -> Path | None:
    """A kept file of the same video with exactly these bytes, if one exists.

    Candidates are other library rows for the video whose file is the same size; only
    those are hashed. The answer never decides whether a download runs (that would tell
    one user what another kept), only where its finished bytes are stored.
    """
    from . import db

    try:
        size = path.stat().st_size
        directory = Path(get_settings().download_dir).resolve()
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT path FROM downloads WHERE youtube_video_id = %s", (youtube_video_id,))
                candidates = [str(row["path"]) for row in cur.fetchall() if row.get("path")]
        own_digest = ""
        for raw in candidates:
            candidate = Path(raw).resolve()
            if candidate == path.resolve() or not candidate.is_file():
                continue
            try:
                candidate.relative_to(directory)
            except ValueError:
                continue
            if candidate.stat().st_size != size:
                continue
            own_digest = own_digest or _file_digest(path)
            if _file_digest(candidate) == own_digest:
                return candidate
    except Exception as exc:  # noqa: BLE001 - keeping a second copy is always safe
        logger.info("could not compare a new download with kept files: %s", exc)
    return None


def _size_text(value: int) -> str:
    size = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if size >= 10 or unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


__all__ = [
    "DownloadJob",
    "DownloadManager",
    "manager",
    "disk_usage",
    "ffmpeg_version",
    "resolve_completed_path",
    "safe_filename",
    "account_usage_bytes",
    "identical_kept_file",
]
