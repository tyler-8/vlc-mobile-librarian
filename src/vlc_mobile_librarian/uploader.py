from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from queue import Queue
from threading import Thread

from vlc_mobile_librarian.library import LocalFile
from vlc_mobile_librarian.vlc_client import VLCConnection, authenticate, upload_bytes, upload_file


class UploadStatus(Enum):
    QUEUED = auto()
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
    """Represents one batch upload operation. Stored in Streamlit session_state."""

    files: list[UploadItem]
    events: Queue = field(default_factory=Queue)
    thread: Thread | None = None
    is_done: bool = False
    # Per-file status, updated by the UI thread draining events:
    file_status: dict[str, UploadEvent] = field(default_factory=dict)


def start_upload_job(files: list[UploadItem], conn: VLCConnection) -> UploadJob:
    """Create an UploadJob and start a background daemon thread.

    A fresh requests.Session is created inside the thread (sessions are not
    thread-safe). Files are uploaded sequentially. Progress events are written
    to UploadJob.events for the UI thread to drain.
    """
    job = UploadJob(files=files)

    def _run() -> None:
        # Authenticate with a fresh session - don't share with the UI thread
        try:
            session = authenticate(conn)
        except Exception as e:
            # If we can't connect at all, emit errors for every file
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

        for f in files:
            # Signal that this file is starting
            job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.UPLOADING))

            def _progress(sent: int, total: int, name: str = f.name) -> None:
                job.events.put(
                    UploadEvent(
                        file_name=name,
                        status=UploadStatus.UPLOADING,
                        bytes_sent=sent,
                        bytes_total=total,
                    )
                )

            try:
                if isinstance(f, LocalFile):
                    upload_file(conn, session, f.path, progress_callback=_progress)
                else:
                    upload_bytes(conn, session, f.data, f.name, progress_callback=_progress)
                job.events.put(UploadEvent(file_name=f.name, status=UploadStatus.DONE))
            except Exception as e:
                job.events.put(
                    UploadEvent(
                        file_name=f.name,
                        status=UploadStatus.ERROR,
                        error_msg=str(e),
                    )
                )
                # Continue with remaining files rather than aborting the batch

        job.events.put(_SENTINEL)

    job.thread = Thread(target=_run, daemon=True)
    job.thread.start()
    return job
