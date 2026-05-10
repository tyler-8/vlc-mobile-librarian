from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LocalFile:
    path: Path
    name: str  # = path.name - key for duplicate detection against VLC titles
    size: int  # bytes
    duration_ms: int = 0  # SongLength in milliseconds; 0 = unknown
    title: str = ""
    artist: str = ""
    album: str = ""


@dataclass(frozen=True)
class PlaylistTrack:
    file: LocalFile
    song_id: int  # Songs.ID - used for cross-playlist deduplication


@dataclass(frozen=True)
class Playlist:
    id: int
    name: str
    is_auto: bool
    tracks: list[PlaylistTrack]
    unsupported_reason: str = ""  # non-empty if smart eval was partial


@dataclass
class SyncPlan:
    to_upload: list[LocalFile]
    already_on_device: list[LocalFile]
    total_local: int
    # Files whose bare filename does not match any device file but whose
    # normalized metadata title and duration match an entry on the device -
    # likely the same song under a different filename. Surfaced separately so
    # the user can override per-file before uploading.
    likely_present: list[LocalFile] = field(default_factory=list)


@dataclass(frozen=True)
class LibraryCategory:
    """A filterable grouping of tracks within a library source.

    The id is opaque - MediaMonkey uses str(track_type_int), other sources
    may use genre names, folder paths, or any other string identifier.
    """

    id: str  # opaque - passed back to scan_library / scan_playlists
    label: str  # human-readable name shown in UI (e.g. "Music", "Podcasts")
    count: int
    samples: tuple[str, ...]  # "Title - Artist" strings (up to 3)
    extensions: tuple[str, ...]  # distinct file extensions found, e.g. ('.mp3',)
