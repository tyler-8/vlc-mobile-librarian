"""Backward-compatible re-export shim.

All data models and source-agnostic utilities live here (or are re-exported
from their new homes). MediaMonkey-specific code has moved to
vlc_mobile_librarian.sources.mediamonkey; shared dataclasses have moved to
vlc_mobile_librarian.models.

Existing imports from vlc_mobile_librarian.library continue to work unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from vlc_mobile_librarian.models import LocalFile, Playlist, PlaylistTrack, SyncPlan
from vlc_mobile_librarian.sources.mediamonkey import (
    TrackTypeInfo,
    _build_order_clause,
    _build_smart_query,
    _resolve_mm_path,
    discover_track_types,
    find_mediamonkey_db,
    scan_library_from_mediamonkey,
    scan_playlists_from_mediamonkey,
)
from vlc_mobile_librarian.vlc_client import VLCFile

__all__ = [
    # Data models
    "LocalFile",
    "PlaylistTrack",
    "Playlist",
    "SyncPlan",
    "TrackTypeInfo",
    # MediaMonkey functions (re-exported for backward compat)
    "find_mediamonkey_db",
    "discover_track_types",
    "scan_library_from_mediamonkey",
    "scan_playlists_from_mediamonkey",
    # Private helpers re-exported for test access
    "_resolve_mm_path",
    "_build_smart_query",
    "_build_order_clause",
    "_canonical_vlc_name",
    # Source-agnostic utilities (implemented here)
    "generate_m3u8",
    "compute_sync_plan",
    "find_potential_duplicates",
]


def generate_m3u8(playlist: Playlist) -> str:
    """Generate M3U8 playlist file content as a string.

    Uses bare filenames only - VLC stores all uploaded files flat in ~/Documents/,
    so a filename-only reference resolves correctly relative to the playlist.
    Duration is in whole seconds (SongLength ms ÷ 1000, rounded).
    """
    lines = ["#EXTM3U"]
    for track in playlist.tracks:
        secs = round(track.file.duration_ms / 1000)
        display = track.file.title or track.file.name
        lines.append(f"#EXTINF:{secs},{display}")
        lines.append(track.file.name)
    return "\n".join(lines) + "\n"


def compute_sync_plan(
    local_files: list[LocalFile],
    vlc_files: list[VLCFile],
) -> SyncPlan:
    """Determine which local files are new vs. already on the device.

    Duplicate detection matches LocalFile.name against VLCFile.filename (the
    bare filename extracted from the download URL, not the metadata title).

    VLC flattens all uploads into a single directory, so path structure is
    irrelevant - only the filename matters. Matching is case-sensitive.
    """
    vlc_filenames: set[str] = {f.filename for f in vlc_files if f.filename}

    to_upload: list[LocalFile] = []
    already_on_device: list[LocalFile] = []

    for f in local_files:
        if f.name in vlc_filenames:
            already_on_device.append(f)
        else:
            to_upload.append(f)

    return SyncPlan(
        to_upload=to_upload,
        already_on_device=already_on_device,
        total_local=len(local_files),
    )


def _canonical_vlc_name(filename: str) -> str:
    """Strip VLC's auto-appended -N suffix to get the canonical filename.

    VLC appends -N when the same filename is uploaded more than once:
      'song.mp3'    -> 'song.mp3'
      'song-1.mp3'  -> 'song.mp3'
      'song-12.mp3' -> 'song.mp3'
    """
    p = Path(filename)
    m = re.match(r"^(.+)-(\d+)$", p.stem)
    if m:
        return m.group(1) + p.suffix
    return filename


def find_potential_duplicates(vlc_files: list[VLCFile]) -> dict[str, list[VLCFile]]:
    """Find groups of VLC device files that are likely duplicates.

    Groups files by canonical name (with the VLC-appended -N suffix stripped).
    Returns only groups with 2+ distinct filenames, with each group sorted by filename.

    VLC's XML inventory may list the same file more than once; those same-filename
    entries are collapsed before grouping so they don't produce false positives.
    """
    # Deduplicate by filename first - VLC XML sometimes lists the same file twice
    seen_filenames: set[str] = set()
    unique: list[VLCFile] = []
    for f in vlc_files:
        if f.filename not in seen_filenames:
            seen_filenames.add(f.filename)
            unique.append(f)

    groups: dict[str, list[VLCFile]] = {}
    for f in unique:
        key = _canonical_vlc_name(f.filename)
        groups.setdefault(key, []).append(f)
    return {
        k: sorted(v, key=lambda f: f.filename)
        for k, v in groups.items()
        if len(v) > 1
    }
