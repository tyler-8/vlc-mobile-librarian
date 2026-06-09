from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from queue import Queue
from threading import Lock, Thread, local

from vlc_mobile_librarian.library import LocalFile
from vlc_mobile_librarian.transcode import (
    TranscodeOptions,
    cached_transcode,
    prune_cache,
    requires_transcode,
    transcoded_name,
)
from vlc_mobile_librarian.vlc_client import VLCConnection, authenticate, upload_bytes, upload_file

logger = logging.getLogger(__name__)


class UploadStatus(Enum):
    QUEUED = auto()
    TRANSCODING = auto()
    UPLOADING = auto()
    DONE = auto()
    ERROR = auto()


@dataclass
class UploadEvent:
    """A single progress event written to the queue by the upload thread."""

    file_name: str
    status: UploadStatus
    bytes_sent: int = 0
    bytes_total: int = 0
    error_msg: str = ""


# Sentinel: file_name="" signals the entire batch is complete
_SENTINEL = UploadEvent(file_name="", status=UploadStatus.DONE)


@dataclass(frozen=True)
class InMemoryFile:
    """A synthetically generated file uploaded from memory (e.g. an .m3u8 playlist)."""

    data: bytes
    name: str
    size: int
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0


# Either a file on disk or an in-memory blob
UploadItem = LocalFile | InMemoryFile


@dataclass
class UploadJob:
    """Represents one batch upload operation. Held in the UI's per-tab state."""

    files: list[UploadItem]
    events: Queue = field(default_factory=Queue)
    thread: Thread | None = None
    is_done: bool = False
    # Per-file status, updated by the UI thread draining events:
    file_status: dict[str, UploadEvent] = field(default_factory=dict)


