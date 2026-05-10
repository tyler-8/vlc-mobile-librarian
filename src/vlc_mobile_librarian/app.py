from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

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
from vlc_mobile_librarian.models import LibraryCategory
from vlc_mobile_librarian.sources import AVAILABLE_SOURCES, LibrarySource
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


_settings = _load_settings()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="VLC Wi-Fi Sync",
    page_icon=":musical_note:",
    layout="wide",
)

# ── Source helpers ────────────────────────────────────────────────────────────


def _get_source_cls(name: str) -> type[LibrarySource]:
    """Return the source class matching name, falling back to the first registered."""
    for cls in AVAILABLE_SOURCES:
        if cls.name == name:
            return cls
    return AVAILABLE_SOURCES[0]


def _config_key(config: dict[str, Any]) -> str:
    """Stable JSON string of a config dict, used as a hashable cache key."""
    return json.dumps(config, sort_keys=True)


# ── Session state defaults ────────────────────────────────────────────────────

ss = st.session_state
ss.setdefault("vlc_conn", None)  # VLCConnection | None
ss.setdefault("vlc_session", None)  # requests.Session | None
ss.setdefault("vlc_files", None)  # list[VLCFile] | None
ss.setdefault("sync_plan", None)  # SyncPlan | None
ss.setdefault("selected_names", [])  # list[str] - filenames chosen for upload
ss.setdefault("likely_overrides", [])  # list[str] - filenames from likely_present opted-in
ss.setdefault("upload_job", None)  # UploadJob | None
ss.setdefault("filter_text", "")  # search filter for the "to upload" list
ss.setdefault("existing_filter_text", "")  # search filter for the "already on device" list
ss.setdefault("playlists", None)  # list[Playlist] | None
ss.setdefault("selected_playlist_ids", [])  # list[int]
ss.setdefault("playlist_upload_job", None)  # UploadJob | None

# Active source and its config
_default_source_cls = _get_source_cls(
    _settings.get("active_source_name", AVAILABLE_SOURCES[0].name)
)
ss.setdefault("active_source_name", _default_source_cls.name)

_saved_source_config: dict[str, Any] = _settings.get("source_config", {})
_initial_config: dict[str, Any] = {}
for _f in _default_source_cls.config_fields():
    if _f.key in _saved_source_config:
        _initial_config[_f.key] = _saved_source_config[_f.key]
    else:
        _detected = _f.autodetect() if _f.autodetect else None
        _initial_config[_f.key] = str(_detected) if _detected is not None else str(_f.default)
