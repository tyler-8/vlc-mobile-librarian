"""Ad-hoc transcoding of lossless audio to Opus before upload.

VLC for iOS decodes Opus natively, and Opus gives the best quality-per-byte of
any modern lossy codec (~128 kbps VBR is transparent for music). This module
optionally re-encodes *lossless* sources (FLAC/WAV/AIFF/...) to Opus right before
upload so the user doesn't waste device space and Wi-Fi transfer time.

Design rules:
  - Only lossless sources are transcoded. Existing lossy files (MP3/AAC/Opus)
    pass through untouched - re-encoding them is generation loss for no benefit.
  - ffmpeg is the encoder, invoked via subprocess (no extra Python dependency).
  - Encodes are cached on disk keyed by (source path, mtime, bitrate) so repeat
    syncs of the same library don't re-encode.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Lossless container/codec extensions worth transcoding. Deliberately excludes
# .m4a/.alac - that container can hold either lossless ALAC or already-lossy AAC,
# so distinguishing them requires ffprobe; left as a follow-up.
LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".wv", ".ape"}

# Where cached Opus encodes live.
_CACHE_DIR = Path.home() / ".cache" / "vlc-mobile-librarian" / "transcode"


class TranscodeError(Exception):
    """Raised when ffmpeg fails to transcode a file."""


def default_workers() -> int:
    """Default number of parallel encodes.

    One ffmpeg per core (libopus is single-threaded) saturates the machine and
    starves the UI server thread, so leave one core free for it and the
    upload loop. Users can raise this in the sidebar if they want full throughput.
    """
    return max(1, (os.cpu_count() or 2) - 1)


@dataclass(frozen=True)
class TranscodeOptions:
    """User-facing transcoding settings, threaded through the upload pipeline."""

    enabled: bool
    bitrate_kbps: int = 128
    max_workers: int = 0  # 0 = auto (one per CPU core)
    cache_cap_bytes: int = 0  # 0 = unlimited (no LRU pruning)

    def resolved_workers(self) -> int:
        """Concrete worker count - the explicit value, or the auto default."""
        return self.max_workers if self.max_workers > 0 else default_workers()


def ffmpeg_available() -> bool:
    """True if the ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def requires_transcode(path: Path) -> bool:
    """True if `path` is a lossless source we should transcode to Opus."""
    return path.suffix.lower() in LOSSLESS_EXTS


def transcoded_name(name: str) -> str:
    """Return `name` with its extension swapped to .opus.

    Operates on the bare filename only (no directory component), matching how
    the rest of the app keys files by `LocalFile.name`.
    """
    return Path(name).with_suffix(".opus").name


def transcode_to_opus(src: Path, dst: Path, bitrate_kbps: int) -> None:
    """Encode `src` to an Opus file at `dst` using ffmpeg.

    Carries over tags (`-map_metadata 0`) and, best-effort, embedded cover art
    (the `0:V?` map is optional so art-less files don't fail). Raises
    TranscodeError on a non-zero exit, including the tail of ffmpeg's stderr.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-c:a",
        "libopus",
        "-b:a",
        f"{bitrate_kbps}k",
        "-vbr",
        "on",
        "-map_metadata",
        "0",
        "-map",
        "0:V?",
        "-c:v",
        "copy",
        "-disposition:v",
        "attached_pic",
        str(dst),
    ]
    logger.info("transcode start: %s @ %dk", src.name, bitrate_kbps)
    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Clean up a partial/empty output so a failed encode is never cached.
        dst.unlink(missing_ok=True)
        stderr_tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        logger.error(
            "transcode FAILED: %s (ffmpeg exit %d)\n%s", src.name, proc.returncode, stderr_tail
        )
        raise TranscodeError(f"ffmpeg failed for {src.name}:\n{stderr_tail}")
    elapsed = time.perf_counter() - start
    out_mb = dst.stat().st_size / 1_048_576
    logger.info("transcode done:  %s in %.1fs → %.1f MB", src.name, elapsed, out_mb)


def _cache_path(src: Path, bitrate_kbps: int) -> Path:
    """Deterministic cache path keyed by resolved source path, mtime and bitrate."""
    resolved = src.resolve()
    mtime = src.stat().st_mtime_ns
    key = f"{resolved}|{mtime}|{bitrate_kbps}".encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    return _CACHE_DIR / f"{digest}.opus"


def cached_transcode(src: Path, opts: TranscodeOptions) -> Path:
    """Return a path to an Opus encode of `src`, transcoding only if not cached.

    The cache key includes the source mtime, so editing the source invalidates
    the old encode. Returns the cache path (caller uploads it under the user-
    facing `transcoded_name`).
    """
    dst = _cache_path(src, opts.bitrate_kbps)
    if dst.exists() and dst.stat().st_size > 0:
        logger.debug("transcode cache hit: %s → %s", src.name, dst.name)
        # Bump mtime so LRU pruning treats a reused encode as recently used.
        with contextlib.suppress(OSError):
            os.utime(dst, None)
        return dst
    transcode_to_opus(src, dst, opts.bitrate_kbps)
    return dst


# ── Cache management ──────────────────────────────────────────────────────────


def _cache_files() -> list[Path]:
    """All files currently in the transcode cache (empty list if none)."""
    if not _CACHE_DIR.exists():
        return []
    return [p for p in _CACHE_DIR.iterdir() if p.is_file()]


def cache_size_bytes() -> int:
    """Total size of the transcode cache on disk, in bytes."""
    total = 0
    for p in _cache_files():
        with contextlib.suppress(OSError):
            total += p.stat().st_size
    return total


def cache_file_count() -> int:
    """Number of cached encodes on disk."""
    return len(_cache_files())


def clear_cache() -> int:
    """Delete every cached encode. Returns bytes freed.

    Safe at any time - cache contents are pure derived data and are re-created on
    demand. Don't call while an upload job is mid-transfer, though, or a file
    being uploaded could be deleted out from under it.
    """
    freed = 0
    for p in _cache_files():
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
        except OSError:
            logger.warning("could not delete cache file %s", p, exc_info=True)
    if freed:
        logger.info("cleared transcode cache: freed %.1f MB", freed / 1_048_576)
    return freed


def prune_cache(max_bytes: int) -> int:
    """Evict least-recently-used encodes until the cache fits within max_bytes.

    Recency is file mtime, which `cached_transcode` bumps on every cache hit, so
    actively-reused encodes survive and stale ones are evicted first. A
    `max_bytes <= 0` cap disables pruning. Returns bytes freed.
    """
    if max_bytes <= 0:
        return 0
    files = []
    for p in _cache_files():
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))

    total = sum(size for _, size, _ in files)
    if total <= max_bytes:
        return 0

    files.sort(key=lambda t: t[0])  # oldest (least recently used) first
    freed = 0
    for _, size, p in files:
        if total - freed <= max_bytes:
            break
        try:
            p.unlink()
            freed += size
        except OSError:
            logger.warning("could not evict cache file %s", p, exc_info=True)
    logger.info(
        "pruned transcode cache: freed %.1f MB (cap %.1f MB)",
        freed / 1_048_576,
        max_bytes / 1_048_576,
    )
    return freed
