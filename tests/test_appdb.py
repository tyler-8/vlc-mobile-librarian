"""Tests for vlc_mobile_librarian.appdb - config persistence to the app's SQLite DB."""

from __future__ import annotations

from pathlib import Path

import pytest

import vlc_mobile_librarian.appdb as appdb
from vlc_mobile_librarian.appdb import (
    TrackTypeConfig,
    get_included_track_types,
    load_track_type_configs,
    save_track_type_configs,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the app DB to a temp file for every test."""
    monkeypatch.setattr(appdb, "_APP_DB", tmp_path / "test_app.db")


# ── load / save roundtrip ─────────────────────────────────────────────────────


def test_load_empty_returns_empty_list():
    assert load_track_type_configs() == []


def test_save_and_load_roundtrip():
    configs = [
        TrackTypeConfig(0, "Music", True),
        TrackTypeConfig(3, "Classical", True),
        TrackTypeConfig(1, "Podcasts", False),
    ]
    save_track_type_configs(configs)
    loaded = load_track_type_configs()
    assert len(loaded) == 3
    by_type = {c.track_type: c for c in loaded}
    assert by_type[0].label == "Music"
    assert by_type[0].include is True
    assert by_type[1].label == "Podcasts"
    assert by_type[1].include is False
    assert by_type[3].label == "Classical"
    assert by_type[3].include is True


def test_save_replaces_all_existing():
    save_track_type_configs(
        [
            TrackTypeConfig(0, "Music", True),
            TrackTypeConfig(3, "Classical", True),
        ]
    )
    # Save a smaller set - old rows should be gone
    save_track_type_configs([TrackTypeConfig(0, "Music", False)])
    loaded = load_track_type_configs()
    assert len(loaded) == 1
    assert loaded[0].track_type == 0
    assert loaded[0].include is False


def test_save_empty_clears_all():
    save_track_type_configs([TrackTypeConfig(0, "Music", True)])
    save_track_type_configs([])
    assert load_track_type_configs() == []


def test_load_returns_ordered_by_track_type():
    save_track_type_configs(
        [
            TrackTypeConfig(3, "Classical", True),
            TrackTypeConfig(0, "Music", True),
            TrackTypeConfig(1, "Podcasts", False),
        ]
    )
    loaded = load_track_type_configs()
    assert [c.track_type for c in loaded] == [0, 1, 3]


# ── get_included_track_types ──────────────────────────────────────────────────


def test_get_included_returns_none_when_no_config():
    assert get_included_track_types() is None


def test_get_included_returns_only_included():
    save_track_type_configs(
        [
            TrackTypeConfig(0, "Music", True),
            TrackTypeConfig(1, "Podcasts", False),
            TrackTypeConfig(3, "Classical", True),
        ]
    )
    result = get_included_track_types()
    assert result == [0, 3]


def test_get_included_returns_empty_list_when_all_excluded():
    save_track_type_configs([TrackTypeConfig(0, "Music", False)])
    result = get_included_track_types()
    # Returns [] (not None) - caller must distinguish from "no config"
    assert result == []
    assert result is not None
