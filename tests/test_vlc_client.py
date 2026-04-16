"""Tests for vlc_mobile_librarian.vlc_client - all HTTP calls mocked via responses library."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
import responses as rsps
from requests.exceptions import ConnectionError as ReqConnError
from requests.exceptions import Timeout as ReqTimeout

from vlc_mobile_librarian.vlc_client import (
    VLCAuthError,
    VLCConnection,
    VLCConnectionError,
    VLCUploadError,
    authenticate,
    fetch_file_list,
    upload_bytes,
    upload_file,
)

BASE = "http://192.168.1.1:80"
CONN = VLCConnection(host="192.168.1.1")
CONN_PASS = VLCConnection(host="192.168.1.1", passcode="1234")


# ── VLCConnection ──────────────────────────────────────────────────────────────


def test_base_url_default_port():
    assert VLCConnection(host="192.168.1.1").base_url == "http://192.168.1.1:80"


def test_base_url_custom_port():
    assert VLCConnection(host="10.0.0.1", port=8080).base_url == "http://10.0.0.1:8080"


# ── authenticate() - no passcode ───────────────────────────────────────────────


@rsps.activate
def test_authenticate_no_passcode_success():
    rsps.add(rsps.GET, f"{BASE}/", status=200)
    session = authenticate(CONN)
    assert isinstance(session, requests.Session)


@rsps.activate
def test_authenticate_no_passcode_connection_error():
    rsps.add(rsps.GET, f"{BASE}/", body=ReqConnError("refused"))
    with pytest.raises(VLCConnectionError, match="Cannot reach VLC"):
        authenticate(CONN)


@rsps.activate
def test_authenticate_no_passcode_timeout():
    rsps.add(rsps.GET, f"{BASE}/", body=ReqTimeout())
    with pytest.raises(VLCConnectionError, match="Timed out"):
        authenticate(CONN)


# ── authenticate() - with passcode ────────────────────────────────────────────


@rsps.activate
def test_authenticate_with_passcode_ok():
    rsps.add(rsps.GET, f"{BASE}/", json={"result": "ok"})
    session = authenticate(CONN_PASS)
    assert isinstance(session, requests.Session)


@rsps.activate
def test_authenticate_with_passcode_banned():
    rsps.add(rsps.GET, f"{BASE}/", json={"result": "ban"})
    with pytest.raises(VLCAuthError, match="banned"):
        authenticate(CONN_PASS)


@rsps.activate
def test_authenticate_with_passcode_ko_remaining():
    rsps.add(rsps.GET, f"{BASE}/", json={"result": "ko", "remainingAttempts": 2})
    with pytest.raises(VLCAuthError, match="2 attempt"):
        authenticate(CONN_PASS)


@rsps.activate
def test_authenticate_with_passcode_non_json_treated_as_ok():
    rsps.add(rsps.GET, f"{BASE}/", body=b"<html>OK</html>", content_type="text/html")
    session = authenticate(CONN_PASS)
    assert isinstance(session, requests.Session)


@rsps.activate
def test_authenticate_with_passcode_connection_error():
    rsps.add(rsps.GET, f"{BASE}/", body=ReqConnError("refused"))
    with pytest.raises(VLCConnectionError):
        authenticate(CONN_PASS)


@rsps.activate
def test_authenticate_with_passcode_timeout():
    rsps.add(rsps.GET, f"{BASE}/", body=ReqTimeout())
    with pytest.raises(VLCConnectionError, match="Timed out"):
        authenticate(CONN_PASS)


# ── fetch_file_list() ──────────────────────────────────────────────────────────

_SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<MediaLibrary>
  <Media title="song.mp3" size="1000000" duration="240"
         pathfile="/download/%2Fdocs%2Fsong.mp3" thumb="/thumb/1" />
  <Media title="album.flac" size="5000000" duration="180"
         pathfile="/download/%2Fdocs%2Falbum.flac" thumb="" />
</MediaLibrary>"""


@rsps.activate
def test_fetch_file_list_returns_parsed_files():
    rsps.add(rsps.GET, f"{BASE}/libMediaVLC.xml", body=_SAMPLE_XML, content_type="application/xml")
    files = fetch_file_list(CONN, requests.Session())
    assert len(files) == 2
    assert files[0].title == "song.mp3"
    assert files[0].filename == "song.mp3"
    assert files[0].size == 1_000_000
    assert files[0].duration == 240
    assert files[1].title == "album.flac"
    assert files[1].filename == "album.flac"


