from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vlc_mobile_librarian.models import LibraryCategory, LocalFile, Playlist, PlaylistTrack
from vlc_mobile_librarian.sources.base import ConfigField, LibrarySource, SourceError

# Default track types to include when no user config has been saved.
# 0 = music, 3 = classical (multi-movement works MediaMonkey tags separately)
_DEFAULT_TRACK_TYPES = [0, 3]

# MediaMonkey stores dates in OLE Automation format (float days since Dec 30, 1899).
# SQLite julianday epoch is Nov 24, 4714 BC.  The JD of Dec 30, 1899 is 2415018.5,
# so:  OLE_date = julianday - _MM_OLE_OFFSET
_MM_OLE_OFFSET = 2415018.5

# Maps QueryDataJSON "field" strings to SQL column expressions.
_FIELD_MAP: dict[str, str] = {
    "playCount": "s.PlayCounter",
    "rating": "s.Rating",
    "dateAdded": "s.DateAdded",
    "bitrate": "s.Bitrate",
    "album": "s.Album",
    "title": "s.SongTitle",
    "artist": "s.Artist",
}

# Maps QueryDataJSON "field" (sort) strings to SQL column expressions.
_SORT_MAP: dict[str, str] = {
    "playcount": "s.PlayCounter",
    "last played": "s.LastTimePlayed",
    "added": "s.DateAdded",
    "title": "s.SongTitle",
    "random": "RANDOM()",
    "artist": "s.Artist",
    "album": "s.Album",
    "rating": "s.Rating",
    "bitrate": "s.Bitrate",
}


@dataclass(frozen=True)
class TrackTypeInfo:
    track_type: int
    count: int
    samples: tuple[str, ...]  # "Title - Artist" strings (up to 3)
    extensions: tuple[str, ...]  # distinct file extensions found, e.g. ('.mp3',)


# ── MediaMonkeySource ─────────────────────────────────────────────────────────


class MediaMonkeySource(LibrarySource):
    """LibrarySource implementation backed by a MediaMonkey SQLite database."""

    name = "MediaMonkey"

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @classmethod
    def config_fields(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="db_path",
                label="MediaMonkey DB path",
                field_type="path",
                placeholder=(
                    r"C:\Users\<name>\AppData\Roaming\MediaMonkey5\MM5.DB"
                    if sys.platform == "win32"
                    else "/mnt/c/Users/<name>/AppData/Roaming/MediaMonkey5/MM5.DB"
                ),
                autodetect=find_mediamonkey_db,
            )
        ]

    @classmethod
    def from_settings(cls, config: dict[str, Any]) -> MediaMonkeySource:
        return cls(Path(config["db_path"]))

    def is_available(self) -> bool:
        return self._db_path.exists()

    def discover_categories(self) -> list[LibraryCategory]:
        """Return one LibraryCategory per distinct TrackType on local drives.

        Raises SourceError on read failure.
        """
        try:
            infos = discover_track_types(self._db_path)
        except FileNotFoundError as exc:
            raise SourceError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise SourceError(f"MediaMonkey DB error: {exc}") from exc
        return [
            LibraryCategory(
                id=str(info.track_type),
                label=_TRACK_TYPE_LABELS.get(info.track_type, f"Type {info.track_type}"),
                count=info.count,
                samples=info.samples,
                extensions=info.extensions,
            )
            for info in infos
        ]

    def scan_library(self, categories: list[str] | None = None) -> list[LocalFile]:
        """Return local audio files filtered by category ids (str track type ints).

        Raises SourceError on read failure.
        """
        track_types = _category_ids_to_track_types(categories)
        try:
            return scan_library_from_mediamonkey(self._db_path, track_types)
        except FileNotFoundError as exc:
            raise SourceError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise SourceError(f"MediaMonkey DB error: {exc}") from exc

    def scan_playlists(self, categories: list[str] | None = None) -> list[Playlist]:
        """Return all playlists with resolved track lists.

        Raises SourceError on read failure.
        """
        track_types = _category_ids_to_track_types(categories)
        try:
            return scan_playlists_from_mediamonkey(self._db_path, track_types)
        except FileNotFoundError as exc:
            raise SourceError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise SourceError(f"MediaMonkey DB error: {exc}") from exc