ss.setdefault("source_config", _initial_config)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fmt_size(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1_024:
        return f"{n / 1_024:.1f} KB"
    return f"{n} B"


def _active_category_ids() -> tuple[str, ...]:
    """Return the configured category ids for the active source, or the source default."""
    included = get_included_category_ids(ss.active_source_name)
    return tuple(included if included is not None else ["0", "3"])


@st.cache_data(show_spinner=False)
def _load_library(
    source_name: str, config_key: str, category_ids: tuple[str, ...]
) -> list[LocalFile]:
    cls = _get_source_cls(source_name)
    return cls.from_settings(json.loads(config_key)).scan_library(list(category_ids))


@st.cache_data(show_spinner=False)
def _discover_categories(source_name: str, config_key: str) -> list[LibraryCategory]:
    cls = _get_source_cls(source_name)
    return cls.from_settings(json.loads(config_key)).discover_categories()


@st.cache_data(show_spinner=False)
def _load_playlists(
    source_name: str, config_key: str, category_ids: tuple[str, ...]
) -> list[Playlist]:
    cls = _get_source_cls(source_name)
    return cls.from_settings(json.loads(config_key)).scan_playlists(list(category_ids))


def _render_upload_progress(
    job: UploadJob, title: str = "Upload progress", key: str = "upload"
) -> None:
    """Drain the event queue, render overall progress bar, and a toggleable per-file log.

    The log open/closed state is stored directly in ss (not in a widget key) so that
    programmatic st.rerun() calls never clobber what the user set.
    """
    while not job.events.empty():
        event = job.events.get_nowait()
        if event.file_name == "":
            job.is_done = True
        else:
            job.file_status[event.file_name] = event

    done_count = sum(1 for e in job.file_status.values() if e.status == UploadStatus.DONE)
    error_count = sum(1 for e in job.file_status.values() if e.status == UploadStatus.ERROR)
    total_files = len(job.files)
    finished = done_count + error_count

    st.subheader(title)
    st.progress(
        finished / total_files if total_files else 1.0,
        text=f"{finished} / {total_files} files",
    )

    log_open_key = f"{key}_log_open"
    if log_open_key not in ss:
        ss[log_open_key] = False

    if not ss[log_open_key] and st.button("Show file log", key=f"{key}_log_btn"):
        ss[log_open_key] = True

    if ss[log_open_key]:
        _COL_WIDTHS = [2, 3, 1.5, 3, 0.8]
        hdr = st.columns(_COL_WIDTHS)
        for col, label in zip(
            hdr,
            ["**Artist**", "**Song Title**", "**File Size**", "**Progress**", "**Status**"],
            strict=False,
        ):
            col.markdown(label)
        st.divider()

        for f in job.files:
            ev = job.file_status.get(f.name)
            cols = st.columns(_COL_WIDTHS)
            cols[0].text(f.artist or "-")
            cols[1].text(f.title or f.name)
            cols[2].text(_fmt_size(f.size))

            if ev is None:
                status_emoji = "⏳"
            elif ev.status == UploadStatus.UPLOADING:
                status_emoji = "⬆️"
                if ev.bytes_total > 0:
                    cols[3].progress(ev.bytes_sent / ev.bytes_total)
                else:
                    cols[3].text("uploading…")
            elif ev.status == UploadStatus.DONE:
                status_emoji = "✅"
                cols[3].progress(1.0)
            elif ev.status == UploadStatus.ERROR:
                status_emoji = "❌"
                cols[3].text(ev.error_msg)
            else:
                status_emoji = "⏳"

            cols[4].text(status_emoji)


def _reset_sync() -> None:
    ss.sync_plan = None
    ss.selected_names = []
    ss.upload_job = None
    ss.filter_text = ""
    ss.existing_filter_text = ""
    ss.playlists = None
    ss.selected_playlist_ids = []
    ss.playlist_upload_job = None


def _reset_all() -> None:
    ss.vlc_conn = None
    ss.vlc_session = None
    ss.vlc_files = None
    _reset_sync()


# ── Sidebar - connection ──────────────────────────────────────────────────────

with st.sidebar:
    st.title("VLC Wi-Fi Sync")
    _active_source_cls = _get_source_cls(ss.active_source_name)
    st.caption(f"Sync your {_active_source_cls.name} library to VLC on iPhone.")
    st.divider()

    st.subheader("VLC Device")
    host = st.text_input(
        "IP Address", value=_settings.get("vlc_host", ""), placeholder="192.168.1.xx"
    )
    port = st.number_input(
        "Port",
        min_value=1,
        max_value=65535,
        value=_settings.get("vlc_port", 80),
        step=1,
    )
    passcode = st.text_input("Passcode (optional)", type="password")

    auto_load_library = st.checkbox(
        "Load library on connect",
        value=_settings.get("auto_load_library", True),
    )

    connect_clicked = st.button("Connect", type="primary", width="stretch")

    if connect_clicked:
        if not host:
            st.error("Enter the VLC device IP address.")
        else:
            conn = VLCConnection(host=host.strip(), port=int(port), passcode=passcode or None)
            with st.spinner("Connecting…"):
                try:
                    session = authenticate(conn)
                    vlc_files = fetch_file_list(conn, session)
                    ss.vlc_conn = conn
                    ss.vlc_session = session
                    ss.vlc_files = vlc_files
                    _reset_sync()
                    _save_settings(
                        {
                            **_settings,
                            "vlc_host": host.strip(),
                            "vlc_port": int(port),
                            "auto_load_library": auto_load_library,
                            "active_source_name": ss.active_source_name,
                            "source_config": ss.source_config,
                        }
                    )
                    st.success(f"Connected - {len(vlc_files)} file(s) on device.")
                    source_cls = _get_source_cls(ss.active_source_name)
                    if (
                        auto_load_library
                        and source_cls.from_settings(ss.source_config).is_available()
                    ):
                        with st.spinner(f"Loading {source_cls.name} library…"):
                            try:
                                track_types = _active_category_ids()
                                ck = _config_key(ss.source_config)
                                local_files = _load_library(ss.active_source_name, ck, track_types)
                                ss.sync_plan = compute_sync_plan(local_files, vlc_files)
                                ss.filter_text = ""
                                ss.playlists = _load_playlists(
                                    ss.active_source_name, ck, track_types
                                )
                            except Exception as e:
                                st.error(f"Failed to read library: {e}")
                except VLCAuthError as e:
                    st.error(f"Auth failed: {e}")
                    _reset_all()
                except VLCConnectionError as e:
                    st.error(str(e))
                    _reset_all()

    if ss.vlc_files is not None:
        st.divider()
        st.caption(f"**{len(ss.vlc_files)}** file(s) on device")
        if st.button("Refresh file list", width="stretch"):
            with st.spinner("Refreshing…"):
                try:
                    ss.vlc_files = fetch_file_list(ss.vlc_conn, ss.vlc_session)
                    _reset_sync()
                    st.rerun()
                except VLCConnectionError as e:
                    st.error(str(e))

# ── Main area ─────────────────────────────────────────────────────────────────

if ss.vlc_files is None:
    st.info("Enter the VLC device IP in the sidebar and click **Connect** to get started.")
    st.stop()

# ── Phase 2: Library scan ─────────────────────────────────────────────────────

source_cls = _get_source_cls(ss.active_source_name)

with st.expander(f"{source_cls.name} Library", expanded=ss.sync_plan is None):
    # Source selector - only shown when multiple sources are registered
    if len(AVAILABLE_SOURCES) > 1:
        source_names = [cls.name for cls in AVAILABLE_SOURCES]
        chosen_name = st.selectbox(
            "Library source",
            source_names,
            index=source_names.index(ss.active_source_name)
            if ss.active_source_name in source_names
            else 0,
        )
        if chosen_name != ss.active_source_name:
            ss.active_source_name = chosen_name
            ss.source_config = {}
            _reset_sync()
            source_cls = _get_source_cls(chosen_name)
            # Populate defaults for the newly selected source
            for _f in source_cls.config_fields():
                _detected = _f.autodetect() if _f.autodetect else None
                ss.source_config[_f.key] = (
                    str(_detected) if _detected is not None else str(_f.default)
                )

    # Render config fields generically (one row: fields + Load button)
    fields = source_cls.config_fields()
    col_fields, col_btn = st.columns([4, 1], vertical_alignment="bottom")

    new_config: dict[str, Any] = {}
    with col_fields:
        for f in fields:
            current_val = ss.source_config.get(f.key, f.default)
            if f.field_type in ("path", "text"):
                new_config[f.key] = st.text_input(
                    f.label,
                    value=current_val,
                    placeholder=f.placeholder,
                    label_visibility="collapsed",
                )
            elif f.field_type == "password":
                new_config[f.key] = st.text_input(
                    f.label,
                    value=current_val,
                    type="password",
                    label_visibility="collapsed",
                )
            elif f.field_type == "integer":
                new_config[f.key] = str(st.number_input(f.label, value=int(current_val or 0)))

    with col_btn:
        scan_clicked = st.button("Load", type="primary", width="stretch")

    if scan_clicked:
        try:
            source_instance = source_cls.from_settings(new_config)
        except (ValueError, KeyError) as e:
            st.error(f"Invalid configuration: {e}")
            source_instance = None

        if source_instance is not None:
            if not source_instance.is_available():
                st.error("Source is not available - check the configuration above.")
            else:
                ss.source_config = new_config
                _reset_sync()
                with st.spinner(f"Reading {source_cls.name} library…"):
                    try:
                        track_types = _active_category_ids()
                        ck = _config_key(ss.source_config)
                        local_files = _load_library(ss.active_source_name, ck, track_types)
                        ss.sync_plan = compute_sync_plan(local_files, ss.vlc_files)
                        ss.filter_text = ""
                        ss.playlists = _load_playlists(ss.active_source_name, ck, track_types)
                    except Exception as e:
                        st.error(f"Failed to read library: {e}")

# ── Track Type Configuration ──────────────────────────────────────────────────

with st.expander("Track Type Configuration"):
    source_instance_for_cats = (
        source_cls.from_settings(ss.source_config)
        if all(ss.source_config.get(f.key) for f in source_cls.config_fields())
        else None
    )

    if source_instance_for_cats is None or not source_instance_for_cats.is_available():
        st.caption(f"Load a {source_cls.name} library first to discover available track types.")
    else:
        try:
            infos = _discover_categories(ss.active_source_name, _config_key(ss.source_config))
        except Exception as e:
            st.error(f"Could not read track types: {e}")
            infos = []

        if infos:
            saved = {c.category_id: c for c in load_category_configs(ss.active_source_name)}
            st.caption(
                "Choose which track types to include in the sync. "
                "Save, then click **Load** to apply."
            )

            with st.form("track_type_config_form", border=False):
                # Header row
                h1, h2, h3, h4, h5 = st.columns([1, 3, 1, 5, 1])
                h1.markdown("**Type**")
                h2.markdown("**Label**")
                h3.markdown("**Tracks**")
                h4.markdown("**Examples**")
                h5.markdown("**Include**")

                label_vals: dict[str, str] = {}
                include_vals: dict[str, bool] = {}

                for info in infos:
                    s = saved.get(info.id)
                    default_include = s.include if s else info.id in ("0", "3")
                    default_label = s.label if s else info.label

                    c1, c2, c3, c4, c5 = st.columns([1, 3, 1, 5, 1])
                    with c1:
                        st.write(info.id)
                    with c2:
                        label_vals[info.id] = st.text_input(
                            "Label",
                            value=default_label,
                            key=f"tt_label_{info.id}",
                            label_visibility="collapsed",
                        )
                    with c3:
                        st.write(f"{info.count:,}")
                    with c4:
                        example = "; ".join(info.samples[:2])
                        if info.extensions:
                            example += f"  ({', '.join(info.extensions)})"
                        st.caption(example)
                    with c5:
                        include_vals[info.id] = st.checkbox(
                            "Include",
                            value=default_include,
                            key=f"tt_include_{info.id}",
                            label_visibility="collapsed",
                        )

                if st.form_submit_button("Save Configuration", type="primary"):
                    new_configs = [
                        CategoryConfig(
                            source_name=ss.active_source_name,
                            category_id=info.id,
                            label=label_vals[info.id],
                            include=include_vals[info.id],
                        )
                        for info in infos
                    ]
                    save_category_configs(ss.active_source_name, new_configs)
                    _load_library.clear()
                    _reset_sync()
                    st.success("Configuration saved. Click **Load** to apply.")

# ── Device Duplicates ─────────────────────────────────────────────────────────

_dup_report = find_potential_duplicates(ss.vlc_files)
_dup_total = _dup_report.total
_dup_label = (
    f"Device Duplicates - {_dup_total} group(s) found"
    if _dup_total
    else "Device Duplicates - none found"
)
with st.expander(_dup_label, expanded=bool(_dup_total)):
    if not _dup_total:
        st.caption("No potential duplicates detected on the device.")
    else:
        st.caption(
            "Detected by comparing metadata title (with duration as tiebreaker) "
            "and by VLC's -N filename suffix. **High** = same title + matching "
            "duration. **Medium** = same title, different durations (may be "
            "distinct songs that share a name). **Filename** = VLC -N suffix match."
        )
        _dup_rows = [
            {
                "Confidence": g.confidence,
                "Key": g.key,
                "Files on Device": ", ".join(f.filename for f in g.files),
                "Count": len(g.files),
                "Reason": g.reason,
            }
            for g in (*_dup_report.high, *_dup_report.medium, *_dup_report.filename)
        ]
        st.dataframe(pd.DataFrame(_dup_rows), hide_index=True, width="stretch")

if ss.sync_plan is None:
    st.info(f"Expand **{source_cls.name} Library** above and click **Load**.")
    st.stop()

plan: SyncPlan = ss.sync_plan

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_tracks, tab_playlists = st.tabs(["Tracks", "Playlists"])

# ── Tab: Tracks ───────────────────────────────────────────────────────────────

with tab_tracks:
    # ── Phase 3: File selection ───────────────────────────────────────────────

    st.divider()
    col_new, col_existing = st.columns(2)

    # -- New files (left column) --

    with col_new:
        st.subheader(f"In Library - {len(plan.to_upload)}")

        filter_text = st.text_input(
            "Filter",
            value=ss.filter_text,
            placeholder="Search title, artist, album, or filename…",
            key="filter_input",
        )
        ss.filter_text = filter_text

        def _matches(f: LocalFile, q: str) -> bool:
            q = q.lower()
            return (
                q in f.name.lower()
                or q in f.title.lower()
                or q in f.artist.lower()
                or q in f.album.lower()
            )

        filtered: list[LocalFile] = (
            [f for f in plan.to_upload if _matches(f, filter_text)]
            if filter_text
            else plan.to_upload
        )

        if not plan.to_upload:
            st.success(f"All {source_cls.name} tracks are already on the device.")
        elif not filtered:
            st.info("No tracks match the filter.")
        else:
            select_all = st.checkbox(
                f"Select all ({len(filtered)} shown)",
                value=False,
                key="select_all",
            )

            if select_all:
                ss.selected_names = [f.name for f in filtered]
            else:
                ss.selected_names = st.multiselect(
                    "Select files to upload",
                    options=[f.name for f in filtered],
                    default=[n for n in ss.selected_names if n in {f.name for f in filtered}],
                    label_visibility="collapsed",
                )

            selected_set = set(ss.selected_names)
            display_files = (
                [f for f in filtered if f.name in selected_set] if ss.selected_names else filtered
            )
            df_new = pd.DataFrame(
                {
                    "Title": [f.title or f.name for f in display_files],
                    "Artist": [f.artist for f in display_files],
                    "Album": [f.album for f in display_files],
                    "Size": [_fmt_size(f.size) for f in display_files],
                }
            )
            st.dataframe(df_new, width="stretch", hide_index=True)

    # -- Already on device (right column) --

    with col_existing:
        st.subheader(f"Already on device - {len(plan.already_on_device)}")

        if plan.already_on_device:
            existing_filter = st.text_input(
                "Filter",
                value=ss.existing_filter_text,
                placeholder="Search title, artist, album, or filename…",
                key="existing_filter_input",
            )
            ss.existing_filter_text = existing_filter

            filtered_existing: list[LocalFile] = (
                [f for f in plan.already_on_device if _matches(f, existing_filter)]
                if existing_filter
                else plan.already_on_device
            )

            if not filtered_existing:
                st.info("No tracks match the filter.")
            else:
                if existing_filter and len(filtered_existing) < len(plan.already_on_device):
                    st.caption(f"{len(filtered_existing)} of {len(plan.already_on_device)} shown")
                df_existing = pd.DataFrame(
                    {
                        "Title": [f.title or f.name for f in filtered_existing],
                        "Artist": [f.artist for f in filtered_existing],
                        "Album": [f.album for f in filtered_existing],
                        "Size": [_fmt_size(f.size) for f in filtered_existing],
                    }
                )
                st.dataframe(df_existing, width="stretch", hide_index=True)
        else:
            st.info(f"No {source_cls.name} tracks are on the device yet.")

    # -- Likely already on device (full-width below the two columns) --

    if plan.likely_present:
        st.divider()
        likely_label = (
            f"Likely already on device - {len(plan.likely_present)}  "
            "(metadata title + duration match a device file)"
        )
        with st.expander(likely_label, expanded=False):
            st.caption(
                "These local files have a different filename than anything on the "
                "device, but their metadata title and duration match a device file. "
                "By default they are **not** uploaded. Check items below to override "
                "and upload anyway."
            )
            ss.likely_overrides = st.multiselect(
                "Upload these despite the title/duration match",
                options=[f.name for f in plan.likely_present],
                default=[
                    n for n in ss.likely_overrides if n in {f.name for f in plan.likely_present}
                ],
                label_visibility="collapsed",
            )
            df_likely = pd.DataFrame(
                {
                    "Title": [f.title or f.name for f in plan.likely_present],
                    "Artist": [f.artist for f in plan.likely_present],
                    "Album": [f.album for f in plan.likely_present],
                    "Filename": [f.name for f in plan.likely_present],
                    "Size": [_fmt_size(f.size) for f in plan.likely_present],
                }
            )
            st.dataframe(df_likely, width="stretch", hide_index=True)

    # ── Phase 4: Upload ───────────────────────────────────────────────────────

    st.divider()

    # Resolve selected names back to LocalFile objects
    name_to_file: dict[str, LocalFile] = {f.name: f for f in plan.to_upload}
    selected_files: list[LocalFile] = [
        name_to_file[n] for n in ss.selected_names if n in name_to_file
    ]
    # Append any opted-in likely_present overrides
    likely_by_name: dict[str, LocalFile] = {f.name: f for f in plan.likely_present}
    override_files: list[LocalFile] = [
        likely_by_name[n] for n in ss.likely_overrides if n in likely_by_name
    ]
    upload_files: list[LocalFile] = selected_files + override_files

    total_size = sum(f.size for f in upload_files)
    if override_files:
        upload_label = (
            f"Upload {len(upload_files)} file(s) "
            f"({len(selected_files)} new + {len(override_files)} overridden)  "
            f"({_fmt_size(total_size)})"
        )
    else:
        upload_label = (
            f"Upload {len(upload_files)} file(s)  ({_fmt_size(total_size)})"
            if upload_files
            else "Upload"
        )
    upload_clicked = st.button(
        upload_label,
        type="primary",
        disabled=not upload_files or ss.upload_job is not None,
    )

    if upload_clicked and upload_files:
        ss.upload_job = start_upload_job(upload_files, ss.vlc_conn)

    # -- Progress rendering --

    job: UploadJob | None = ss.upload_job
    if job is not None:
        _render_upload_progress(job, key="tracks_upload")

        done_count = sum(1 for e in job.file_status.values() if e.status == UploadStatus.DONE)
        error_count = sum(1 for e in job.file_status.values() if e.status == UploadStatus.ERROR)

        if not job.is_done:
            time.sleep(0.15)
            st.rerun()
        else:
            if error_count == 0:
                st.success(f"All {done_count} file(s) uploaded successfully!")
            else:
                st.warning(f"{done_count} uploaded, {error_count} failed.")

            if st.button("Clear & refresh device file list"):
                ss.upload_job = None
                ss.selected_names = []
                ss.likely_overrides = []
                with st.spinner("Refreshing…"):
                    try:
                        ss.vlc_files = fetch_file_list(ss.vlc_conn, ss.vlc_session)
                        track_types = _active_category_ids()
                        ck = _config_key(ss.source_config)
                        local_files = _load_library(ss.active_source_name, ck, track_types)
                        ss.sync_plan = compute_sync_plan(local_files, ss.vlc_files)
                    except VLCConnectionError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error(f"Failed to reload library: {e}")
                st.rerun()

# ── Tab: Playlists ────────────────────────────────────────────────────────────

with tab_playlists:
    if ss.playlists is None:
        st.info(
            f"Load the {source_cls.name} library first "
            f"(expand **{source_cls.name} Library** and click **Load**)."
        )
    else:
        playlists: list[Playlist] = ss.playlists
        vlc_index = build_vlc_index(ss.vlc_files or [])

        if not playlists:
            st.info(f"No playlists found in the {source_cls.name} library.")
        else:
            st.caption(
                f"{len(playlists)} playlist(s) found. "
                "Select which ones to sync, then click **Sync Selected Playlists**."
            )

            selected_ids: set[int] = set(ss.selected_playlist_ids)

            for pl in playlists:
                track_kinds = [classify_local_file(t.file, vlc_index) for t in pl.tracks]
                paired = list(zip(pl.tracks, track_kinds, strict=True))
                tracks_new = [t for t, k in paired if k == "new"]
                tracks_existing = [t for t, k in paired if k == "already_on_device"]
                tracks_likely = [t for t, k in paired if k == "likely_present"]
                badge = "Auto" if pl.is_auto else "Static"
                header = (
                    f"{pl.name}  ·  {badge}  ·  {len(pl.tracks)} track(s)"
                    f"  ({len(tracks_new)} to upload, {len(tracks_existing)} on device"
                    + (f", {len(tracks_likely)} likely present" if tracks_likely else "")
                    + ")"
                )
                with st.expander(header):
                    if pl.unsupported_reason:
                        st.warning(
                            f"Partial evaluation - some conditions were skipped: "
                            f"{pl.unsupported_reason}"
                        )

                    checked = st.checkbox(
                        "Sync this playlist",
                        value=pl.id in selected_ids,
                        key=f"pl_check_{pl.id}",
                    )
                    if checked:
                        selected_ids.add(pl.id)
                    else:
                        selected_ids.discard(pl.id)

                    if pl.tracks:

                        def _fmt_duration(ms: int) -> str:
                            s = ms // 1000
                            return f"{s // 60}:{s % 60:02d}"

                        _kind_label = {
                            "already_on_device": "on device",
                            "likely_present": "likely on device",
                            "new": "upload",
                        }
                        df_pl = pd.DataFrame(
                            {
                                "Title": [t.file.title or t.file.name for t in pl.tracks],
                                "Artist": [t.file.artist for t in pl.tracks],
                                "Album": [t.file.album for t in pl.tracks],
                                "Duration": [_fmt_duration(t.file.duration_ms) for t in pl.tracks],
                                "Status": [_kind_label[k] for k in track_kinds],
                            }
                        )
                        st.dataframe(df_pl, width="stretch", hide_index=True)
                    else:
                        st.caption("No tracks in this playlist.")

            ss.selected_playlist_ids = list(selected_ids)

            # Summary + sync button
            st.divider()
            selected_playlists = [pl for pl in playlists if pl.id in selected_ids]

            if selected_playlists:
                # Collect unique tracks needing upload (deduplicate by filename across playlists).
                # Skip both exact filename matches AND likely-already-present (title+duration)
                # matches - playlist sync defaults to conservative behavior. Likely-present
                # overrides for individual files are handled in the Tracks tab.
                seen: set[str] = set()
                tracks_to_upload: list[LocalFile] = []
                for pl in selected_playlists:
                    for track in pl.tracks:
                        if track.file.name in seen:
                            continue
                        kind = classify_local_file(track.file, vlc_index)
                        if kind == "new":
                            tracks_to_upload.append(track.file)
                            seen.add(track.file.name)

                total_upload_size = sum(f.size for f in tracks_to_upload)
                st.caption(
                    f"{len(selected_playlists)} playlist(s) selected - "
                    f"{len(tracks_to_upload)} new track(s) to upload "
                    f"({_fmt_size(total_upload_size)}) + "
                    f"{len(selected_playlists)} .m3u8 file(s)"
                )

            sync_clicked = st.button(
                "Sync Selected Playlists",
                type="primary",
                disabled=not selected_playlists or ss.playlist_upload_job is not None,
            )

            if sync_clicked and selected_playlists:
                # Build name overrides so the M3U8 references the device's actual
                # filename for any track that's likely_present (different filename
                # but same title+duration). Otherwise the playlist would reference
                # a file that doesn't exist on the device.
                m3u8_overrides: dict[str, str] = {}
                for pl in selected_playlists:
                    for track in pl.tracks:
                        match = match_device_file(track.file, vlc_index)
                        if match is not None and match.filename != track.file.name:
                            m3u8_overrides[track.file.name] = match.filename

                # Build the upload list: new audio tracks first, then .m3u8 files
                upload_items: list[UploadItem] = list(tracks_to_upload)
                for pl in selected_playlists:
                    content = generate_m3u8(pl, name_override=m3u8_overrides).encode("utf-8")
                    upload_items.append(
                        InMemoryFile(
                            data=content,
                            name=f"{pl.name}.m3u8",
                            size=len(content),
                            title=f"{pl.name} (playlist)",
                        )
                    )
                ss.playlist_upload_job = start_upload_job(upload_items, ss.vlc_conn)

            # Playlist upload progress
            pjob: UploadJob | None = ss.playlist_upload_job
            if pjob is not None:
                _render_upload_progress(pjob, title="Sync progress", key="playlist_upload")

                p_done = sum(1 for e in pjob.file_status.values() if e.status == UploadStatus.DONE)
                p_error = sum(
                    1 for e in pjob.file_status.values() if e.status == UploadStatus.ERROR
                )

                if not pjob.is_done:
                    time.sleep(0.15)
                    st.rerun()
                else:
                    if p_error == 0:
                        st.success(f"All {p_done} file(s) synced successfully!")
                    else:
                        st.warning(f"{p_done} synced, {p_error} failed.")

                    if st.button("Clear & refresh device file list", key="pl_refresh"):
                        ss.playlist_upload_job = None
                        ss.selected_playlist_ids = []
                        with st.spinner("Refreshing…"):
                            try:
                                ss.vlc_files = fetch_file_list(ss.vlc_conn, ss.vlc_session)
                            except VLCConnectionError as e:
                                st.error(str(e))
                        st.rerun()
