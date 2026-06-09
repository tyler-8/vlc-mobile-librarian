"""Backward-compatible re-export shim.

All data models and source-agnostic utilities live here (or are re-exported
from their new homes). MediaMonkey-specific code has moved to
vlc_mobile_librarian.sources.mediamonkey; shared dataclasses have moved to
vlc_mobile_librarian.models.

Existing imports from vlc_mobile_librarian.library continue to work unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

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
    "DuplicateGroup",
    "DuplicateReport",
    "VLCDeviceIndex",
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
    "_normalize_title",
    "_durations_match",
    # Source-agnostic utilities (implemented here)
    "generate_m3u8",
    "compute_sync_plan",
    "find_potential_duplicates",
    "build_vlc_index",
    "classify_local_file",
    "match_device_file",
]


_FEAT_RE = re.compile(r"\s*\((?:feat|ft)\.?\s+[^)]+\)", re.IGNORECASE)
_BRACKETS_RE = re.compile(r"\s*\[[^\]]+\]")
_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def generate_m3u8(
    playlist: Playlist,
    name_override: dict[str, str] | None = None,
) -> str:
    """Generate M3U8 playlist file content as a string.

    Uses bare filenames only - VLC stores all uploaded files flat in ~/Documents/,
    so a filename-only reference resolves correctly relative to the playlist.
    Duration is in whole seconds (SongLength ms ÷ 1000, rounded).

    If `name_override` is provided and contains an entry for a track's local
    filename, the override is used in the generated M3U8 instead. This lets
    the caller point references at the device's actual filename when a
    different-named copy of the song already exists there.
    """
    overrides = name_override or {}
    lines = ["#EXTM3U"]
    for track in playlist.tracks:
        secs = round(track.file.duration_ms / 1000)
        display = track.file.title or track.file.name
        target = overrides.get(track.file.name, track.file.name)
        lines.append(f"#EXTINF:{secs},{display}")
        lines.append(target)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class VLCDeviceIndex:
    """Lookup tables over a VLC device's file list, for fast classification.

    by_filename - bare filename -> VLCFile (one entry per distinct filename)
    by_title    - normalized title -> list of VLCFiles sharing that title
    """

    by_filename: dict[str, VLCFile]
    by_title: dict[str, list[VLCFile]]


def build_vlc_index(vlc_files: list[VLCFile]) -> VLCDeviceIndex:
    """Pre-build a `VLCDeviceIndex` for repeated `classify_local_file` calls."""
    by_filename: dict[str, VLCFile] = {}
    by_title: dict[str, list[VLCFile]] = {}
    for f in vlc_files:
        if f.filename and f.filename not in by_filename:
            by_filename[f.filename] = f
        nt = _normalize_title(f.title)
        if nt:
            by_title.setdefault(nt, []).append(f)
    return VLCDeviceIndex(by_filename=by_filename, by_title=by_title)


def match_device_file(
    local: LocalFile,
    index: VLCDeviceIndex,
    project_name: Callable[[LocalFile], str] | None = None,
) -> VLCFile | None:
    """Return the VLCFile on the device that matches `local`, or None.

    Match priority: exact bare filename, then normalized title with a duration
    within tolerance. The returned `VLCFile.filename` is the name the device
    uses for that song - useful when generating M3U8 playlists so references
    point to the file that actually exists on the device.

    `project_name`, when given, maps a LocalFile to the filename it will actually
    be uploaded under (e.g. a transcoded `.opus` name) so matching reflects what
    lands on the device rather than the source filename.
    """
    name = project_name(local) if project_name else local.name
    vf = index.by_filename.get(name)
    if vf is not None:
        return vf
    nt = _normalize_title(local.title)
    if nt:
        local_secs = local.duration_ms // 1000
        for cand in index.by_title.get(nt, []):
            if _durations_match(local_secs, cand.duration):
                return cand
    return None


def classify_local_file(
    local: LocalFile,
    index: VLCDeviceIndex,
    project_name: Callable[[LocalFile], str] | None = None,
) -> str:
    """Classify a single local file against the device index.

    Returns one of:
      "already_on_device" - bare filename matches an entry on the device
      "likely_present"    - normalized title matches AND duration matches
                            (within tolerance) some device entry
      "new"               - no match

    `project_name`, when given, maps a LocalFile to the filename it will actually
    be uploaded under (e.g. a transcoded `.opus` name), so a re-encoded file
    already on the device is recognized as `already_on_device` rather than being
    re-flagged every sync.
    """
    name = project_name(local) if project_name else local.name
    if name in index.by_filename:
        return "already_on_device"
    nt = _normalize_title(local.title)
    if nt:
        local_secs = local.duration_ms // 1000
        for cand in index.by_title.get(nt, []):
            if _durations_match(local_secs, cand.duration):
                return "likely_present"
    return "new"


def compute_sync_plan(
    local_files: list[LocalFile],
    vlc_files: list[VLCFile],
    project_name: Callable[[LocalFile], str] | None = None,
) -> SyncPlan:
    """Determine which local files are new vs. already on the device.

    Three-tier classification:
      - already_on_device: bare filename matches an entry on the device
        (case-sensitive). VLC flattens all uploads into a single directory, so
        only the filename matters here.
      - likely_present:    filename does NOT match, but the file's metadata
        title (normalized) and duration (within ±3s, with 0 as wildcard) match
        an entry on the device. Suggests the same song was uploaded under a
        different filename convention. Surfaced for user override - not
        auto-skipped.
      - to_upload:         no match by either filename or title+duration.

    `project_name`, when given, maps each LocalFile to the filename it will be
    uploaded under (e.g. a transcoded `.opus` name) so classification reflects
    what actually lands on the device.
    """
    index = build_vlc_index(vlc_files)

    to_upload: list[LocalFile] = []
    already_on_device: list[LocalFile] = []
    likely_present: list[LocalFile] = []

    for f in local_files:
        kind = classify_local_file(f, index, project_name)
        if kind == "already_on_device":
            already_on_device.append(f)
        elif kind == "likely_present":
            likely_present.append(f)
        else:
            to_upload.append(f)

    return SyncPlan(
        to_upload=to_upload,
        already_on_device=already_on_device,
        total_local=len(local_files),
        likely_present=likely_present,
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


def _normalize_title(title: str) -> str:
    """Normalize a track title for fuzzy-equality comparison.

    Steps: URL-unescape, lowercase, strip "(feat. ...)" / "[bracket]" annotations,
    collapse non-alphanumeric punctuation to spaces, squash whitespace.
    Returns "" for input that normalizes to empty (callers should skip).
    """
    if not title:
        return ""
    t = unquote(title)
    t = t.lower()
    t = _FEAT_RE.sub("", t)
    t = _BRACKETS_RE.sub("", t)
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _durations_match(a: int, b: int, *, tol: int = 3) -> bool:
    """True if two durations agree within tolerance.

    A duration of 0 is treated as wildcard - corrupt/incomplete uploads often
    report 0:00, and we want them to cluster with their good counterparts.
    Otherwise, durations within `tol` seconds are considered matching;
    accommodates re-encoding drift across formats (FLAC/m4a/mp3).
    """
    if a == 0 or b == 0:
        return True
    return abs(a - b) <= tol


@dataclass(frozen=True)
class DuplicateGroup:
    """A set of VLCFiles that are likely duplicates of each other.

    confidence:
      "high"     - same normalized title with mutually-matching durations
      "medium"   - same normalized title but durations disagree (could be
                   distinct songs that share a name, e.g. multiple "Hellfire")
      "filename" - shared canonical filename via VLC's -N suffix convention
    """

    key: str
    confidence: str
    files: list[VLCFile]
    reason: str


@dataclass(frozen=True)
class DuplicateReport:
    high: list[DuplicateGroup] = field(default_factory=list)
    medium: list[DuplicateGroup] = field(default_factory=list)
    filename: list[DuplicateGroup] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.high) + len(self.medium) + len(self.filename)


def _cluster_by_duration(files: list[VLCFile]) -> list[list[VLCFile]]:
    """Single-link cluster a list of files by pairwise duration match.

    Two files are connected iff `_durations_match(a.duration, b.duration)`.
    Returns clusters as lists; singletons are included.
    """
    n = len(files)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if _durations_match(files[i].duration, files[j].duration):
                union(i, j)

    clusters: dict[int, list[VLCFile]] = {}
    for i, f in enumerate(files):
        clusters.setdefault(find(i), []).append(f)
    return list(clusters.values())


def find_potential_duplicates(vlc_files: list[VLCFile]) -> DuplicateReport:
    """Find groups of VLC device files that are likely duplicates.

    Returns a DuplicateReport partitioned into three confidence tiers:

    - high:     same normalized metadata title AND matching duration (±3s,
                with 0 as wildcard). The strongest signal we can derive from
                the device's limited per-file metadata.
    - medium:   same normalized title but mutually-mismatched durations -
                often distinct songs that happen to share a name (e.g. multiple
                tracks titled "Hellfire").
    - filename: legacy detector for VLC's auto-appended -N suffix on
                identical-filename re-uploads.

    VLC's XML inventory may list the same file more than once; those same-
    filename entries are collapsed before grouping so they don't produce
    false positives.
    """
    # Deduplicate by filename first - VLC XML sometimes lists the same file twice
    seen_filenames: set[str] = set()
    unique: list[VLCFile] = []
    for f in vlc_files:
        if f.filename not in seen_filenames:
            seen_filenames.add(f.filename)
            unique.append(f)

    # ── Filename tier (existing -N suffix detection) ──────────────────────────
    fn_groups: dict[str, list[VLCFile]] = {}
    for f in unique:
        key = _canonical_vlc_name(f.filename)
        fn_groups.setdefault(key, []).append(f)
    filename_tier = [
        DuplicateGroup(
            key=k,
            confidence="filename",
            files=sorted(v, key=lambda f: f.filename),
            reason="shared canonical filename (VLC -N suffix)",
        )
        for k, v in sorted(fn_groups.items())
        if len(v) > 1
    ]

    # ── Title-based tiers ────────────────────────────────────────────────────
    title_buckets: dict[str, list[VLCFile]] = {}
    for f in unique:
        nt = _normalize_title(f.title)
        if nt:
            title_buckets.setdefault(nt, []).append(f)

    high_tier: list[DuplicateGroup] = []
    medium_tier: list[DuplicateGroup] = []
    for nt, bucket in sorted(title_buckets.items()):
        if len(bucket) < 2:
            continue
        clusters = _cluster_by_duration(bucket)
        high_clusters = [c for c in clusters if len(c) >= 2]
        leftovers = [f for c in clusters if len(c) == 1 for f in c]
        for cluster in high_clusters:
            high_tier.append(
                DuplicateGroup(
                    key=nt,
                    confidence="high",
                    files=sorted(cluster, key=lambda f: f.filename),
                    reason="same title + matching duration",
                )
            )
        if len(leftovers) >= 2:
            medium_tier.append(
                DuplicateGroup(
                    key=nt,
                    confidence="medium",
                    files=sorted(leftovers, key=lambda f: f.filename),
                    reason="same title, durations differ (may be distinct songs)",
                )
            )

    return DuplicateReport(high=high_tier, medium=medium_tier, filename=filename_tier)