# Human-readable labels for known MediaMonkey TrackType values.
_TRACK_TYPE_LABELS: dict[int, str] = {
    0: "Music",
    1: "Podcast",
    2: "Video",
    3: "Classical",
    4: "Audiobook",
}


def _category_ids_to_track_types(categories: list[str] | None) -> list[int] | None:
    """Convert LibraryCategory id strings to MediaMonkey track type ints.

    None → None (caller uses source default).
    [] → [] (caller returns empty).
    ["0", "3"] → [0, 3].
    """
    if categories is None:
        return None
    result: list[int] = []
    for cat_id in categories:
        with contextlib.suppress(ValueError):
            result.append(int(cat_id))
    return result


# ── Auto-discovery ────────────────────────────────────────────────────────────


_ENV_VAR = "MM_DB_PATH"


def find_mediamonkey_db() -> Path | None:
    """Return the MediaMonkey database path.

    Resolution order:
    1. ``MM_DB_PATH`` environment variable - used as-is if set.
    2. Auto-discovery: standard AppData location on Windows or WSL2
       (Windows drives mounted at /mnt/c).

    Tries MM5 (MediaMonkey 5+) then MM4, under all user profiles on the C: drive.
    """
    env = os.environ.get(_ENV_VAR)
    if env:
        return Path(env)

    root = Path("C:\\") if sys.platform == "win32" else Path("/mnt/c")
    if not root.exists():
        return None
    for pattern in (
        "Users/*/AppData/Roaming/MediaMonkey5/MM5.DB",
        "Users/*/AppData/Roaming/MediaMonkey/MM.DB",
    ):
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


# ── Internal DB helpers ───────────────────────────────────────────────────────


