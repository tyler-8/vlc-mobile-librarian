"""NiceGUI front-end for VLC Mobile Librarian.

Replaces the former Streamlit app. The backend (transcode / uploader / vlc_client /
library / sources) is untouched and UI-agnostic: `start_upload_job` runs the work on
a daemon thread and pushes `UploadEvent`s onto a `queue.Queue`. Here a single
`ui.timer` drains that queue and mutates only the affected DOM elements in place — no
full-page rerun, which is what made the Streamlit version struggle with thousands of
files.

State is per-tab: everything mutable lives on an `AppState` instance created inside the
`@ui.page('/')` handler and captured by closures. This is a local single-user tool, so a
single tab is assumed (see the migration plan for how to make uploads survive a refresh).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from nicegui import run, ui

from vlc_mobile_librarian.appdb import (
    CategoryConfig,
    get_included_category_ids,
    load_category_configs,
    save_category_configs,
)
from vlc_mobile_librarian.library import (
    LocalFile,
    Playlist,
    SyncPlan,
    build_vlc_index,
    classify_local_file,
    compute_sync_plan,
    find_potential_duplicates,
    generate_m3u8,
    match_device_file,
)
from vlc_mobile_librarian.sources import AVAILABLE_SOURCES, LibrarySource
from vlc_mobile_librarian.transcode import (
    TranscodeOptions,
    cache_file_count,
    cache_size_bytes,
    clear_cache,
    default_workers,
    ffmpeg_available,
    requires_transcode,
    transcoded_name,
)
from vlc_mobile_librarian.uploader import (
    InMemoryFile,
    UploadItem,
    UploadJob,
    UploadStatus,
    start_upload_job,
)
from vlc_mobile_librarian.vlc_client import (
    VLCAuthError,
    VLCConnection,
    VLCConnectionError,
    VLCFile,
    authenticate,
    fetch_file_list,
)

# ── Persistent settings ───────────────────────────────────────────────────────

_SETTINGS_FILE = Path.home() / ".config" / "vlc-mobile-librarian" / "settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(data: dict) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2))


# ── Logging ───────────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Route vlc_mobile_librarian logs to the terminal (stderr).

    Level is controlled by the VLC_LIBRARIAN_LOG_LEVEL env var (default INFO; set
    to DEBUG for cache hits, encode-wait, and per-chunk detail).
    """
    pkg_logger = logging.getLogger("vlc_mobile_librarian")
    level_name = os.environ.get("VLC_LIBRARIAN_LOG_LEVEL", "INFO").upper()
    pkg_logger.setLevel(getattr(logging, level_name, logging.INFO))
    if not pkg_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
            )
        )
        pkg_logger.addHandler(handler)
    pkg_logger.propagate = False


_configure_logging()

# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_source_cls(name: str) -> type[LibrarySource]:
    """Return the source class matching name, falling back to the first registered."""
    for cls in AVAILABLE_SOURCES:
        if cls.name == name:
            return cls
    return AVAILABLE_SOURCES[0]


