from vlc_mobile_librarian.sources.base import ConfigField, LibrarySource, SourceError
from vlc_mobile_librarian.sources.mediamonkey import MediaMonkeySource, find_mediamonkey_db

# Registry of available source plugins.
# To add a new source: create sources/<name>.py, implement LibrarySource
# (including name, config_fields, and from_settings), then add the class here.
# app.py requires no changes.
AVAILABLE_SOURCES: list[type[LibrarySource]] = [
    MediaMonkeySource,
]

__all__ = [
    "ConfigField",
    "LibrarySource",
    "SourceError",
    "MediaMonkeySource",
    "find_mediamonkey_db",
    "AVAILABLE_SOURCES",
]
