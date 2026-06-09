"""Tests for vlc_mobile_librarian.transcode - lossless→Opus transcoding helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from vlc_mobile_librarian import transcode
from vlc_mobile_librarian.transcode import (
    TranscodeError,
    TranscodeOptions,
    cache_file_count,
    cache_size_bytes,
    cached_transcode,
    clear_cache,
    default_workers,
    ffmpeg_available,
    prune_cache,
    requires_transcode,
    transcode_to_opus,
    transcoded_name,
)

_has_ffmpeg = ffmpeg_available()
_skip_no_ffmpeg = pytest.mark.skipif(not _has_ffmpeg, reason="ffmpeg not installed")


# ── pure helpers ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("/music/a.flac", True),
        ("a.FLAC", True),
        ("b.wav", True),
        ("c.aiff", True),
        ("d.aif", True),
        ("e.mp3", False),
        ("f.m4a", False),  # ambiguous ALAC/AAC - skipped in v1
        ("g.opus", False),
        ("h.ogg", False),
    ],
)
def test_requires_transcode(name: str, expected: bool):
    assert requires_transcode(Path(name)) is expected


def test_transcoded_name_swaps_extension():
    assert transcoded_name("song.flac") == "song.opus"
    assert transcoded_name("My Song [Live].wav") == "My Song [Live].opus"
    # Bare filename only - no directory component leaks in
    assert transcoded_name("a.b.flac") == "a.b.opus"


def test_ffmpeg_available_returns_bool():
    assert isinstance(ffmpeg_available(), bool)


def test_default_workers_is_at_least_one():
    assert default_workers() >= 1


def test_resolved_workers_explicit_overrides_auto():
    assert TranscodeOptions(enabled=True, max_workers=4).resolved_workers() == 4


def test_resolved_workers_zero_falls_back_to_auto():
    opts = TranscodeOptions(enabled=True, max_workers=0)
    assert opts.resolved_workers() == default_workers()


# ── caching (no real ffmpeg needed - encoder is stubbed) ────────────────────────


def test_cached_transcode_reuses_encode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "song.flac"
    src.write_bytes(b"fake-flac")
    monkeypatch.setattr(transcode, "_CACHE_DIR", tmp_path / "cache")

    calls: list[tuple[Path, Path, int]] = []

    def fake_encode(s: Path, d: Path, b: int) -> None:
        calls.append((s, d, b))
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"opusdata")

    monkeypatch.setattr(transcode, "transcode_to_opus", fake_encode)
    opts = TranscodeOptions(enabled=True, bitrate_kbps=128)

    p1 = cached_transcode(src, opts)
    p2 = cached_transcode(src, opts)

    assert p1 == p2
    assert p1.read_bytes() == b"opusdata"
    assert len(calls) == 1  # second call was a cache hit


def test_cached_transcode_bitrate_is_part_of_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "song.flac"
    src.write_bytes(b"fake-flac")
    monkeypatch.setattr(transcode, "_CACHE_DIR", tmp_path / "cache")

    calls: list[int] = []

    def fake_encode(s: Path, d: Path, b: int) -> None:
        calls.append(b)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"x")

    monkeypatch.setattr(transcode, "transcode_to_opus", fake_encode)

    p128 = cached_transcode(src, TranscodeOptions(enabled=True, bitrate_kbps=128))
    p96 = cached_transcode(src, TranscodeOptions(enabled=True, bitrate_kbps=96))

    assert p128 != p96
    assert calls == [128, 96]


# ── real ffmpeg encode ──────────────────────────────────────────────────────────


@_skip_no_ffmpeg
def test_transcode_to_opus_produces_opus(tmp_path: Path):
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav)],
        check=True,
        capture_output=True,
    )
    dst = tmp_path / "tone.opus"
    transcode_to_opus(wav, dst, 96)

    assert dst.exists() and dst.stat().st_size > 0
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=nw=1:nk=1",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "opus"


@_skip_no_ffmpeg
def test_transcode_to_opus_failure_raises_and_cleans_up(tmp_path: Path):
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"this is not audio")
    dst = tmp_path / "out.opus"

    with pytest.raises(TranscodeError):
        transcode_to_opus(bad, dst, 96)

    # A failed encode must not leave a partial/empty file behind to be cached
    assert not dst.exists()


# ── cache management ────────────────────────────────────────────────────────────


def _write_cache_file(cache: Path, name: str, size: int, mtime: float) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / name
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


def test_cache_size_and_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(transcode, "_CACHE_DIR", cache)

    # Missing dir reports empty, doesn't raise
    assert cache_size_bytes() == 0
    assert cache_file_count() == 0

    _write_cache_file(cache, "a.opus", 123, 1000)
    _write_cache_file(cache, "b.opus", 77, 1000)
    assert cache_size_bytes() == 200
    assert cache_file_count() == 2


def test_clear_cache_deletes_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(transcode, "_CACHE_DIR", cache)
    _write_cache_file(cache, "a.opus", 100, 1000)
    _write_cache_file(cache, "b.opus", 100, 1000)

    assert clear_cache() == 200
    assert cache_file_count() == 0
    # Idempotent - nothing left to free
    assert clear_cache() == 0


def test_prune_cache_evicts_least_recently_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(transcode, "_CACHE_DIR", cache)
    # a oldest, c newest - each 100 bytes, total 300
    _write_cache_file(cache, "a.opus", 100, 1000)
    _write_cache_file(cache, "b.opus", 100, 2000)
    _write_cache_file(cache, "c.opus", 100, 3000)

    # Cap 250 -> must drop to <=250, so evict only the oldest (a)
    freed = prune_cache(max_bytes=250)
    assert freed == 100
    assert not (cache / "a.opus").exists()
    assert (cache / "b.opus").exists()
    assert (cache / "c.opus").exists()


def test_prune_cache_noop_when_under_cap_or_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cache = tmp_path / "cache"
    monkeypatch.setattr(transcode, "_CACHE_DIR", cache)
    _write_cache_file(cache, "a.opus", 100, 1000)

    assert prune_cache(max_bytes=1000) == 0  # under cap
    assert prune_cache(max_bytes=0) == 0  # disabled
    assert cache_file_count() == 1


def test_cached_transcode_bumps_mtime_on_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "song.flac"
    src.write_bytes(b"fake-flac")
    monkeypatch.setattr(transcode, "_CACHE_DIR", tmp_path / "cache")

    def fake_encode(s: Path, d: Path, b: int) -> None:
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"opus")
        os.utime(d, (1000, 1000))  # backdate so the bump is observable

    monkeypatch.setattr(transcode, "transcode_to_opus", fake_encode)
    opts = TranscodeOptions(enabled=True, bitrate_kbps=128)

    dst = cached_transcode(src, opts)  # miss -> encodes, mtime backdated to 1000
    assert dst.stat().st_mtime == 1000
    cached_transcode(src, opts)  # hit -> should bump mtime to ~now
    assert dst.stat().st_mtime > 1000