def _fmt_size(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1_024:
        return f"{n / 1_024:.1f} KB"
    return f"{n} B"


def _fmt_duration(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _active_category_ids(source_name: str) -> tuple[str, ...]:
    """Return the configured category ids for the active source, or the source default."""
    included = get_included_category_ids(source_name)
    return tuple(included if included is not None else ["0", "3"])


# ── Per-tab application state ──────────────────────────────────────────────────


@dataclass
class AppState:
    """All mutable UI state for one browser tab (replaces Streamlit's session_state)."""

    settings: dict = field(default_factory=dict)

    # Connection / device
    vlc_conn: VLCConnection | None = None
    vlc_session: requests.Session | None = None
    vlc_files: list[VLCFile] | None = None

    # Library / sync
    active_source_name: str = ""
    source_config: dict[str, Any] = field(default_factory=dict)
    sync_plan: SyncPlan | None = None
    playlists: list[Playlist] | None = None

    # In-flight jobs
    upload_job: UploadJob | None = None
    playlist_upload_job: UploadJob | None = None

    # Upload settings
    upload_concurrency: int = 2

    # Transcode settings
    transcode_enabled: bool = False
    transcode_bitrate: int = 128
    transcode_workers: int = field(default_factory=default_workers)
    transcode_cache_cap_gb: float = 5.0

    def transcode_opts(self) -> TranscodeOptions:
        return TranscodeOptions(
            enabled=bool(self.transcode_enabled),
            bitrate_kbps=int(self.transcode_bitrate),
            max_workers=int(self.transcode_workers),
            cache_cap_bytes=int(float(self.transcode_cache_cap_gb) * 1024**3),
        )

    def name_projector(self) -> Callable[[LocalFile], str] | None:
        """Map a LocalFile to its uploaded filename, or None (identity).

        When transcoding is on, lossless files upload under a `.opus` name; this keeps
        duplicate detection and playlist references aligned with what lands on the device.
        """
        if not self.transcode_enabled:
            return None
        return lambda f: transcoded_name(f.name) if requires_transcode(f.path) else f.name

    def reset_sync(self) -> None:
        self.sync_plan = None
        self.playlists = None
        self.upload_job = None
        self.playlist_upload_job = None


def _initial_state() -> AppState:
    """Build an AppState seeded from the persisted settings file."""
    s = _load_settings()
    default_cls = _get_source_cls(s.get("active_source_name", AVAILABLE_SOURCES[0].name))

    saved_cfg: dict[str, Any] = s.get("source_config", {})
    config: dict[str, Any] = {}
    for f in default_cls.config_fields():
        if f.key in saved_cfg:
            config[f.key] = saved_cfg[f.key]
        else:
            detected = f.autodetect() if f.autodetect else None
            config[f.key] = str(detected) if detected is not None else str(f.default)

    return AppState(
        settings=s,
        active_source_name=default_cls.name,
        source_config=config,
        transcode_enabled=s.get("transcode_enabled", False) and ffmpeg_available(),
        transcode_bitrate=s.get("transcode_bitrate", 128),
        transcode_workers=s.get("transcode_workers", default_workers()),
        transcode_cache_cap_gb=s.get("transcode_cache_cap_gb", 5.0),
        upload_concurrency=s.get("upload_concurrency", 2),
    )


# ── Upload progress: the timer-driven loop that replaces st.rerun() ────────────

_LOG_COLUMNS = [
    {"name": "artist", "label": "Artist", "field": "artist", "align": "left"},
    {"name": "title", "label": "Title", "field": "title", "align": "left"},
    {"name": "size", "label": "Size", "field": "size", "align": "right"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
]

# Status -> display glyph for the summary line and log rows.
_STATUS_GLYPH = {
    UploadStatus.TRANSCODING: "🎛️ transcoding…",
    UploadStatus.UPLOADING: "⬆️ uploading",
    UploadStatus.DONE: "✅ done",
    UploadStatus.ERROR: "❌ error",
}


def _window_rows(job: UploadJob, recent_cap: int = 50) -> list[dict]:
    """Rows for the live log: every active file + the most recent finished ones.

    Rendering one row per file for thousands of files is what bloated the old UI. We
    instead show only files currently transcoding/uploading plus the last `recent_cap`
    that finished — the aggregate bar carries the rest.
    """
    active: list[dict] = []
    recent: list[dict] = []
    for f in job.files:
        ev = job.file_status.get(f.name)
        if ev is None:
            continue  # still queued — represented in the summary counts, not the log
        if ev.status == UploadStatus.UPLOADING and ev.bytes_total > 0:
            pct = int(100 * ev.bytes_sent / ev.bytes_total)
            status = f"⬆️ {pct}%"
        elif ev.status == UploadStatus.ERROR:
            status = f"❌ {ev.error_msg}" if ev.error_msg else "❌ error"
        else:
            status = _STATUS_GLYPH.get(ev.status, "⏳ queued")
        row = {
            "name": f.name,
            "artist": f.artist or "-",
            "title": f.title or f.name,
            "size": _fmt_size(f.size),
            "status": status,
        }
        if ev.status in (UploadStatus.TRANSCODING, UploadStatus.UPLOADING):
            active.append(row)
        else:
            recent.append(row)
    return active + recent[-recent_cap:]


class ProgressView:
    """Renders one UploadJob's progress and drives it with a single ui.timer.

    The timer callback drains `job.events` (written by the uploader thread) and mutates
    the bar / label / table in place. When the batch finishes it deactivates itself and
    invokes `on_done` so the caller can show completion controls.
    """

    def __init__(self, container: ui.element, title: str, on_done: Callable[[UploadJob], None]):
        self.container = container
        self.title = title
        self.on_done = on_done
        self.job: UploadJob | None = None
        self.timer: ui.timer | None = None
        self.bar: ui.linear_progress | None = None
        self.label: ui.label | None = None
        self.table: ui.table | None = None

    def start(self, job: UploadJob) -> None:
        self.stop()
        self.job = job
        self.container.clear()
        with self.container:
            ui.label(self.title).classes("text-lg font-medium")
            self.bar = ui.linear_progress(value=0.0, show_value=False, size="20px").classes(
                "w-full"
            )
            self.label = ui.label("Starting…").classes("text-sm text-grey-7")
            self.table = (
                ui.table(columns=_LOG_COLUMNS, rows=[], row_key="name")
                .props("dense flat bordered virtual-scroll")
                .classes("w-full")
                .style("max-height: 420px")
            )
        self.timer = ui.timer(0.15, self._tick)

    def _tick(self) -> None:
        job = self.job
        if job is None:
            return
        while not job.events.empty():
            ev = job.events.get_nowait()
            if ev.file_name == "":
                job.is_done = True
            else:
                job.file_status[ev.file_name] = ev

        statuses = [e.status for e in job.file_status.values()]
        done = sum(s == UploadStatus.DONE for s in statuses)
        errors = sum(s == UploadStatus.ERROR for s in statuses)
        transcoding = sum(s == UploadStatus.TRANSCODING for s in statuses)
        uploading = sum(s == UploadStatus.UPLOADING for s in statuses)
        total = len(job.files)
        finished = done + errors

        parts = [f"{finished} / {total} done"]
        if transcoding:
            parts.append(f"🎛️ {transcoding} transcoding")
        if uploading:
            parts.append(f"⬆️ {uploading} uploading")
        if errors:
            parts.append(f"❌ {errors} failed")

        assert self.bar is not None and self.label is not None and self.table is not None
        self.bar.value = finished / total if total else 1.0
        self.label.text = "  ·  ".join(parts)
        self.table.rows = _window_rows(job)
        self.table.update()

        if job.is_done:
            self.stop()
            self.on_done(job)

    def stop(self) -> None:
        if self.timer is not None:
            self.timer.deactivate()
            self.timer.delete()
            self.timer = None


# ── Page ──────────────────────────────────────────────────────────────────────


@dataclass
class PageCtx:
    """Shared operations a section renderer may need to trigger on the whole page.

    Lets the section renderers live at module scope (testable, readable) while still
    reaching the page-level closures that re-render content or talk to the device.
    """

    state: AppState
    refresh_main: Callable[[], None]
    reload_library: Callable[[], Awaitable[None]]
    fetch_device: Callable[[], Awaitable[bool]]


@ui.page("/")
def index() -> None:
    state = _initial_state()
    ui.page_title("VLC Wi-Fi Sync")
    dark = ui.dark_mode(value=bool(state.settings.get("dark_mode", False)))

    def _toggle_dark(v: bool) -> None:
        dark.value = bool(v)
        state.settings["dark_mode"] = bool(v)
        _save_settings(state.settings)
        _apply_drawer_bg(bool(v))

    # ── Sidebar: connection + transcoding ──────────────────────────────────────
    drawer = ui.left_drawer(value=True, fixed=True).style("width: 340px")

    def _apply_drawer_bg(is_dark: bool) -> None:
        """Keep the sidebar background in step with the theme so its text stays readable."""
        drawer.classes(add="bg-grey-9" if is_dark else "bg-grey-2",
                       remove="bg-grey-2" if is_dark else "bg-grey-9")

    _apply_drawer_bg(bool(state.settings.get("dark_mode", False)))

    with drawer:
        active_cls = _get_source_cls(state.active_source_name)
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label("VLC Wi-Fi Sync").classes("text-xl font-bold")
            ui.switch(
                value=bool(state.settings.get("dark_mode", False)),
                on_change=lambda e: _toggle_dark(e.value),
            ).props("icon=dark_mode dense").tooltip("Toggle dark mode")
        ui.label(f"Sync your {active_cls.name} library to VLC on iPhone.").classes(
            "text-xs text-grey-7"
        )
        ui.separator()

        ui.label("VLC Device").classes("font-medium")
        host_in = ui.input("IP Address", value=state.settings.get("vlc_host", "")).props(
            'placeholder="192.168.1.xx"'
        ).classes("w-full")
        port_in = ui.number(
            "Port", value=state.settings.get("vlc_port", 80), min=1, max=65535
        ).classes("w-full")
        pass_in = ui.input("Passcode (optional)", password=True).classes("w-full")
        auto_load_in = ui.checkbox(
            "Load library on connect", value=state.settings.get("auto_load_library", True)
        )

        ui.separator()
        ui.label("Transcoding").classes("font-medium")
        ffmpeg_ok = ffmpeg_available()
        tc_enable_in = ui.checkbox(
            "Convert lossless → Opus before upload",
            value=state.transcode_enabled,
            on_change=lambda e: _set_transcode_enabled(e.value),
        )
        tc_enable_in.set_enabled(ffmpeg_ok)
        if not ffmpeg_ok:
            ui.label("Install ffmpeg to enable transcoding.").classes("text-xs text-grey-7")
        bitrate_in = ui.select(
            [96, 128, 160, 192], value=state.transcode_bitrate, label="Opus bitrate (kbps)"
        ).classes("w-full")
        workers_in = ui.number(
            "Parallel encodes", value=int(state.transcode_workers), min=1, max=32
        ).classes("w-full")
        cache_cap_in = ui.number(
            "Cache size limit (GB)",
            value=float(state.transcode_cache_cap_gb),
            min=0,
            max=1000,
            step=0.5,
        ).classes("w-full")
        # Independent of transcoding - how many tracks upload at once.
        uploads_in = ui.number(
            "Parallel uploads", value=int(state.upload_concurrency), min=1, max=8
        ).classes("w-full")

        def _set_transcode_enabled(v: bool) -> None:
            state.transcode_enabled = bool(v)
            for w in (bitrate_in, workers_in):
                w.set_enabled(bool(v) and ffmpeg_ok)

        _set_transcode_enabled(state.transcode_enabled)

        cache_caption = ui.label().classes("text-xs text-grey-7")

        def _refresh_cache_caption() -> None:
            cache_caption.text = (
                f"Cache: {_fmt_size(cache_size_bytes())} · {cache_file_count()} file(s)"
            )

        def _clear_cache() -> None:
            if state.upload_job is not None or state.playlist_upload_job is not None:
                ui.notify("Can't clear the cache while a sync is in progress.", type="warning")
                return
            freed = clear_cache()
            _refresh_cache_caption()
            ui.notify(f"Cleared {_fmt_size(freed)} of cached encodes", type="positive")

        ui.button("Clear transcode cache", on_click=_clear_cache).props(
            "flat dense"
        ).classes("w-full").set_enabled(ffmpeg_ok)
        _refresh_cache_caption()

        def _gather_settings() -> dict:
            return {
                **state.settings,
                "vlc_host": (host_in.value or "").strip(),
                "vlc_port": int(port_in.value or 80),
                "auto_load_library": bool(auto_load_in.value),
                "active_source_name": state.active_source_name,
                "source_config": state.source_config,
                "transcode_enabled": bool(state.transcode_enabled),
                "transcode_bitrate": int(bitrate_in.value),
                "transcode_workers": int(workers_in.value),
                "transcode_cache_cap_gb": float(cache_cap_in.value),
                "upload_concurrency": int(uploads_in.value),
            }

        def _sync_transcode_from_widgets() -> None:
            state.transcode_bitrate = int(bitrate_in.value)
            state.transcode_workers = int(workers_in.value)
            state.transcode_cache_cap_gb = float(cache_cap_in.value)
            state.upload_concurrency = int(uploads_in.value)

        connect_btn = ui.button("Connect").props("color=primary").classes("w-full")
        device_caption = ui.label().classes("text-xs text-grey-7")

        async def _connect() -> None:
            host = (host_in.value or "").strip()
            if not host:
                ui.notify("Enter the VLC device IP address.", type="negative")
                return
            _sync_transcode_from_widgets()
            conn = VLCConnection(
                host=host, port=int(port_in.value or 80), passcode=pass_in.value or None
            )
            connect_btn.props("loading")
            try:
                session = await run.io_bound(authenticate, conn)
                vlc_files = await run.io_bound(fetch_file_list, conn, session)
            except VLCAuthError as e:
                ui.notify(f"Auth failed: {e}", type="negative")
                return
            except VLCConnectionError as e:
                ui.notify(str(e), type="negative")
                return
            finally:
                connect_btn.props(remove="loading")

            state.vlc_conn = conn
            state.vlc_session = session
            state.vlc_files = vlc_files
            state.reset_sync()
            state.settings = _gather_settings()
            _save_settings(state.settings)
            ui.notify(f"Connected — {len(vlc_files)} file(s) on device.", type="positive")
            _refresh_device_caption()

            source_cls = _get_source_cls(state.active_source_name)
            if auto_load_in.value and source_cls.from_settings(state.source_config).is_available():
                await _load_library()
            main.refresh()

        connect_btn.on_click(_connect)

        async def _fetch_device() -> bool:
            """Re-fetch the device file list into state. Returns True on success."""
            if state.vlc_conn is None:
                return False
            try:
                state.vlc_files = await run.io_bound(
                    fetch_file_list, state.vlc_conn, state.vlc_session
                )
            except VLCConnectionError as e:
                ui.notify(str(e), type="negative")
                return False
            _refresh_device_caption()
            return True

        async def _refresh_device_list() -> None:
            if await _fetch_device():
                state.reset_sync()
                main.refresh()

        refresh_btn = ui.button("Refresh file list", on_click=_refresh_device_list).props(
            "flat dense"
        ).classes("w-full")

        def _refresh_device_caption() -> None:
            if state.vlc_files is None:
                device_caption.text = ""
                refresh_btn.set_visibility(False)
            else:
                device_caption.text = f"{len(state.vlc_files)} file(s) on device"
                refresh_btn.set_visibility(True)

        _refresh_device_caption()

    # ── Library loading (shared by connect-auto-load and the Load button) ──────
    async def _load_library() -> None:
        source_cls = _get_source_cls(state.active_source_name)
        try:
            cat_ids = list(_active_category_ids(state.active_source_name))
            source = source_cls.from_settings(state.source_config)
            local_files = await run.io_bound(source.scan_library, cat_ids)
            state.sync_plan = compute_sync_plan(
                local_files, state.vlc_files or [], project_name=state.name_projector()
            )
            playlist_source = source_cls.from_settings(state.source_config)
            state.playlists = await run.io_bound(playlist_source.scan_playlists, cat_ids)
        except Exception as e:  # noqa: BLE001 — surface any source read failure to the UI
            ui.notify(f"Failed to read library: {e}", type="negative")

    # ── Main content (re-rendered only on explicit actions, never during upload) ─
    @ui.refreshable
    def main() -> None:
        if state.vlc_files is None:
            ui.label(
                "Enter the VLC device IP in the sidebar and click Connect to get started."
            ).classes("text-grey-7 q-pa-md")
            return

        source_cls = _get_source_cls(state.active_source_name)
        _render_library_config(ctx, source_cls)
        _render_track_type_config(ctx, source_cls)
        _render_duplicates(state)

        if state.sync_plan is None:
            ui.label(f"Expand {source_cls.name} Library above and click Load.").classes(
                "text-grey-7 q-pa-md"
            )
            return

        with ui.tabs() as tabs:
            tab_tracks = ui.tab("Tracks")
            tab_playlists = ui.tab("Playlists")
        with ui.tab_panels(tabs, value=tab_tracks).classes("w-full"):
            with ui.tab_panel(tab_tracks):
                _render_tracks_tab(ctx, source_cls)
            with ui.tab_panel(tab_playlists):
                _render_playlists_tab(ctx, source_cls)

    ctx = PageCtx(
        state=state,
        refresh_main=main.refresh,
        reload_library=_load_library,
        fetch_device=_fetch_device,
    )
    main()


# ── Section renderers ─────────────────────────────────────────────────────────


def _track_columns(*, extra_filename: bool = False) -> list[dict]:
    cols = [
        {"name": "title", "label": "Title", "field": "title", "align": "left", "sortable": True},
        {"name": "artist", "label": "Artist", "field": "artist", "align": "left", "sortable": True},
        {"name": "album", "label": "Album", "field": "album", "align": "left", "sortable": True},
    ]
    if extra_filename:
        cols.append({"name": "filename", "label": "Filename", "field": "filename", "align": "left"})
    cols.append({"name": "size", "label": "Size", "field": "size", "align": "right"})
    return cols


def _track_row(f: LocalFile, *, with_filename: bool = False) -> dict:
    row = {
        "name": f.name,
        "title": f.title or f.name,
        "artist": f.artist,
        "album": f.album,
        "size": _fmt_size(f.size),
    }
    if with_filename:
        row["filename"] = f.name
    return row


_PAGE_SIZE = 50


def _paginated_table(
    columns: list[dict],
    rows: list[dict],
    *,
    row_key: str,
    selection: str | None = None,
    page_size: int = _PAGE_SIZE,
) -> ui.table:
    kwargs: dict[str, Any] = {
        "columns": columns,
        "rows": rows,
        "row_key": row_key,
        "pagination": {"rowsPerPage": page_size},
    }
    if selection:
        kwargs["selection"] = selection
    # table-layout:fixed keeps the table within its container width; columns
    # share the available space proportionally instead of growing to fit content.
    table = (
        ui.table(**kwargs)
        .props(
            'dense flat bordered :rows-per-page-options="[25, 50, 100, 0]"'
            ' table-style="table-layout:fixed;width:100%"'
        )
        .classes("w-full")
    )
    # Truncate text that overflows its column and reveal it on hover.
    cell_style = "display:block;width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
    for col in columns:
        if col.get("align") == "left":
            table.add_slot(
                f'body-cell-{col["name"]}',
                f'<q-td :props="props">'
                f'<span style="{cell_style}" :title="props.value">{{{{ props.value }}}}</span>'
                f"</q-td>",
            )
    return table


def _render_library_config(ctx: PageCtx, source_cls: type[LibrarySource]) -> None:
    state = ctx.state
    expanded = state.sync_plan is None
    with ui.expansion(f"{source_cls.name} Library", value=expanded).classes("w-full"):
        if len(AVAILABLE_SOURCES) > 1:

            def _on_source_change(e: Any) -> None:
                state.active_source_name = e.value
                new_cls = _get_source_cls(e.value)
                state.source_config = {}
                for fld in new_cls.config_fields():
                    detected = fld.autodetect() if fld.autodetect else None
                    state.source_config[fld.key] = (
                        str(detected) if detected is not None else str(fld.default)
                    )
                state.reset_sync()
                ctx.refresh_main()

            ui.select(
                [c.name for c in AVAILABLE_SOURCES],
                value=state.active_source_name,
                label="Library source",
                on_change=_on_source_change,
            ).classes("w-full")

        fields = source_cls.config_fields()
        inputs: dict[str, Any] = {}
        with ui.row().classes("w-full items-end"):
            for fld in fields:
                cur = state.source_config.get(fld.key, fld.default)
                if fld.field_type == "password":
                    inputs[fld.key] = ui.input(fld.label, password=True, value=cur).classes(
                        "flex-grow"
                    )
                elif fld.field_type == "integer":
                    inputs[fld.key] = ui.number(fld.label, value=int(cur or 0)).classes("flex-grow")
                else:
                    inp = ui.input(fld.label, value=cur).classes("flex-grow")
                    if fld.placeholder:
                        inp.props(f'placeholder="{fld.placeholder}"')
                    inputs[fld.key] = inp

            async def _load() -> None:
                new_config: dict[str, Any] = {}
                for fld in fields:
                    v = inputs[fld.key].value
                    new_config[fld.key] = (
                        str(int(v or 0)) if fld.field_type == "integer" else (v or "")
                    )
                try:
                    source = source_cls.from_settings(new_config)
                except (ValueError, KeyError) as e:
                    ui.notify(f"Invalid configuration: {e}", type="negative")
                    return
                if not source.is_available():
                    ui.notify(
                        "Source is not available — check the configuration above.", type="negative"
                    )
                    return
                state.source_config = new_config
                state.reset_sync()
                load_btn.props("loading")
                try:
                    await ctx.reload_library()
                finally:
                    load_btn.props(remove="loading")
                ctx.refresh_main()

            load_btn = ui.button("Load", on_click=_load).props("color=primary")


def _render_track_type_config(ctx: PageCtx, source_cls: type[LibrarySource]) -> None:
    state = ctx.state
    with ui.expansion("Track Type Configuration").classes("w-full"):
        configured = all(state.source_config.get(f.key) for f in source_cls.config_fields())
        source = source_cls.from_settings(state.source_config) if configured else None
        if source is None or not source.is_available():
            ui.label(
                f"Load a {source_cls.name} library first to discover available track types."
            ).classes("text-grey-7")
            return

        body = ui.column().classes("w-full")

        async def _discover() -> None:
            body.clear()
            try:
                infos = await run.io_bound(source.discover_categories)
            except Exception as e:  # noqa: BLE001
                with body:
                    ui.label(f"Could not read track types: {e}").classes("text-negative")
                return
            if not infos:
                with body:
                    ui.label("This source has no configurable track types.").classes("text-grey-7")
                return

            saved = {c.category_id: c for c in load_category_configs(state.active_source_name)}
            label_inputs: dict[str, Any] = {}
            include_inputs: dict[str, Any] = {}
            with body:
                ui.label(
                    "Choose which track types to include in the sync. "
                    "Save, then click Load to apply."
                ).classes("text-xs text-grey-7")
                for info in infos:
                    s = saved.get(info.id)
                    default_include = s.include if s else info.id in ("0", "3")
                    default_label = s.label if s else info.label
                    example = "; ".join(info.samples[:2])
                    if info.extensions:
                        example += f"  ({', '.join(info.extensions)})"
                    with ui.row().classes("w-full items-center no-wrap"):
                        ui.label(info.id).classes("w-8")
                        label_inputs[info.id] = (
                            ui.input(value=default_label).props("dense").classes("w-48")
                        )
                        ui.label(f"{info.count:,}").classes("w-16 text-right")
                        ui.label(example).classes("flex-grow text-xs text-grey-7")
                        include_inputs[info.id] = ui.checkbox("Include", value=default_include)

                def _save() -> None:
                    new_configs = [
                        CategoryConfig(
                            source_name=state.active_source_name,
                            category_id=info.id,
                            label=label_inputs[info.id].value,
                            include=include_inputs[info.id].value,
                        )
                        for info in infos
                    ]
                    save_category_configs(state.active_source_name, new_configs)
                    state.reset_sync()
                    ui.notify("Configuration saved. Click Load to apply.", type="positive")
                    ctx.refresh_main()

                ui.button("Save Configuration", on_click=_save).props("color=primary")

        ui.button("Discover / refresh track types", on_click=_discover).props("flat dense")


def _render_duplicates(state: AppState) -> None:
    report = find_potential_duplicates(state.vlc_files or [])
    total = report.total
    label = (
        f"Device Duplicates — {total} group(s) found"
        if total
        else "Device Duplicates — none found"
    )
    with ui.expansion(label, value=bool(total)).classes("w-full"):
        if not total:
            ui.label("No potential duplicates detected on the device.").classes("text-grey-7")
            return
        ui.label(
            "High = same title + matching duration.  Medium = same title, different durations.  "
            "Filename = VLC -N suffix match."
        ).classes("text-xs text-grey-7")
        cols = [
            {"name": "confidence", "label": "Confidence", "field": "confidence", "align": "left"},
            {"name": "key", "label": "Key", "field": "key", "align": "left"},
            {"name": "files", "label": "Files on Device", "field": "files", "align": "left"},
            {"name": "count", "label": "Count", "field": "count", "align": "right"},
            {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
        ]
        rows = [
            {
                "id": i,
                "confidence": g.confidence,
                "key": g.key,
                "files": ", ".join(f.filename for f in g.files),
                "count": len(g.files),
                "reason": g.reason,
            }
            for i, g in enumerate((*report.high, *report.medium, *report.filename))
        ]
        ui.table(columns=cols, rows=rows, row_key="id").props("dense flat bordered").classes(
            "w-full"
        )


def _render_tracks_tab(ctx: PageCtx, source_cls: type[LibrarySource]) -> None:
    state = ctx.state
    plan = state.sync_plan
    assert plan is not None
    left_table: ui.table | None = None
    likely_table: ui.table | None = None

    ui.separator()
    with ui.row().classes("w-full no-wrap"):
        with ui.column().classes("flex-grow"):
            ui.label(f"In Library — {len(plan.to_upload)}").classes("text-lg font-medium")
            if not plan.to_upload:
                ui.label(f"All {source_cls.name} tracks are already on the device.").classes(
                    "text-positive"
                )
            else:
                left_search = ui.input(placeholder="Search title, artist, album, filename…").props(
                    "dense clearable"
                ).classes("w-full")
                left_table = _paginated_table(
                    _track_columns(),
                    [_track_row(f) for f in plan.to_upload],
                    row_key="name",
                    selection="multiple",
                )
                left_search.bind_value(left_table, "filter")

        with ui.column().classes("flex-grow"):
            ui.label(f"Already on device — {len(plan.already_on_device)}").classes(
                "text-lg font-medium"
            )
            if plan.already_on_device:
                right_search = ui.input(placeholder="Search…").props("dense clearable").classes(
                    "w-full"
                )
                right_table = _paginated_table(
                    _track_columns(),
                    [_track_row(f) for f in plan.already_on_device],
                    row_key="name",
                )
                right_search.bind_value(right_table, "filter")
            else:
                ui.label(f"No {source_cls.name} tracks are on the device yet.").classes(
                    "text-grey-7"
                )

    if plan.likely_present:
        with ui.expansion(
            f"Likely already on device — {len(plan.likely_present)} "
            "(metadata title + duration match a device file)"
        ).classes("w-full"):
            ui.label(
                "These have a different filename than anything on the device, but their title and "
                "duration match a device file. Not uploaded by default — select rows to override."
            ).classes("text-xs text-grey-7")
            likely_table = _paginated_table(
                _track_columns(extra_filename=True),
                [_track_row(f, with_filename=True) for f in plan.likely_present],
                row_key="name",
                selection="multiple",
            )

    ui.separator()
    name_to_file = {f.name: f for f in plan.to_upload}
    likely_by_name = {f.name: f for f in plan.likely_present}

    upload_btn = ui.button("Upload").props("color=primary")
    progress_box = ui.column().classes("w-full")
    completion_box = ui.column().classes("w-full")

    def _selected_files() -> list[LocalFile]:
        files: list[LocalFile] = []
        if left_table is not None:
            files += [
                name_to_file[r["name"]]
                for r in left_table.selected
                if r["name"] in name_to_file
            ]
        if likely_table is not None:
            files += [
                likely_by_name[r["name"]]
                for r in likely_table.selected
                if r["name"] in likely_by_name
            ]
        return files

    def _on_done(job: UploadJob) -> None:
        done = sum(e.status == UploadStatus.DONE for e in job.file_status.values())
        errors = sum(e.status == UploadStatus.ERROR for e in job.file_status.values())
        completion_box.clear()
        with completion_box:
            if errors == 0:
                ui.label(f"All {done} file(s) uploaded successfully!").classes("text-positive")
            else:
                ui.label(f"{done} uploaded, {errors} failed.").classes("text-warning")

            async def _clear_refresh() -> None:
                state.upload_job = None
                if await ctx.fetch_device():
                    await ctx.reload_library()
                ctx.refresh_main()

            ui.button("Clear & refresh device file list", on_click=_clear_refresh).props("flat")
        upload_btn.enable()

    view = ProgressView(progress_box, "Upload progress", _on_done)

    def _upload() -> None:
        files = _selected_files()
        if not files:
            ui.notify("Select at least one file to upload.", type="warning")
            return
        completion_box.clear()
        state.upload_job = start_upload_job(
            files,
            state.vlc_conn,
            transcode=state.transcode_opts(),
            concurrency=state.upload_concurrency,
        )
        upload_btn.disable()
        view.start(state.upload_job)

    upload_btn.on_click(_upload)


def _render_playlists_tab(ctx: PageCtx, source_cls: type[LibrarySource]) -> None:
    state = ctx.state
    if state.playlists is None:
        ui.label(
            f"Load the {source_cls.name} library first "
            f"(expand {source_cls.name} Library and click Load)."
        ).classes("text-grey-7")
        return
    playlists = state.playlists
    if not playlists:
        ui.label(f"No playlists found in the {source_cls.name} library.").classes("text-grey-7")
        return

    vlc_index = build_vlc_index(state.vlc_files or [])
    proj = state.name_projector()
    kind_label = {
        "already_on_device": "on device",
        "likely_present": "likely on device",
        "new": "upload",
    }

    ui.label(
        f"{len(playlists)} playlist(s) found. Select which ones to sync, then click "
        "Sync Selected Playlists."
    ).classes("text-xs text-grey-7")

    checkboxes: dict[int, Any] = {}
    for pl in playlists:
        kinds = [classify_local_file(t.file, vlc_index, proj) for t in pl.tracks]
        paired = list(zip(pl.tracks, kinds, strict=True))
        new_ct = sum(k == "new" for k in kinds)
        dev_ct = sum(k == "already_on_device" for k in kinds)
        likely_ct = sum(k == "likely_present" for k in kinds)
        badge = "Auto" if pl.is_auto else "Static"
        header = (
            f"{pl.name}  ·  {badge}  ·  {len(pl.tracks)} track(s)"
            f"  ({new_ct} to upload, {dev_ct} on device"
            + (f", {likely_ct} likely present" if likely_ct else "")
            + ")"
        )
        with ui.expansion(header).classes("w-full"):
            if pl.unsupported_reason:
                ui.label(
                    f"Partial evaluation — some conditions were skipped: {pl.unsupported_reason}"
                ).classes("text-warning")
            checkboxes[pl.id] = ui.checkbox("Sync this playlist")
            if pl.tracks:
                cols = [
                    {"name": "title", "label": "Title", "field": "title", "align": "left"},
                    {"name": "artist", "label": "Artist", "field": "artist", "align": "left"},
                    {"name": "album", "label": "Album", "field": "album", "align": "left"},
                    {"name": "dur", "label": "Duration", "field": "duration", "align": "right"},
                    {"name": "status", "label": "Status", "field": "status", "align": "left"},
                ]
                rows = [
                    {
                        "id": i,
                        "title": t.file.title or t.file.name,
                        "artist": t.file.artist,
                        "album": t.file.album,
                        "duration": _fmt_duration(t.file.duration_ms),
                        "status": kind_label[k],
                    }
                    for i, (t, k) in enumerate(paired)
                ]
                _paginated_table(cols, rows, row_key="id", page_size=25)
            else:
                ui.label("No tracks in this playlist.").classes("text-grey-7")

    ui.separator()
    summary = ui.label().classes("text-xs text-grey-7")
    sync_btn = ui.button("Sync Selected Playlists").props("color=primary")
    progress_box = ui.column().classes("w-full")
    completion_box = ui.column().classes("w-full")

    def _selected_playlists() -> list[Playlist]:
        return [pl for pl in playlists if checkboxes[pl.id].value]

    def _compute_uploads(sel: list[Playlist]) -> list[LocalFile]:
        seen: set[str] = set()
        tracks: list[LocalFile] = []
        for pl in sel:
            for track in pl.tracks:
                if track.file.name in seen:
                    continue
                if classify_local_file(track.file, vlc_index, proj) == "new":
                    tracks.append(track.file)
                    seen.add(track.file.name)
        return tracks

    def _update_summary() -> None:
        sel = _selected_playlists()
        if not sel:
            summary.text = ""
            return
        tracks = _compute_uploads(sel)
        size = sum(f.size for f in tracks)
        summary.text = (
            f"{len(sel)} playlist(s) selected — {len(tracks)} new track(s) to upload "
            f"({_fmt_size(size)}) + {len(sel)} .m3u8 file(s)"
        )

    for cb in checkboxes.values():
        cb.on_value_change(_update_summary)

    def _on_done(job: UploadJob) -> None:
        done = sum(e.status == UploadStatus.DONE for e in job.file_status.values())
        errors = sum(e.status == UploadStatus.ERROR for e in job.file_status.values())
        completion_box.clear()
        with completion_box:
            if errors == 0:
                ui.label(f"All {done} file(s) synced successfully!").classes("text-positive")
            else:
                ui.label(f"{done} synced, {errors} failed.").classes("text-warning")

            async def _clear_refresh() -> None:
                state.playlist_upload_job = None
                await ctx.fetch_device()
                ctx.refresh_main()

            ui.button("Clear & refresh device file list", on_click=_clear_refresh).props("flat")
        sync_btn.enable()

    view = ProgressView(progress_box, "Sync progress", _on_done)

    def _sync() -> None:
        sel = _selected_playlists()
        if not sel:
            ui.notify("Select at least one playlist.", type="warning")
            return
        tracks = _compute_uploads(sel)
        # Point M3U8 references at the device's actual filename: an existing differently
        # named copy, or the `.opus` name a track will be transcoded to.
        overrides: dict[str, str] = {}
        for pl in sel:
            for track in pl.tracks:
                match = match_device_file(track.file, vlc_index, project_name=proj)
                if match is not None and match.filename != track.file.name:
                    overrides[track.file.name] = match.filename
                elif proj is not None:
                    projected = proj(track.file)
                    if projected != track.file.name:
                        overrides[track.file.name] = projected

        items: list[UploadItem] = list(tracks)
        for pl in sel:
            content = generate_m3u8(pl, name_override=overrides).encode("utf-8")
            items.append(
                InMemoryFile(
                    data=content,
                    name=f"{pl.name}.m3u8",
                    size=len(content),
                    title=f"{pl.name} (playlist)",
                )
            )
        completion_box.clear()
        state.playlist_upload_job = start_upload_job(
            items,
            state.vlc_conn,
            transcode=state.transcode_opts(),
            concurrency=state.upload_concurrency,
        )
        sync_btn.disable()
        view.start(state.playlist_upload_job)

    sync_btn.on_click(_sync)
    _update_summary()
