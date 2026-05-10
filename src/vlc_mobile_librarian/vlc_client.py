from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from requests_toolbelt import MultipartEncoderMonitor


class VLCConnectionError(Exception):
    pass


class VLCAuthError(Exception):
    pass


class VLCUploadError(Exception):
    pass


@dataclass(frozen=True)
class VLCFile:
    title: str  # media metadata title (may differ from filename)
    filename: str  # bare filename as stored on device, extracted from download URL
    size: int  # bytes
    duration: int  # seconds
    thumb_url: str
    download_url: str


@dataclass
class VLCConnection:
    host: str
    port: int = 80
    passcode: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def authenticate(conn: VLCConnection) -> requests.Session:
    """Create a session and authenticate with the VLC server.

    Raises VLCConnectionError if the server cannot be reached.
    Raises VLCAuthError if the passcode is rejected.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "vlc-mobile-librarian/1.0"})

    if conn.passcode:
        try:
            resp = session.get(
                f"{conn.base_url}/",
                params={"code": conn.passcode},
                timeout=10,
            )
        except requests.exceptions.ConnectionError as e:
            raise VLCConnectionError(f"Cannot reach VLC at {conn.base_url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise VLCConnectionError(f"Timed out connecting to {conn.base_url}") from e

        # VLC returns JSON: {"result": "ok"|"ko"|"ban", ...}
        try:
            data = resp.json()
            if data.get("result") == "ban":
                raise VLCAuthError("Too many failed attempts - IP is banned by VLC.")
            if data.get("result") == "ko":
                remaining = data.get("remainingAttempts", "?")
                raise VLCAuthError(f"Incorrect passcode. {remaining} attempt(s) remaining.")
        except (ValueError, AttributeError):
            # Not JSON - VLC may not have a passcode set; treat as OK
            pass
    else:
        # No passcode - just verify connectivity
        try:
            session.get(f"{conn.base_url}/", timeout=10)
        except requests.exceptions.ConnectionError as e:
            raise VLCConnectionError(f"Cannot reach VLC at {conn.base_url}: {e}") from e
        except requests.exceptions.Timeout:
            raise VLCConnectionError(f"Timed out connecting to {conn.base_url}") from None

    return session


def _parse_duration(raw: str) -> int:
    """Parse VLC's duration attribute into integer seconds.

    Accepts plain seconds ("240"), "MM:SS" ("02:48"), and "HH:MM:SS" ("1:02:48").
    Returns 0 on malformed input.
    """
    if not raw:
        return 0
    parts = raw.split(":")
    try:
        if len(parts) == 1:
            return max(int(parts[0]), 0)
        if len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return max(m * 60 + s, 0)
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return max(h * 3600 + m * 60 + s, 0)
    except ValueError:
        return 0
    return 0


def fetch_file_list(conn: VLCConnection, session: requests.Session) -> list[VLCFile]:
    """Fetch the XML library listing from the VLC device.

    Returns a list of VLCFile objects representing files currently on the device.
    """
    try:
        resp = session.get(f"{conn.base_url}/libMediaVLC.xml", timeout=15)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise VLCConnectionError(f"Lost connection to VLC: {e}") from e
    except requests.exceptions.Timeout:
        raise VLCConnectionError("Timed out fetching file list from VLC") from None

    try:
        # Pass bytes so ElementTree can detect encoding from the XML declaration
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise VLCConnectionError(f"Could not parse VLC library XML: {e}") from e

    files: list[VLCFile] = []
    for media in root.iter("Media"):
        title = media.get("title", "")
        if not title:
            continue
        try:
            size = int(media.get("size", 0))
        except ValueError:
            size = 0
        duration = _parse_duration(media.get("duration", ""))
        download_url = media.get("pathfile", "")
        # Extract the bare filename from the download URL.
        # pathfile encodes the full iOS path as a single URL segment, e.g.:
        #   /download/%2Fvar%2Fmobile%2F...%2FDocuments%2F01%201UP.flac
        # Step 1: take the segment after /download/ → "%2Fvar%2F...%2F01 1UP.flac"
        # Step 2: URL-decode → "/var/mobile/.../Documents/01 1UP.flac"
        # Step 3: take Path().name → "01 1UP.flac"
        try:
            encoded_segment = Path(urlparse(download_url).path).name
            filename = Path(unquote(encoded_segment)).name
        except Exception:
            filename = title
        files.append(
            VLCFile(
                title=title,
                filename=filename,
                size=size,
                duration=duration,
                thumb_url=media.get("thumb", ""),
                download_url=download_url,
            )
        )

    return files


def upload_file(
    conn: VLCConnection,
    session: requests.Session,
    file_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Upload a file to the VLC device via multipart/form-data POST.

    progress_callback(bytes_sent, bytes_total) is called after each chunk.
    Raises VLCUploadError on failure.
    """

    def _monitor_callback(monitor: MultipartEncoderMonitor) -> None:
        if progress_callback is not None:
            progress_callback(monitor.bytes_read, monitor.len)

    try:
        with open(file_path, "rb") as fh:
            encoder = MultipartEncoderMonitor.from_fields(
                fields={"files[]": (file_path.name, fh, "application/octet-stream")},
                callback=_monitor_callback,
            )
            try:
                resp = session.post(
                    f"{conn.base_url}/upload.json",
                    data=encoder,
                    headers={"Content-Type": encoder.content_type},
                    timeout=300,  # large files over Wi-Fi can be slow
                )
            except requests.exceptions.ConnectionError as e:
                raise VLCUploadError(
                    f"Connection lost during upload of {file_path.name}: {e}"
                ) from e
            except requests.exceptions.Timeout:
                raise VLCUploadError(f"Timed out uploading {file_path.name}") from None

            if resp.status_code != 200:
                raise VLCUploadError(f"VLC returned HTTP {resp.status_code} for {file_path.name}")
    except OSError as e:
        raise VLCUploadError(f"Cannot open {file_path}: {e}") from e


def upload_bytes(
    conn: VLCConnection,
    session: requests.Session,
    data: bytes,
    filename: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Upload in-memory bytes to the VLC device as a file named `filename`.

    Used for synthetically generated files (e.g. .m3u8 playlists) that don't
    exist on disk.  progress_callback(bytes_sent, bytes_total) is called after
    each chunk.  Raises VLCUploadError on failure.
    """

    def _monitor_callback(monitor: MultipartEncoderMonitor) -> None:
        if progress_callback is not None:
            progress_callback(monitor.bytes_read, monitor.len)

    encoder = MultipartEncoderMonitor.from_fields(
        fields={"files[]": (filename, io.BytesIO(data), "application/octet-stream")},
        callback=_monitor_callback,
    )
    try:
        resp = session.post(
            f"{conn.base_url}/upload.json",
            data=encoder,
            headers={"Content-Type": encoder.content_type},
            timeout=30,
        )
    except requests.exceptions.ConnectionError as e:
        raise VLCUploadError(f"Connection lost during upload of {filename}: {e}") from e
    except requests.exceptions.Timeout:
        raise VLCUploadError(f"Timed out uploading {filename}") from None

    if resp.status_code != 200:
        raise VLCUploadError(f"VLC returned HTTP {resp.status_code} for {filename}")
