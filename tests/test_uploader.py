"""Tests for vlc_mobile_librarian.uploader - background thread upload job."""

from __future__ import annotations

from pathlib import Path
from queue import Empty
from unittest.mock import MagicMock, patch

from vlc_mobile_librarian.models import LocalFile
from vlc_mobile_librarian.uploader import (
    InMemoryFile,
    UploadEvent,
    UploadJob,
    UploadStatus,
    start_upload_job,
)
from vlc_mobile_librarian.vlc_client import VLCConnection, VLCConnectionError, VLCUploadError

CONN = VLCConnection(host="192.168.1.1")


def _collect_events(job: UploadJob, timeout: float = 5.0) -> list[UploadEvent]:
    """Join the upload thread and drain all events from the queue."""
    job.thread.join(timeout=timeout)
    events: list[UploadEvent] = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except Empty:
            break
    return events


def _statuses(events: list[UploadEvent], name: str) -> list[UploadStatus]:
    return [e.status for e in events if e.file_name == name]


# ── dataclasses ────────────────────────────────────────────────────────────────


def test_upload_event_defaults():
    ev = UploadEvent(file_name="x.mp3", status=UploadStatus.QUEUED)
    assert ev.bytes_sent == 0
    assert ev.bytes_total == 0
    assert ev.error_msg == ""


def test_in_memory_file_fields():
    f = InMemoryFile(data=b"abc", name="pl.m3u8", size=3)
    assert f.data == b"abc"
    assert f.title == ""
    assert f.duration_ms == 0


def test_upload_job_initial_state():
    job = UploadJob(files=[])
    assert job.is_done is False
    assert job.thread is None
    assert job.file_status == {}


# ── start_upload_job - LocalFile success ──────────────────────────────────────


def test_local_file_upload_success(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"audio")
    local = LocalFile(path=f, name="song.mp3", size=5)

    mock_session = MagicMock()
    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=mock_session) as mock_auth,
        patch("vlc_mobile_librarian.uploader.upload_file") as mock_upload,
    ):
        job = start_upload_job([local], CONN)
        events = _collect_events(job)

    mock_auth.assert_called_once_with(CONN)
    mock_upload.assert_called_once()
    assert mock_upload.call_args[0][2] == f  # file path passed through

    named = [e for e in events if e.file_name == "song.mp3"]
    statuses = [e.status for e in named]
    assert UploadStatus.UPLOADING in statuses
    assert UploadStatus.DONE in statuses


# ── start_upload_job - InMemoryFile success ────────────────────────────────────


def test_in_memory_file_upload_success():
    mem = InMemoryFile(data=b"#EXTM3U\n", name="playlist.m3u8", size=8)

    mock_session = MagicMock()
    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=mock_session),
        patch("vlc_mobile_librarian.uploader.upload_bytes") as mock_upload,
    ):
        job = start_upload_job([mem], CONN)
        events = _collect_events(job)

    mock_upload.assert_called_once()
    call_args = mock_upload.call_args
    assert call_args[0][2] == b"#EXTM3U\n"  # data
    assert call_args[0][3] == "playlist.m3u8"  # filename

    statuses = _statuses(events, "playlist.m3u8")
    assert UploadStatus.UPLOADING in statuses
    assert UploadStatus.DONE in statuses


# ── start_upload_job - multiple files ─────────────────────────────────────────


def test_multiple_files_all_succeed(tmp_path: Path):
    f1 = tmp_path / "a.mp3"
    f1.write_bytes(b"a")
    f2 = tmp_path / "b.mp3"
    f2.write_bytes(b"b")
    local1 = LocalFile(path=f1, name="a.mp3", size=1)
    local2 = LocalFile(path=f2, name="b.mp3", size=1)

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.upload_file"),
    ):
        job = start_upload_job([local1, local2], CONN)
        events = _collect_events(job)

    assert UploadStatus.DONE in _statuses(events, "a.mp3")
    assert UploadStatus.DONE in _statuses(events, "b.mp3")


# ── start_upload_job - auth failure ───────────────────────────────────────────


def test_auth_failure_emits_error_for_all_files(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"audio")
    local = LocalFile(path=f, name="song.mp3", size=5)

    with patch(
        "vlc_mobile_librarian.uploader.authenticate", side_effect=VLCConnectionError("no route")
    ):
        job = start_upload_job([local], CONN)
        events = _collect_events(job)

    error_events = [e for e in events if e.status == UploadStatus.ERROR and e.file_name]
    assert len(error_events) == 1
    assert "no route" in error_events[0].error_msg


def test_auth_failure_multiple_files_all_get_errors(tmp_path: Path):
    files = []
    for i in range(3):
        f = tmp_path / f"track{i}.mp3"
        f.write_bytes(b"x")
        files.append(LocalFile(path=f, name=f"track{i}.mp3", size=1))

    with patch(
        "vlc_mobile_librarian.uploader.authenticate", side_effect=VLCConnectionError("timeout")
    ):
        job = start_upload_job(files, CONN)
        events = _collect_events(job)

    error_events = [e for e in events if e.status == UploadStatus.ERROR and e.file_name]
    assert len(error_events) == 3


# ── start_upload_job - upload error continues batch ───────────────────────────


def test_upload_error_continues_with_remaining_files(tmp_path: Path):
    f1 = tmp_path / "fails.mp3"
    f1.write_bytes(b"x")
    f2 = tmp_path / "ok.mp3"
    f2.write_bytes(b"x")
    local1 = LocalFile(path=f1, name="fails.mp3", size=1)
    local2 = LocalFile(path=f2, name="ok.mp3", size=1)

    def _upload_side_effect(conn, session, path, progress_callback=None):
        if path.name == "fails.mp3":
            raise VLCUploadError("disk full")

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.upload_file", side_effect=_upload_side_effect),
    ):
        job = start_upload_job([local1, local2], CONN)
        events = _collect_events(job)

    assert UploadStatus.ERROR in _statuses(events, "fails.mp3")
    assert UploadStatus.DONE in _statuses(events, "ok.mp3")


# ── sentinel ───────────────────────────────────────────────────────────────────


def test_sentinel_emitted_after_all_files(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    local = LocalFile(path=f, name="song.mp3", size=1)

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.upload_file"),
    ):
        job = start_upload_job([local], CONN)
        events = _collect_events(job)

    # Sentinel is the last event: file_name="" status=DONE
    sentinel = events[-1]
    assert sentinel.file_name == ""
    assert sentinel.status == UploadStatus.DONE


def test_sentinel_emitted_after_auth_failure():
    mem = InMemoryFile(data=b"x", name="f.m3u8", size=1)

    with patch("vlc_mobile_librarian.uploader.authenticate", side_effect=VLCConnectionError("err")):
        job = start_upload_job([mem], CONN)
        events = _collect_events(job)

    sentinel = events[-1]
    assert sentinel.file_name == ""
    assert sentinel.status == UploadStatus.DONE
