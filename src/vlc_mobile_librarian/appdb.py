from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_APP_DB = Path.home() / ".config" / "vlc-mobile-librarian" / "app.db"


# ── Generic API ───────────────────────────────────────────────────────────────


@dataclass
class CategoryConfig:
    """Persisted user configuration for one library category.

    source_name: the LibrarySource.name string (e.g. "MediaMonkey").
    category_id: the LibraryCategory.id string (e.g. "0", "3" for MM track types).
    """

    source_name: str
    category_id: str
    label: str
    include: bool


def _conn() -> sqlite3.Connection:
    _APP_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_APP_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS category_config (
            source_name TEXT    NOT NULL,
            category_id TEXT    NOT NULL,
            label       TEXT    NOT NULL DEFAULT '',
            include     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (source_name, category_id)
        );
    """)
    conn.commit()
    _migrate_track_type_config(conn)
    return conn


def _migrate_track_type_config(conn: sqlite3.Connection) -> None:
    """One-time migration: copy track_type_config rows → category_config, then drop."""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "track_type_config" not in tables:
        return
    existing = conn.execute("SELECT track_type, label, include FROM track_type_config").fetchall()
    for track_type, label, include in existing:
        conn.execute(
            """
            INSERT OR IGNORE INTO category_config (source_name, category_id, label, include)
            VALUES (?, ?, ?, ?)
            """,
            ("MediaMonkey", str(track_type), label, include),
        )
    conn.execute("DROP TABLE track_type_config")
    conn.commit()


def load_category_configs(source_name: str) -> list[CategoryConfig]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT source_name, category_id, label, include"
            " FROM category_config"
            " WHERE source_name = ?"
            " ORDER BY category_id",
            (source_name,),
        ).fetchall()
    return [CategoryConfig(r[0], r[1], r[2], bool(r[3])) for r in rows]


def save_category_configs(source_name: str, configs: list[CategoryConfig]) -> None:
    """Replace all saved category configs for the given source."""
    with _conn() as conn:
        conn.execute("DELETE FROM category_config WHERE source_name = ?", (source_name,))
        conn.executemany(
            "INSERT INTO category_config (source_name, category_id, label, include)"
            " VALUES (?, ?, ?, ?)",
            [(c.source_name, c.category_id, c.label, int(c.include)) for c in configs],
        )


def get_included_category_ids(source_name: str) -> list[str] | None:
    """Return the list of category_id strings the user has marked for inclusion.

    Returns None (not []) when no configuration has been saved yet, so callers
    can distinguish "user explicitly excluded everything" from "no config yet".
    """
    configs = load_category_configs(source_name)
    if not configs:
        return None
    return [c.category_id for c in configs if c.include]


# ── Backward-compat wrappers (MediaMonkey-specific) ───────────────────────────
# These allow existing code and tests that use the old TrackTypeConfig API to
# continue working without modification.


@dataclass
class TrackTypeConfig:
    track_type: int
    label: str
    include: bool


def load_track_type_configs() -> list[TrackTypeConfig]:
    configs = load_category_configs("MediaMonkey")
    result: list[TrackTypeConfig] = []
    for c in configs:
        with contextlib.suppress(ValueError):
            result.append(TrackTypeConfig(int(c.category_id), c.label, c.include))
    result.sort(key=lambda c: c.track_type)
    return result


def save_track_type_configs(configs: list[TrackTypeConfig]) -> None:
    """Replace all saved track type configs with the given list."""
    category_configs = [
        CategoryConfig("MediaMonkey", str(c.track_type), c.label, c.include) for c in configs
    ]
    save_category_configs("MediaMonkey", category_configs)


def get_included_track_types() -> list[int] | None:
    """Return the list of track_type ints the user has marked for inclusion.

    Returns None (not []) when no configuration has been saved yet, so callers
    can distinguish "user explicitly excluded everything" from "no config yet".
    """
    ids = get_included_category_ids("MediaMonkey")
    if ids is None:
        return None
    result: list[int] = []
    for cat_id in ids:
        with contextlib.suppress(ValueError):
            result.append(int(cat_id))
    return result