def start_upload_job(
    files: list[UploadItem],
    conn: VLCConnection,
    transcode: TranscodeOptions | None = None,
    concurrency: int = 2,
) -> UploadJob:
    """Create an UploadJob and start a background daemon thread.

    Files are uploaded on a pool of `concurrency` worker threads. Each worker
    gets its own lazily-authenticated requests.Session (sessions are not safe for
    concurrent requests). Progress events are written to UploadJob.events for the
    UI thread to drain.

    If `transcode` is enabled, lossless LocalFiles are re-encoded to Opus (and
    uploaded under a `.opus` filename); already-lossy files and in-memory items
    pass through unchanged. Encodes run in parallel on a thread pool (each ffmpeg
    is its own process, so worker threads just block on it) sized by
    `TranscodeOptions.resolved_workers()`, while the upload workers consume each
    encode as it's needed - an upload never waits on an encode that a transcode
    worker has already finished ahead of time.
    """
    job = UploadJob(files=files)
    tc_on = bool(transcode and transcode.enabled)
    logger.info(
        "upload job: %d item(s), transcode=%s%s → %s:%d",
        len(files),
        "on" if tc_on else "off",
        f" ({transcode.bitrate_kbps}k, {transcode.resolved_workers()} workers)" if tc_on else "",
        conn.host,
        conn.port,
    )

    def _run() -> None:
        batch_start = time.perf_counter()
        # Probe connectivity up front so a dead connection fails the whole batch
        # fast rather than re-trying auth on every worker. Each worker creates its
        # own session below - sessions aren't safe for concurrent requests.
        try:
            authenticate(conn)
        except Exception as e:
            # If we can't connect at all, emit errors for every file
            logger.error("upload job aborted - authentication failed: %s", e)
            for f in files:
                job.events.put(
                    UploadEvent(
                        file_name=f.name,
                        status=UploadStatus.ERROR,
                        error_msg=str(e),
                    )
                )
            job.events.put(_SENTINEL)
            return

        # One authenticated session per upload worker thread, created lazily.
        tlocal = local()

        def _session():
            s = getattr(tlocal, "session", None)
            if s is None:
                s = authenticate(conn)
                tlocal.session = s
            return s

        def _encode(f: LocalFile) -> Path:
            # Runs on a pool worker; emit TRANSCODING when this encode actually
            # starts (queued files stay pending until a worker frees up).
            job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.TRANSCODING))
            return cached_transcode(f.path, transcode)

        # Kick off all lossless encodes up front so they run in parallel while
        # the upload loop works through the batch.
        futures: dict[str, Future[Path]] = {}
        executor: ThreadPoolExecutor | None = None
        if transcode and transcode.enabled:
            to_encode = [
                f for f in files if isinstance(f, LocalFile) and requires_transcode(f.path)
            ]
            if to_encode:
                workers = transcode.resolved_workers()
                logger.info("queuing %d encode(s) across %d worker(s)", len(to_encode), workers)
                executor = ThreadPoolExecutor(max_workers=workers)
                for f in to_encode:
                    futures[f.name] = executor.submit(_encode, f)

        counts = {"done": 0, "error": 0}
        counts_lock = Lock()

        def _upload_one(f: UploadItem) -> None:
            def _progress(sent: int, total: int) -> None:
                job.events.put(
                    UploadEvent(
                        file_name=f.name,
                        status=UploadStatus.UPLOADING,
                        bytes_sent=sent,
                        bytes_total=total,
                    )
                )

            t0 = time.perf_counter()
            try:
                session = _session()
                if isinstance(f, LocalFile) and f.name in futures:
                    # Block on the (likely already finished) parallel encode
                    logger.debug("awaiting encode: %s", f.name)
                    opus_path = futures[f.name].result()
                    upload_name = transcoded_name(f.name)
                    logger.info("upload start: %s (transcoded)", upload_name)
                    job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.UPLOADING))
                    upload_file(
                        conn,
                        session,
                        opus_path,
                        progress_callback=_progress,
                        upload_name=upload_name,
                    )
                elif isinstance(f, LocalFile):
                    logger.info("upload start: %s", f.name)
                    job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.UPLOADING))
                    upload_file(conn, session, f.path, progress_callback=_progress)
                else:
                    logger.info("upload start: %s (generated)", f.name)
                    job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.UPLOADING))
                    upload_bytes(conn, session, f.data, f.name, progress_callback=_progress)
                job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.DONE))
                with counts_lock:
                    counts["done"] += 1
                    finished = counts["done"] + counts["error"]
                logger.info(
                    "upload done:  %s in %.1fs (%d/%d)",
                    f.name,
                    time.perf_counter() - t0,
                    finished,
                    len(files),
                )
            except Exception as e:
                with counts_lock:
                    counts["error"] += 1
                logger.exception("upload FAILED: %s - %s", f.name, e)
                job.events.put(
                    UploadEvent(
                        file_name=f.name,
                        status=UploadStatus.ERROR,
                        error_msg=str(e),
                    )
                )
                # Continue with remaining files rather than aborting the batch

        upload_executor = ThreadPoolExecutor(max_workers=max(1, concurrency))
        try:
            # list() forces the map to drain so any straggler exceptions surface
            # before we move on to cache pruning.
            list(upload_executor.map(_upload_one, files))
        finally:
            upload_executor.shutdown(wait=True)
            if executor is not None:
                executor.shutdown(wait=True)

        done_count = counts["done"]
        error_count = counts["error"]

        logger.info(
            "upload job complete: %d ok, %d failed in %.1fs",
            done_count,
            error_count,
            time.perf_counter() - batch_start,
        )

        # Evict least-recently-used encodes if the cache exceeds the cap. Safe
        # here - all uploads for this batch are finished, and the files we just
        # wrote have the freshest mtime so they won't be the ones evicted.
        if transcode and transcode.enabled and transcode.cache_cap_bytes > 0:
            prune_cache(transcode.cache_cap_bytes)

        job.events.put(_SENTINEL)

    job.thread = Thread(target=_run, daemon=True)
    job.thread.start()
    return job