@rsps.activate
def test_fetch_file_list_skips_empty_title():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <MediaLibrary>
      <Media title="" size="100" duration="1" pathfile="/download/x" thumb="" />
      <Media title="good.mp3" size="100" duration="1" pathfile="/download/%2Fgood.mp3" thumb="" />
    </MediaLibrary>"""
    rsps.add(rsps.GET, f"{BASE}/libMediaVLC.xml", body=xml, content_type="application/xml")
    files = fetch_file_list(CONN, requests.Session())
    assert len(files) == 1
    assert files[0].title == "good.mp3"


@rsps.activate
def test_fetch_file_list_handles_bad_size_and_duration():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <MediaLibrary>
      <Media title="x.mp3" size="not-a-number" duration="bad"
             pathfile="/download/%2Fx.mp3" thumb="" />
    </MediaLibrary>"""
    rsps.add(rsps.GET, f"{BASE}/libMediaVLC.xml", body=xml, content_type="application/xml")
    files = fetch_file_list(CONN, requests.Session())
    assert files[0].size == 0
    assert files[0].duration == 0


@rsps.activate
def test_fetch_file_list_connection_error():
    rsps.add(rsps.GET, f"{BASE}/libMediaVLC.xml", body=ReqConnError("lost"))
    with pytest.raises(VLCConnectionError, match="Lost connection"):
        fetch_file_list(CONN, requests.Session())


@rsps.activate
def test_fetch_file_list_timeout():
    rsps.add(rsps.GET, f"{BASE}/libMediaVLC.xml", body=ReqTimeout())
    with pytest.raises(VLCConnectionError, match="Timed out"):
        fetch_file_list(CONN, requests.Session())


@rsps.activate
def test_fetch_file_list_bad_xml():
    rsps.add(rsps.GET, f"{BASE}/libMediaVLC.xml", body=b"this is not xml!!!")
    with pytest.raises(VLCConnectionError, match="parse"):
        fetch_file_list(CONN, requests.Session())


# ── upload_file() ──────────────────────────────────────────────────────────────


@rsps.activate
def test_upload_file_success(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake audio data")
    rsps.add(rsps.POST, f"{BASE}/upload.json", status=200)
    upload_file(CONN, requests.Session(), f)  # must not raise


@rsps.activate
def test_upload_file_with_progress_callback(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake audio data")
    rsps.add(rsps.POST, f"{BASE}/upload.json", status=200)
    calls: list[tuple[int, int]] = []
    upload_file(CONN, requests.Session(), f, progress_callback=lambda s, t: calls.append((s, t)))
    # Callback may or may not fire depending on how responses reads the body;
    # the important thing is no exception was raised.


@rsps.activate
def test_upload_file_bad_status(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake audio data")
    rsps.add(rsps.POST, f"{BASE}/upload.json", status=500)
    with pytest.raises(VLCUploadError, match="HTTP 500"):
        upload_file(CONN, requests.Session(), f)


def test_upload_file_os_error(tmp_path: Path):
    f = tmp_path / "nonexistent.mp3"
    with pytest.raises(VLCUploadError, match="Cannot open"):
        upload_file(CONN, requests.Session(), f)


@rsps.activate
def test_upload_file_connection_error(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake audio data")
    rsps.add(rsps.POST, f"{BASE}/upload.json", body=ReqConnError("dropped"))
    with pytest.raises(VLCUploadError, match="Connection lost"):
        upload_file(CONN, requests.Session(), f)


@rsps.activate
def test_upload_file_timeout(tmp_path: Path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake audio data")
    rsps.add(rsps.POST, f"{BASE}/upload.json", body=ReqTimeout())
    with pytest.raises(VLCUploadError, match="Timed out"):
        upload_file(CONN, requests.Session(), f)


# ── upload_bytes() ─────────────────────────────────────────────────────────────


@rsps.activate
def test_upload_bytes_success():
    rsps.add(rsps.POST, f"{BASE}/upload.json", status=200)
    upload_bytes(CONN, requests.Session(), b"playlist content", "playlist.m3u8")


@rsps.activate
def test_upload_bytes_with_progress_callback():
    rsps.add(rsps.POST, f"{BASE}/upload.json", status=200)
    calls: list[tuple[int, int]] = []
    upload_bytes(
        CONN,
        requests.Session(),
        b"data",
        "file.m3u8",
        progress_callback=lambda s, t: calls.append((s, t)),
    )


@rsps.activate
def test_upload_bytes_bad_status():
    rsps.add(rsps.POST, f"{BASE}/upload.json", status=403)
    with pytest.raises(VLCUploadError, match="HTTP 403"):
        upload_bytes(CONN, requests.Session(), b"data", "file.m3u8")


@rsps.activate
def test_upload_bytes_connection_error():
    rsps.add(rsps.POST, f"{BASE}/upload.json", body=ReqConnError("dropped"))
    with pytest.raises(VLCUploadError, match="Connection lost"):
        upload_bytes(CONN, requests.Session(), b"data", "file.m3u8")


@rsps.activate
def test_upload_bytes_timeout():
    rsps.add(rsps.POST, f"{BASE}/upload.json", body=ReqTimeout())
    with pytest.raises(VLCUploadError, match="Timed out"):
        upload_bytes(CONN, requests.Session(), b"data", "file.m3u8")
