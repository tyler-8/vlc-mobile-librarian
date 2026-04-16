from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from vlc_mobile_librarian.models import LibraryCategory, LocalFile, Playlist


@dataclass
class ConfigField:
    """Describes one configuration input required by a LibrarySource.

    Used by app.py to render source-specific setup UI generically - no
    source-specific code in the app layer.

    Attributes:
        key:         Key in the settings dict passed to from_settings().
        label:       Human-readable UI label.
        field_type:  Controls which Streamlit widget is rendered.
        placeholder: Hint text shown when the field is empty.
        default:     Static fallback used when autodetect returns None.
        autodetect:  Optional callable that attempts to find the value
                     automatically (e.g. scanning well-known install paths).
                     Returns the detected value or None.
    """

    key: str
    label: str
    field_type: Literal["path", "text", "password", "integer"] = "text"
    placeholder: str = ""
    default: Any = ""
    autodetect: Callable[[], Any] | None = None


class LibrarySource(ABC):
    """Abstract interface every music library integration must implement.

    Subclasses MUST define a class-level ``name`` string, e.g.::

        class MySource(LibrarySource):
            name = "MySource"

    Lifecycle:
      1. Call config_fields() (classmethod) to discover what config the user
         must provide.  The app renders these fields generically.
      2. Call from_settings(config) (classmethod) to construct an instance
         from the filled-in config dict.
      3. Call is_available() to check the source is reachable before scanning.
      4. Call discover_categories() to let the user configure what to include.
         Returns [] if the source has no category concept - app hides that section.
      5. Call scan_library(categories) and/or scan_playlists(categories) with the
         user's selected category ids (list[str]).

    All scan methods may raise SourceError on read failure.
    """

    # Every concrete subclass must set this as a class attribute.
    name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Only enforce on concrete (non-abstract) subclasses.
        if not getattr(cls, "__abstractmethods__", None) and "name" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must define class attribute 'name' (e.g. name = 'MySource')"
            )

    # ── Plugin registration protocol ─────────────────────────────────────────

    @classmethod
    @abstractmethod
    def config_fields(cls) -> list[ConfigField]:
        """Return the list of config inputs this source requires.

        The app iterates these to render a generic setup form and to
        auto-populate defaults via each field's autodetect callable.
        """

    @classmethod
    @abstractmethod
    def from_settings(cls, config: dict[str, Any]) -> LibrarySource:
        """Construct an instance from the config dict produced by config_fields().

        config keys match ConfigField.key values.  Raises ValueError if
        required keys are missing or invalid.
        """

    # ── Scan protocol ─────────────────────────────────────────────────────────

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the source is configured and reachable (non-destructive)."""

    @abstractmethod
    def discover_categories(self) -> list[LibraryCategory]:
        """Return available categories for user configuration.

        Returns [] if this source does not support categories, in which case
        scan_library() and scan_playlists() ignore the categories argument.

        Raises SourceError on read failure.
        """

    @abstractmethod
    def scan_library(self, categories: list[str] | None = None) -> list[LocalFile]:
        """Return local audio files, optionally filtered by category ids.

        categories: list of LibraryCategory.id values to include.
                    None means "use source default".
                    [] means "include nothing" - returns [].

        Results are sorted consistently (e.g. artist / album / name).
        Raises SourceError on read failure.
        """

    @abstractmethod
    def scan_playlists(self, categories: list[str] | None = None) -> list[Playlist]:
        """Return all playlists with resolved track lists.

        categories: same semantics as scan_library.
        Raises SourceError on read failure.
        """


class SourceError(Exception):
    """Raised by LibrarySource implementations on read or configuration failures."""