def _open_mm_db(db_path: Path) -> sqlite3.Connection:
    """Open the MediaMonkey DB read-only with the required IUNICODE collation stub.

    MediaMonkey registers a custom IUNICODE collation in its own SQLite build.
    We register a compatible case-folding stub so index lookups don't fail.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.create_collation(
        "IUNICODE",
        lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
    )
    return conn


def _resolve_mm_path(drive_letter_index: int, song_path: str) -> Path:
    """Convert a MediaMonkey SongPath + DriveLetter index to a filesystem Path.

    MediaMonkey stores SongPath without the drive letter, e.g.:
        SongPath    = ":\\Music\\Artist\\song.mp3"
        DriveLetter = 3  →  D:

    The DriveLetter index is 0-based: A=0, B=1, C=2, D=3, …

    On Windows returns a native Windows path (e.g. D:\\Music\\song.mp3).
    On Linux / WSL2 returns a WSL mount path (e.g. /mnt/d/Music/song.mp3).
    """
    drive = chr(ord("A") + drive_letter_index)
    # song_path starts with ":" e.g. ":\Music\foo.mp3" - strip the leading colon
    rest = song_path[1:]
    if sys.platform == "win32":
        return Path(f"{drive}:{rest}")
    # Linux / WSL2: replace backslashes and prepend the WSL mount prefix
    return Path(f"/mnt/{drive.lower()}{rest.replace(chr(92), '/')}")


def _build_smart_query(query_data: dict) -> tuple[str, list, str]:
    """Translate a QueryDataJSON dict into (where_fragment, params, warning).

    Returns an SQL WHERE fragment (no WHERE keyword), the corresponding
    parameter list, and a human-readable warning for any conditions that were
    skipped because they use unsupported fields or operators.
    """
    conditions: list[str] = []
    params: list = []
    warnings: list[str] = []

    for cond in query_data.get("conditions", {}).get("data", []):
        field = cond.get("field", "")
        op = cond.get("operator", "")
        value = cond.get("value", "")

        if op == "is accessible":
            # Already guaranteed accessible by DriveType = 3 in the base query.
            continue

        sql_col = _FIELD_MAP.get(field)
        if sql_col is None:
            warnings.append(f"unsupported field '{field}'")
            continue

        if op == "< (days ago)":
            # "dateAdded < N days ago" = added within the last N days
            # OLE date: today_ole - N = julianday('now') - _MM_OLE_OFFSET - N
            try:
                n = float(value)
            except (ValueError, TypeError):
                warnings.append(f"invalid value '{value}' for '{field} {op}'")
                continue
            conditions.append(f"{sql_col} >= (julianday('now') - {_MM_OLE_OFFSET} - ?)")
            params.append(n)
        elif op == "> (days ago)":
            # "dateAdded > N days ago" = added more than N days ago
            try:
                n = float(value)
            except (ValueError, TypeError):
                warnings.append(f"invalid value '{value}' for '{field} {op}'")
                continue
            conditions.append(f"{sql_col} < (julianday('now') - {_MM_OLE_OFFSET} - ?)")
            params.append(n)
        elif op == "does not contain":
            conditions.append(f"{sql_col} NOT LIKE ?")
            params.append(f"%{value}%")
        elif op == "contains":
            conditions.append(f"{sql_col} LIKE ?")
            params.append(f"%{value}%")
        elif op in (">", ">=", "<", "<=", "="):
            conditions.append(f"{sql_col} {op} ?")
            params.append(value)
        else:
            warnings.append(f"unsupported operator '{op}' on field '{field}'")

    where_fragment = " AND ".join(conditions) if conditions else "1=1"
    return where_fragment, params, "; ".join(warnings)


def _build_order_clause(sort_orders: list[dict]) -> str:
    """Translate a sortOrders list into an SQL ORDER BY clause (with keyword)."""
    parts: list[str] = []
    for so in sort_orders:
        col = _SORT_MAP.get(so.get("field", "").lower())
        if col is None:
            continue
        if col == "RANDOM()":
            parts.append("RANDOM()")
        else:
            direction = "ASC" if so.get("ascending", True) else "DESC"
            parts.append(f"{col} {direction}")
    return f"ORDER BY {', '.join(parts)}" if parts else "ORDER BY s.SongTitle ASC"


def _rows_to_playlist_tracks(
    rows: list[tuple],
    drive_letter_col: int = 7,
) -> list[PlaylistTrack]:
    """Convert raw DB rows into PlaylistTrack objects.

    Expected columns per row:
        0: s.ID, 1: s.SongPath, 2: s.FileLength, 3: s.SongLength,
        4: SongTitle, 5: Artist, 6: Album, 7: m.DriveLetter
    """
    tracks: list[PlaylistTrack] = []
    for row in rows:
        song_id, song_path, file_length, song_length, title, artist, album, drive_letter = row
        try:
            local_path = _resolve_mm_path(drive_letter, song_path)
        except (IndexError, TypeError):
            continue
        tracks.append(
            PlaylistTrack(
                file=LocalFile(
                    path=local_path,
                    name=local_path.name,
                    size=file_length or 0,
                    duration_ms=song_length or 0,
                    title=title,
                    artist=artist,
                    album=album,
                ),
                song_id=song_id,
            )
        )
    return tracks


# ── Public scanning functions ─────────────────────────────────────────────────


def discover_track_types(db_path: Path) -> list[TrackTypeInfo]:
    """Scan the MediaMonkey DB and return one TrackTypeInfo per distinct TrackType on local drives.

    Raises FileNotFoundError if db_path does not exist.
    Raises sqlite3.Error on database read errors.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"MediaMonkey database not found: {db_path}")

    conn = _open_mm_db(db_path)
    try:
        counts = conn.execute("""
            SELECT s.TrackType, COUNT(*) AS cnt
            FROM Songs s
            JOIN Medias m ON s.IDMedia = m.IDMedia
            WHERE m.DriveType = 3
              AND s.SongPath IS NOT NULL AND s.SongPath != ''
            GROUP BY s.TrackType
            ORDER BY s.TrackType
        """).fetchall()

        results: list[TrackTypeInfo] = []
        for track_type, count in counts:
            sample_rows = conn.execute(
                """
                SELECT COALESCE(s.SongTitle, ''), COALESCE(s.Artist, '')
                FROM Songs s
                JOIN Medias m ON s.IDMedia = m.IDMedia
                WHERE s.TrackType = ? AND m.DriveType = 3
                  AND s.SongPath IS NOT NULL AND s.SongPath != ''
                LIMIT 3
            """,
                (track_type,),
            ).fetchall()
            samples = tuple(f"{t} - {a}" if a else t for t, a in sample_rows if t)

            # Get last 6 chars of each distinct path, then extract the extension
            # in Python. Using 6 chars covers extensions up to 5 chars (e.g. .flac).
            ext_rows = conn.execute(
                """
                SELECT DISTINCT LOWER(SUBSTR(s.SongPath, LENGTH(s.SongPath) - 5))
                FROM Songs s
                JOIN Medias m ON s.IDMedia = m.IDMedia
                WHERE s.TrackType = ? AND m.DriveType = 3
                  AND s.SongPath IS NOT NULL AND s.SongPath != ''
            """,
                (track_type,),
            ).fetchall()
            exts: set[str] = set()
            for (last6,) in ext_rows:
                if last6:
                    dot = last6.rfind(".")
                    if dot != -1:
                        exts.add(last6[dot:])
            extensions = tuple(sorted(exts))

            results.append(
                TrackTypeInfo(
                    track_type=track_type,
                    count=count,
                    samples=samples,
                    extensions=extensions,
                )
            )
        return results
    finally:
        conn.close()


