"""Tests for vlc_mobile_librarian.library - pure and DB-backed functions using a temp DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vlc_mobile_librarian.library import (
    LocalFile,
    Playlist,
    PlaylistTrack,
    _build_order_clause,
    _build_smart_query,
    _canonical_vlc_name,
    _resolve_mm_path,
    compute_sync_plan,
    discover_track_types,
    find_potential_duplicates,
    generate_m3u8,
    scan_library_from_mediamonkey,
    scan_playlists_from_mediamonkey,
)
from vlc_mobile_librarian.vlc_client import VLCFile

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mm_db(tmp_path: Path) -> Path:
    """Minimal MediaMonkey-like SQLite DB for testing, with two track types."""
    db = tmp_path / "MM5.DB"
    conn = sqlite3.connect(str(db))
    conn.create_collation(
        "IUNICODE",
        lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
    )
    conn.executescript("""
        CREATE TABLE Medias (
            IDMedia     INTEGER PRIMARY KEY,
            DriveType   INTEGER,
            DriveLetter INTEGER
        );
        CREATE TABLE Songs (
            ID           INTEGER PRIMARY KEY,
            IDMedia      INTEGER,
            TrackType    INTEGER,
            SongPath     TEXT,
            FileLength   INTEGER,
            SongLength   INTEGER,
            SongTitle    TEXT,
            Artist       TEXT,
            Album        TEXT,
            PlayCounter  INTEGER DEFAULT 0,
            Rating       INTEGER DEFAULT 0,
            Bitrate      INTEGER DEFAULT 0,
            DateAdded    REAL    DEFAULT 0,
            LastTimePlayed REAL  DEFAULT 0
        );
        CREATE TABLE Playlists (
            IDPlaylist      INTEGER PRIMARY KEY,
            PlaylistName    TEXT,
            IsAutoPlaylist  INTEGER,
            QueryDataJSON   TEXT
        );
        CREATE TABLE PlaylistSongs (
            IDPlaylistSong  INTEGER PRIMARY KEY,
            IDPlaylist      INTEGER,
            IDSong          INTEGER,
            SongOrder       INTEGER
        );
        -- Local drive: DriveType=3, DriveLetter=3 (D:)
        INSERT INTO Medias VALUES (1, 3, 3);
        -- TrackType=0 (music); SongLength in milliseconds
        INSERT INTO Songs VALUES
            (1, 1, 0, ':\\Music\\song_a.mp3',
             5000000, 210000, 'Song A', 'Artist A', 'Album X',
             5, 80, 320, 46100.0, 46120.0);
        INSERT INTO Songs VALUES
            (2, 1, 0, ':\\Music\\song_b.mp3',
             4000000, 180000, 'Song B', 'Artist B', 'Album Y',
             0, 60, 128, 46090.0, 0.0);
        -- TrackType=3 (classical)
        INSERT INTO Songs VALUES
            (3, 1, 3, ':\\Classical\\vivaldi.mp3',
             6000000, 300000, 'Four Seasons', 'Vivaldi', 'The Four Seasons',
             2, 90, 320, 46080.0, 46115.0);
        -- TrackType=1 (podcast - should be excluded by default)
        INSERT INTO Songs VALUES
            (4, 1, 1, ':\\Podcasts\\ep1.mp3',
             3000000, 3600000, 'Episode 1', 'My Podcast', 'My Podcast',
             0, 0, 128, 46070.0, 0.0);
    """)
    conn.close()
    return db


def _lf(name: str, size: int = 0) -> LocalFile:
    return LocalFile(path=Path(f"/mnt/d/{name}"), name=name, size=size)


def _vf(filename: str) -> VLCFile:
    return VLCFile(
        title=filename,
        filename=filename,
        size=0,
        duration=0,
        thumb_url="",
        download_url="",
    )


# ── _resolve_mm_path ──────────────────────────────────────────────────────────


def test_resolve_mm_path_d_drive():
    assert _resolve_mm_path(3, ":\\Music\\foo.mp3") == Path("/mnt/d/Music/foo.mp3")


def test_resolve_mm_path_c_drive():
    assert _resolve_mm_path(2, ":\\Users\\user\\song.flac") == Path("/mnt/c/Users/user/song.flac")


def test_resolve_mm_path_a_drive():
    assert _resolve_mm_path(0, ":\\test.mp3") == Path("/mnt/a/test.mp3")


def test_resolve_mm_path_nested():
    assert _resolve_mm_path(3, ":\\Artist\\Album\\track 01.mp3") == Path(
        "/mnt/d/Artist/Album/track 01.mp3"
    )


# ── compute_sync_plan ─────────────────────────────────────────────────────────


def test_sync_plan_all_new():
    local = [_lf("a.mp3"), _lf("b.mp3")]
    plan = compute_sync_plan(local, [])
    assert plan.to_upload == local
    assert plan.already_on_device == []
    assert plan.total_local == 2


def test_sync_plan_all_existing():
    local = [_lf("a.mp3"), _lf("b.mp3")]
    plan = compute_sync_plan(local, [_vf("a.mp3"), _vf("b.mp3")])
    assert plan.to_upload == []
    assert len(plan.already_on_device) == 2
    assert plan.total_local == 2


def test_sync_plan_mixed():
    local = [_lf("a.mp3"), _lf("b.mp3"), _lf("c.mp3")]
    plan = compute_sync_plan(local, [_vf("b.mp3")])
    assert len(plan.to_upload) == 2
    assert plan.already_on_device == [_lf("b.mp3")]


def test_sync_plan_case_sensitive():
    # Matching is case-sensitive - "Song.MP3" != "song.mp3"
    local = [_lf("Song.MP3")]
    plan = compute_sync_plan(local, [_vf("song.mp3")])
    assert len(plan.to_upload) == 1
    assert plan.already_on_device == []


def test_sync_plan_vlc_empty_filename_ignored():
    # VLCFile with empty filename should not count as a match
    local = [_lf("a.mp3")]
    vlc = [
        VLCFile(
            title="a.mp3",
            filename="",
            size=0,
            duration=0,
            thumb_url="",
            download_url="",
        )
    ]
    plan = compute_sync_plan(local, vlc)
    assert len(plan.to_upload) == 1


# ── scan_library_from_mediamonkey ─────────────────────────────────────────────


def test_scan_missing_db():
    with pytest.raises(FileNotFoundError):
        scan_library_from_mediamonkey(Path("/nonexistent/MM5.DB"))


def test_scan_empty_track_types(mm_db: Path):
    assert scan_library_from_mediamonkey(mm_db, track_types=[]) == []


def test_scan_default_includes_music_and_classical(mm_db: Path):
    results = scan_library_from_mediamonkey(mm_db)
    names = {f.name for f in results}
    assert "song_a.mp3" in names
    assert "song_b.mp3" in names
    assert "vivaldi.mp3" in names
    assert "ep1.mp3" not in names  # TrackType=1, excluded by default


def test_scan_explicit_track_types(mm_db: Path):
    results = scan_library_from_mediamonkey(mm_db, track_types=[0])
    names = {f.name for f in results}
    assert "song_a.mp3" in names
    assert "song_b.mp3" in names
    assert "vivaldi.mp3" not in names


def test_scan_path_conversion(mm_db: Path):
    results = scan_library_from_mediamonkey(mm_db, track_types=[0])
    paths = {f.path for f in results}
    assert Path("/mnt/d/Music/song_a.mp3") in paths


def test_scan_metadata(mm_db: Path):
    results = scan_library_from_mediamonkey(mm_db, track_types=[0])
    by_name = {f.name: f for f in results}
    f = by_name["song_a.mp3"]
    assert f.title == "Song A"
    assert f.artist == "Artist A"
    assert f.album == "Album X"
    assert f.size == 5_000_000


def test_scan_sorted_by_artist_album_name(mm_db: Path):
    results = scan_library_from_mediamonkey(mm_db)
    keys = [(f.artist.lower(), f.album.lower(), f.name.lower()) for f in results]
    assert keys == sorted(keys)


# ── discover_track_types ──────────────────────────────────────────────────────


def test_discover_missing_db():
    with pytest.raises(FileNotFoundError):
        discover_track_types(Path("/nonexistent/MM5.DB"))


def test_discover_returns_all_types(mm_db: Path):
    infos = discover_track_types(mm_db)
    types = {i.track_type for i in infos}
    assert types == {0, 1, 3}


def test_discover_counts(mm_db: Path):
    infos = {i.track_type: i for i in discover_track_types(mm_db)}
    assert infos[0].count == 2
    assert infos[3].count == 1
    assert infos[1].count == 1


def test_discover_samples_populated(mm_db: Path):
    infos = {i.track_type: i for i in discover_track_types(mm_db)}
    assert len(infos[0].samples) > 0
    assert any("Song" in s for s in infos[0].samples)


def test_discover_extensions(mm_db: Path):
    infos = {i.track_type: i for i in discover_track_types(mm_db)}
    assert ".mp3" in infos[0].extensions


# ── _build_smart_query ────────────────────────────────────────────────────────


def test_smart_query_rating_gte():
    where, params, warning = _build_smart_query(
        {"conditions": {"data": [{"field": "rating", "operator": ">=", "value": "80"}]}}
    )
    assert "s.Rating >= ?" in where
    assert params == ["80"]
    assert warning == ""


def test_smart_query_play_count_gt():
    where, params, warning = _build_smart_query(
        {"conditions": {"data": [{"field": "playCount", "operator": ">", "value": "0"}]}}
    )
    assert "s.PlayCounter > ?" in where
    assert params == ["0"]


def test_smart_query_does_not_contain():
    where, params, warning = _build_smart_query(
        {
            "conditions": {
                "data": [{"field": "album", "operator": "does not contain", "value": "Christmas"}]
            }
        }
    )
    assert "s.Album NOT LIKE ?" in where
    assert params == ["%Christmas%"]


def test_smart_query_contains():
    where, params, warning = _build_smart_query(
        {"conditions": {"data": [{"field": "title", "operator": "contains", "value": "Love"}]}}
    )
    assert "s.SongTitle LIKE ?" in where
    assert params == ["%Love%"]


def test_smart_query_date_added_days_ago():
    where, params, warning = _build_smart_query(
        {
            "conditions": {
                "data": [{"field": "dateAdded", "operator": "< (days ago)", "value": "30"}]
            }
        }
    )
    assert "s.DateAdded >= (julianday('now') - 2415018.5 - ?)" in where
    assert params == [30.0]
    assert warning == ""


def test_smart_query_is_accessible_silently_skipped():
    where, params, warning = _build_smart_query(
        {"conditions": {"data": [{"field": "status", "operator": "is accessible", "value": ""}]}}
    )
    # No SQL emitted, no warning
    assert where == "1=1"
    assert params == []
    assert warning == ""


def test_smart_query_unsupported_field_warns():
    where, params, warning = _build_smart_query(
        {"conditions": {"data": [{"field": "unknownfield", "operator": ">", "value": "0"}]}}
    )
    assert where == "1=1"
    assert "unsupported field 'unknownfield'" in warning


def test_smart_query_unsupported_operator_warns():
    where, params, warning = _build_smart_query(
        {"conditions": {"data": [{"field": "rating", "operator": "is between", "value": "50,90"}]}}
    )
    assert where == "1=1"
    assert "unsupported operator" in warning


def test_smart_query_multiple_conditions():
    where, params, warning = _build_smart_query(
        {
            "conditions": {
                "data": [
                    {"field": "rating", "operator": ">=", "value": "80"},
                    {"field": "playCount", "operator": ">", "value": "0"},
                ]
            }
        }
    )
    assert "s.Rating >= ?" in where
    assert "s.PlayCounter > ?" in where
    assert params == ["80", "0"]
    assert warning == ""


# ── _build_order_clause ───────────────────────────────────────────────────────


def test_order_clause_playcount_desc():
    clause = _build_order_clause([{"field": "playcount", "ascending": False}])
    assert clause == "ORDER BY s.PlayCounter DESC"


def test_order_clause_random():
    clause = _build_order_clause([{"field": "random", "ascending": True}])
    assert "RANDOM()" in clause


def test_order_clause_fallback():
    clause = _build_order_clause([])
    assert clause == "ORDER BY s.SongTitle ASC"


def test_order_clause_unknown_field_skipped():
    clause = _build_order_clause([{"field": "unknown", "ascending": True}])
    assert clause == "ORDER BY s.SongTitle ASC"


# ── generate_m3u8 ─────────────────────────────────────────────────────────────


def _make_playlist(tracks: list[LocalFile]) -> Playlist:
    return Playlist(
        id=1,
        name="Test",
        is_auto=False,
        tracks=[PlaylistTrack(file=f, song_id=i) for i, f in enumerate(tracks)],
    )


def test_generate_m3u8_header():
    pl = _make_playlist([_lf("song.mp3")])
    content = generate_m3u8(pl)
    assert content.startswith("#EXTM3U\n")


def test_generate_m3u8_bare_filename():
    f = LocalFile(
        path=Path("/mnt/d/Music/song.mp3"), name="song.mp3", size=1000, duration_ms=210000
    )
    pl = _make_playlist([f])
    lines = generate_m3u8(pl).splitlines()
    assert lines[2] == "song.mp3"  # bare filename, not full path


def test_generate_m3u8_extinf_duration():
    f = LocalFile(path=Path("/mnt/d/x.mp3"), name="x.mp3", size=0, duration_ms=211000)
    pl = _make_playlist([f])
    lines = generate_m3u8(pl).splitlines()
    assert lines[1].startswith("#EXTINF:211,")  # 211000ms → 211s


def test_generate_m3u8_uses_title():
    f = LocalFile(
        path=Path("/mnt/d/track01.mp3"), name="track01.mp3", size=0, duration_ms=0, title="My Song"
    )
    pl = _make_playlist([f])
    extinf = generate_m3u8(pl).splitlines()[1]
    assert extinf.endswith(",My Song")


def test_generate_m3u8_falls_back_to_name():
    f = LocalFile(path=Path("/mnt/d/track01.mp3"), name="track01.mp3", size=0)
    pl = _make_playlist([f])
    extinf = generate_m3u8(pl).splitlines()[1]
    assert extinf.endswith(",track01.mp3")


def test_generate_m3u8_multiple_tracks():
    tracks = [
        LocalFile(path=Path(f"/mnt/d/{i}.mp3"), name=f"{i}.mp3", size=0, duration_ms=60000)
        for i in range(3)
    ]
    pl = _make_playlist(tracks)
    lines = [ln for ln in generate_m3u8(pl).splitlines() if ln]
    assert len(lines) == 1 + 3 * 2  # header + (EXTINF + filename) per track


# ── scan_playlists_from_mediamonkey ───────────────────────────────────────────


def test_scan_playlists_missing_db():
    with pytest.raises(FileNotFoundError):
        scan_playlists_from_mediamonkey(Path("/nonexistent/MM5.DB"))


def test_scan_playlists_static(mm_db: Path):
    """Static playlist resolved from PlaylistSongs in correct order."""
    conn = sqlite3.connect(str(mm_db))
    conn.create_collation(
        "IUNICODE", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())
    )
    conn.executescript("""
        INSERT INTO Playlists VALUES (1, 'My Mix', 0, NULL);
        INSERT INTO PlaylistSongs VALUES (1, 1, 2, 0);
        INSERT INTO PlaylistSongs VALUES (2, 1, 1, 1);
    """)
    conn.close()

    playlists = scan_playlists_from_mediamonkey(mm_db)
    pl = next(p for p in playlists if p.name == "My Mix")
    assert not pl.is_auto
    assert len(pl.tracks) == 2
    # Order follows SongOrder: IDSong=2 first, IDSong=1 second
    assert pl.tracks[0].file.name == "song_b.mp3"
    assert pl.tracks[1].file.name == "song_a.mp3"
    assert pl.unsupported_reason == ""


def test_scan_playlists_smart_rating(mm_db: Path):
    """Smart playlist filtered by rating >= 80 returns only song_a and vivaldi."""
    import json

    conn = sqlite3.connect(str(mm_db))
    conn.create_collation(
        "IUNICODE", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())
    )
    qdata = json.dumps(
        {
            "conditions": {"data": [{"field": "rating", "operator": ">=", "value": "80"}]},
            "sortOrders": [{"field": "title", "ascending": True}],
        }
    )
    conn.execute("INSERT INTO Playlists VALUES (2, 'Top Rated', 1, ?)", (qdata,))
    conn.commit()
    conn.close()

    playlists = scan_playlists_from_mediamonkey(mm_db)
    pl = next(p for p in playlists if p.name == "Top Rated")
    names = {t.file.name for t in pl.tracks}
    assert "song_a.mp3" in names  # rating=80
    assert "vivaldi.mp3" in names  # rating=90
    assert "song_b.mp3" not in names  # rating=60


def test_scan_playlists_smart_file_limit(mm_db: Path):
    """Smart playlist with files limit=1 returns exactly 1 track."""
    import json

    conn = sqlite3.connect(str(mm_db))
    conn.create_collation(
        "IUNICODE", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())
    )
    qdata = json.dumps(
        {
            "conditions": {"data": []},
            "limit": "files",
            "limitValue": 1,
            "sortOrders": [{"field": "title", "ascending": True}],
        }
    )
    conn.execute("INSERT INTO Playlists VALUES (3, 'One Track', 1, ?)", (qdata,))
    conn.commit()
    conn.close()

    playlists = scan_playlists_from_mediamonkey(mm_db)
    pl = next(p for p in playlists if p.name == "One Track")
    assert len(pl.tracks) == 1


def test_scan_playlists_duration_ms(mm_db: Path):
    """Tracks resolved from playlist have correct duration_ms from SongLength."""
    conn = sqlite3.connect(str(mm_db))
    conn.create_collation(
        "IUNICODE", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())
    )
    conn.executescript("""
        INSERT INTO Playlists VALUES (4, 'Duration Test', 0, NULL);
        INSERT INTO PlaylistSongs VALUES (10, 4, 1, 0);
    """)
    conn.close()

    playlists = scan_playlists_from_mediamonkey(mm_db)
    pl = next(p for p in playlists if p.name == "Duration Test")
    assert pl.tracks[0].file.duration_ms == 210000  # song_a SongLength


# ── _canonical_vlc_name ───────────────────────────────────────────────────────


def test_canonical_vlc_name_no_suffix():
    assert _canonical_vlc_name("song.mp3") == "song.mp3"


def test_canonical_vlc_name_single_digit():
    assert _canonical_vlc_name("song-1.mp3") == "song.mp3"


def test_canonical_vlc_name_multi_digit():
    assert _canonical_vlc_name("song-12.mp3") == "song.mp3"


def test_canonical_vlc_name_no_extension():
    assert _canonical_vlc_name("song-1") == "song"


def test_canonical_vlc_name_hyphen_in_base():
    # Hyphen already in the base name; only the trailing -N is stripped
    assert _canonical_vlc_name("my-song-1.flac") == "my-song.flac"


def test_canonical_vlc_name_no_change_non_numeric():
    assert _canonical_vlc_name("song-remix.mp3") == "song-remix.mp3"


# ── find_potential_duplicates ─────────────────────────────────────────────────


def test_find_potential_duplicates_empty():
    assert find_potential_duplicates([]) == {}


def test_find_potential_duplicates_no_dups():
    assert find_potential_duplicates([_vf("song.mp3"), _vf("other.flac")]) == {}


def test_find_potential_duplicates_basic_pair():
    result = find_potential_duplicates([_vf("song.mp3"), _vf("song-1.mp3")])
    assert list(result.keys()) == ["song.mp3"]
    assert [f.filename for f in result["song.mp3"]] == ["song-1.mp3", "song.mp3"]


def test_find_potential_duplicates_three_way_chain():
    files = [_vf("song.mp3"), _vf("song-1.mp3"), _vf("song-2.mp3")]
    result = find_potential_duplicates(files)
    assert len(result) == 1
    assert len(result["song.mp3"]) == 3


def test_find_potential_duplicates_numbered_only_no_original():
    # song.mp3 was deleted but song-1.mp3 and song-2.mp3 remain - still duplicates
    result = find_potential_duplicates([_vf("song-1.mp3"), _vf("song-2.mp3")])
    assert list(result.keys()) == ["song.mp3"]
    assert len(result["song.mp3"]) == 2


def test_find_potential_duplicates_multiple_groups():
    files = [
        _vf("a.mp3"),
        _vf("a-1.mp3"),
        _vf("b.flac"),
        _vf("b-1.flac"),
        _vf("c.mp3"),  # no duplicate
    ]
    result = find_potential_duplicates(files)
    assert set(result.keys()) == {"a.mp3", "b.flac"}
    assert "c.mp3" not in result


def test_find_potential_duplicates_same_filename_not_flagged():
    # VLC XML lists the same file twice - should NOT be treated as a duplicate
    assert find_potential_duplicates([_vf("song.mp3"), _vf("song.mp3")]) == {}


def test_find_potential_duplicates_sorted_within_group():
    # Files returned within each group are sorted by filename
    result = find_potential_duplicates([_vf("song-2.mp3"), _vf("song.mp3"), _vf("song-1.mp3")])
    names = [f.filename for f in result["song.mp3"]]
    assert names == sorted(names)
