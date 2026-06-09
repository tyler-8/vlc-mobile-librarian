"""Tests for vlc_mobile_librarian.uploader - background thread upload job."""

from __future__ import annotations

import threading
from pathlib import Path
from queue import Empty
from unittest.mock import MagicMock, patch

from vlc_mobile_librarian.models import LocalFile
from vlc_mobile_librarian.transcode import TranscodeOptions
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

    # Authenticated at least once (a connectivity probe + per-worker sessions).
    assert mock_auth.call_count >= 1
    assert mock_auth.call_args == ((CONN,),)
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


def test_uploads_run_concurrently(tmp_path: Path):
    # A Barrier of 3 only releases once 3 uploads are in-flight simultaneously.
    # If uploads ran sequentially, barrier.wait() would time out and raise,
    # turning every file into an ERROR and failing the DONE assertions below.
    files = []
    for i in range(3):
        p = tmp_path / f"s{i}.mp3"
        p.write_bytes(b"x")
        files.append(LocalFile(path=p, name=f"s{i}.mp3", size=1))

    barrier = threading.Barrier(3, timeout=5)

    def fake_upload(conn, session, path, progress_callback=None):
        barrier.wait()  # blocks until all 3 uploads are running concurrently

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.upload_file", side_effect=fake_upload),
    ):
        job = start_upload_job(files, CONN, concurrency=3)
        events = _collect_events(job, timeout=10)

    for i in range(3):
        assert UploadStatus.DONE in _statuses(events, f"s{i}.mp3")


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


# ── transcoding ────────────────────────────────────────────────────────────────


def test_transcode_lossless_encodes_and_uploads_opus_name(tmp_path: Path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"flac")
    local = LocalFile(path=f, name="song.flac", size=4)
    opus = tmp_path / "cached.opus"
    opus.write_bytes(b"opus")

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.cached_transcode", return_value=opus) as mock_tc,
        patch("vlc_mobile_librarian.uploader.upload_file") as mock_upload,
    ):
        job = start_upload_job(
            [local], CONN, transcode=TranscodeOptions(enabled=True, bitrate_kbps=128)
        )
        events = _collect_events(job)

    mock_tc.assert_called_once()
    # Uploads the transcoded temp file, but under the .opus display name
    assert mock_upload.call_args[0][2] == opus
    assert mock_upload.call_args.kwargs["upload_name"] == "song.opus"

    statuses = _statuses(events, "song.flac")
    assert UploadStatus.TRANSCODING in statuses
    assert UploadStatus.DONE in statuses


def test_transcode_runs_encodes_in_parallel(tmp_path: Path):
    # A Barrier of N only releases once N encodes are in-flight simultaneously.
    # If encodes ran sequentially, barrier.wait() would time out and raise,
    # turning every file into an ERROR and failing the DONE assertions below.
    files = []
    for i in range(3):
        p = tmp_path / f"s{i}.flac"
        p.write_bytes(b"x")
        files.append(LocalFile(path=p, name=f"s{i}.flac", size=1))

    barrier = threading.Barrier(3, timeout=5)

    def fake_tc(path: Path, opts: TranscodeOptions) -> Path:
        barrier.wait()  # blocks until all 3 encodes are running concurrently
        out = tmp_path / f"{path.stem}.cached"
        out.write_bytes(b"o")
        return out

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.cached_transcode", side_effect=fake_tc),
        patch("vlc_mobile_librarian.uploader.upload_file") as mock_upload,
    ):
        job = start_upload_job(files, CONN, transcode=TranscodeOptions(enabled=True, max_workers=3))
        events = _collect_events(job, timeout=10)

    for i in range(3):
        assert UploadStatus.DONE in _statuses(events, f"s{i}.flac")
    uploaded_names = {c.kwargs["upload_name"] for c in mock_upload.call_args_list}
    assert uploaded_names == {"s0.opus", "s1.opus", "s2.opus"}


def test_transcode_skips_already_lossy_files(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"mp3")
    local = LocalFile(path=f, name="song.mp3", size=3)

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.cached_transcode") as mock_tc,
        patch("vlc_mobile_librarian.uploader.upload_file") as mock_upload,
    ):
        job = start_upload_job([local], CONN, transcode=TranscodeOptions(enabled=True))
        _collect_events(job)

    mock_tc.assert_not_called()
    # Uploaded unchanged - original path, no upload_name override
    assert mock_upload.call_args[0][2] == f
    assert mock_upload.call_args.kwargs.get("upload_name") is None


def test_transcode_disabled_does_not_encode_lossless(tmp_path: Path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"flac")
    local = LocalFile(path=f, name="song.flac", size=4)

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.cached_transcode") as mock_tc,
        patch("vlc_mobile_librarian.uploader.upload_file") as mock_upload,
    ):
        job = start_upload_job([local], CONN, transcode=TranscodeOptions(enabled=False))
        _collect_events(job)

    mock_tc.assert_not_called()
    assert mock_upload.call_args[0][2] == f


def test_cache_pruned_after_batch_when_cap_set(tmp_path: Path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"flac")
    local = LocalFile(path=f, name="song.flac", size=4)
    opus = tmp_path / "cached.opus"
    opus.write_bytes(b"opus")

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.cached_transcode", return_value=opus),
        patch("vlc_mobile_librarian.uploader.upload_file"),
        patch("vlc_mobile_librarian.uploader.prune_cache") as mock_prune,
    ):
        job = start_upload_job(
            [local], CONN, transcode=TranscodeOptions(enabled=True, cache_cap_bytes=5000)
        )
        _collect_events(job)

    mock_prune.assert_called_once_with(5000)


def test_cache_not_pruned_when_cap_zero(tmp_path: Path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"flac")
    local = LocalFile(path=f, name="song.flac", size=4)
    opus = tmp_path / "cached.opus"
    opus.write_bytes(b"opus")

    with (
        patch("vlc_mobile_librarian.uploader.authenticate", return_value=MagicMock()),
        patch("vlc_mobile_librarian.uploader.cached_transcode", return_value=opus),
        patch("vlc_mobile_librarian.uploader.upload_file"),
        patch("vlc_mobile_librarian.uploader.prune_cache") as mock_prune,
    ):
        job = start_upload_job(
            [local], CONN, transcode=TranscodeOptions(enabled=True, cache_cap_bytes=0)
        )
        _collect_events(job)

    mock_prune.assert_not_called()


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