def scan_library_from_mediamonkey(
    db_path: Path,
    track_types: list[int] | None = None,
) -> list[LocalFile]:
    """Read the MediaMonkey SQLite database and return local audio tracks.

    track_types controls which TrackType values are included. Defaults to
    [0, 3] (music and classical). Pass an explicit list to override.

    Raises FileNotFoundError if db_path does not exist.
    Raises sqlite3.Error on database read errors.
    """
    _types = track_types if track_types is not None else _DEFAULT_TRACK_TYPES
    if not _types:
        return []

    if not db_path.exists():
        raise FileNotFoundError(f"MediaMonkey database not found: {db_path}")

    conn = _open_mm_db(db_path)
    try:
        placeholders = ",".join("?" * len(_types))
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                s.SongPath,
                s.FileLength,
                s.SongLength,
                COALESCE(s.SongTitle, '') AS SongTitle,
                COALESCE(s.Artist,    '') AS Artist,
                COALESCE(s.Album,     '') AS Album,
                m.DriveLetter
            FROM Songs s
            JOIN Medias m ON s.IDMedia = m.IDMedia
            WHERE s.TrackType IN ({placeholders})
              AND m.DriveType = 3
              AND s.SongPath IS NOT NULL
              AND s.SongPath != ''
        """,
            _types,
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    results: list[LocalFile] = []
    for song_path, file_length, song_length, title, artist, album, drive_letter in rows:
        try:
            local_path = _resolve_mm_path(drive_letter, song_path)
        except (IndexError, TypeError):
            continue
        results.append(
            LocalFile(
                path=local_path,
                name=local_path.name,
                size=file_length or 0,
                duration_ms=song_length or 0,
                title=title,
                artist=artist,
                album=album,
            )
        )

    results.sort(key=lambda f: (f.artist.lower(), f.album.lower(), f.name.lower()))
    return results


def scan_playlists_from_mediamonkey(
    db_path: Path,
    track_types: list[int] | None = None,
) -> list[Playlist]:
    """Load all MediaMonkey playlists and resolve their track lists.

    For static playlists (IsAutoPlaylist=0): reads PlaylistSongs joined to Songs.
    For smart playlists (IsAutoPlaylist=1): evaluates QueryDataJSON criteria.

    track_types restricts which TrackType values are eligible (same default as
    scan_library_from_mediamonkey: [0, 3]).

    Raises FileNotFoundError if db_path does not exist.
    Raises sqlite3.Error on database read errors.
    """
    _types = track_types if track_types is not None else _DEFAULT_TRACK_TYPES
    if not db_path.exists():
        raise FileNotFoundError(f"MediaMonkey database not found: {db_path}")

    conn = _open_mm_db(db_path)
    try:
        playlist_rows = conn.execute(
            "SELECT IDPlaylist, PlaylistName, IsAutoPlaylist, QueryDataJSON"
            " FROM Playlists ORDER BY PlaylistName"
        ).fetchall()

        type_placeholders = ",".join("?" * len(_types)) if _types else "0"

        results: list[Playlist] = []
        for pid, name, is_auto, query_data_json in playlist_rows:
            tracks: list[PlaylistTrack] = []
            warning = ""

            if not is_auto:
                # Static playlist - join through PlaylistSongs
                rows = conn.execute(
                    f"""
                    SELECT s.ID, s.SongPath, s.FileLength, s.SongLength,
                           COALESCE(s.SongTitle, ''), COALESCE(s.Artist, ''),
                           COALESCE(s.Album, ''), m.DriveLetter
                    FROM PlaylistSongs ps
                    JOIN Songs s ON s.ID = ps.IDSong
                    JOIN Medias m ON s.IDMedia = m.IDMedia
                    WHERE ps.IDPlaylist = ?
                      AND m.DriveType = 3
                      AND s.SongPath IS NOT NULL AND s.SongPath != ''
                      {"AND s.TrackType IN (" + type_placeholders + ")" if _types else ""}
                    ORDER BY ps.SongOrder ASC
                    """,
                    [pid] + (_types if _types else []),
                ).fetchall()
                tracks = _rows_to_playlist_tracks(rows)

            else:
                # Smart playlist - evaluate QueryDataJSON
                try:
                    query_data = json.loads(query_data_json) if query_data_json else {}
                except (json.JSONDecodeError, TypeError):
                    query_data = {}
                    warning = "could not parse QueryDataJSON"

                where_fragment, smart_params, warning = _build_smart_query(query_data)
                order_clause = _build_order_clause(query_data.get("sortOrders", []))

                limit_type = query_data.get("limit", "")
                limit_value = query_data.get("limitValue")

                # Apply SQL LIMIT only for file-count limits; others truncate in Python.
                sql_limit = ""
                if limit_type == "files" and limit_value is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        sql_limit = f"LIMIT {int(limit_value)}"

                base_conditions = (
                    f"m.DriveType = 3"
                    f" AND s.SongPath IS NOT NULL AND s.SongPath != ''"
                    f"{' AND s.TrackType IN (' + type_placeholders + ')' if _types else ''}"
                )
                base_params: list = _types if _types else []

                sql = f"""
                    SELECT s.ID, s.SongPath, s.FileLength, s.SongLength,
                           COALESCE(s.SongTitle, ''), COALESCE(s.Artist, ''),
                           COALESCE(s.Album, ''), m.DriveLetter
                    FROM Songs s
                    JOIN Medias m ON s.IDMedia = m.IDMedia
                    WHERE {base_conditions}
                      AND {where_fragment}
                    {order_clause}
                    {sql_limit}
                """
                rows = conn.execute(sql, base_params + smart_params).fetchall()
                tracks = _rows_to_playlist_tracks(rows)

                # Post-fetch truncation for length/megabyte limits
                if limit_type == "length" and limit_value is not None:
                    try:
                        limit_ms = float(limit_value) * 60_000
                        running = 0.0
                        truncated: list[PlaylistTrack] = []
                        for track in tracks:
                            if running + track.file.duration_ms > limit_ms:
                                break
                            truncated.append(track)
                            running += track.file.duration_ms
                        tracks = truncated
                    except (ValueError, TypeError):
                        pass
                elif limit_type == "megabytes" and limit_value is not None:
                    try:
                        limit_bytes = float(limit_value) * 1_048_576
                        running_b = 0.0
                        truncated_b: list[PlaylistTrack] = []
                        for track in tracks:
                            if running_b + track.file.size > limit_bytes:
                                break
                            truncated_b.append(track)
                            running_b += track.file.size
                        tracks = truncated_b
                    except (ValueError, TypeError):
                        pass

            results.append(
                Playlist(
                    id=pid,
                    name=name,
                    is_auto=bool(is_auto),
                    tracks=tracks,
                    unsupported_reason=warning,
                )
            )

        return results
    finally:
        conn.close()
