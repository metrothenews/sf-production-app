from __future__ import annotations

import sys
import types

from so_sync.drive_upload import DriveUploadClient


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self, *args, **kwargs):
        return self._payload


class _RaisingExec:
    def __init__(self, exc: Exception):
        self._exc = exc

    def execute(self, *args, **kwargs):
        raise self._exc


class _FakeFiles:
    def __init__(self):
        self.last_list_kwargs = {}
        self.last_create_kwargs = {}
        self.last_delete_kwargs = {}
        self.last_get_kwargs = {}
        self.get_payload = {"id": "file_meta_1", "size": "3", "name": "n", "parents": ["p1"], "trashed": False}

    def list(self, **kwargs):
        self.last_list_kwargs = kwargs
        return _FakeExec({"files": [{"id": "folder123", "name": "n"}]})

    def create(self, **kwargs):
        self.last_create_kwargs = kwargs
        return _FakeExec({"id": "newfolder456"})

    def delete(self, **kwargs):
        self.last_delete_kwargs = kwargs
        return _FakeExec({})

    def get(self, **kwargs):
        self.last_get_kwargs = kwargs
        return _FakeExec(self.get_payload)


class _FakeService:
    def __init__(self):
        self._files = _FakeFiles()

    def files(self):
        return self._files


def _install_fake_googleapiclient_http(monkeypatch):
    http_mod = types.ModuleType("googleapiclient.http")

    class _MediaFileUpload:
        def __init__(self, filename: str, mimetype: str, resumable: bool = False):
            self.filename = filename
            self.mimetype = mimetype
            self.resumable = resumable

    class _MediaIoBaseDownload:
        def __init__(self, *_args, **_kwargs):
            pass

        def next_chunk(self):
            return None, True

    http_mod.MediaFileUpload = _MediaFileUpload
    http_mod.MediaIoBaseDownload = _MediaIoBaseDownload
    pkg_mod = types.ModuleType("googleapiclient")
    pkg_mod.http = http_mod
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg_mod)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http_mod)
    return http_mod


def test_find_folder_by_name_builds_exact_match_query() -> None:
    service = _FakeService()
    client = DriveUploadClient(service=service)
    folder_id = client.find_folder_by_name("My Folder", "parent_1")
    q = service.files().last_list_kwargs["q"]
    assert folder_id == "folder123"
    assert "'parent_1' in parents" in q
    assert "name = 'My Folder'" in q
    assert "mimeType = 'application/vnd.google-apps.folder'" in q


def test_create_folder_sets_parent_and_folder_mime() -> None:
    service = _FakeService()
    client = DriveUploadClient(service=service)
    folder_id = client.create_folder("Run Folder", "parent_2")
    body = service.files().last_create_kwargs["body"]
    assert folder_id == "newfolder456"
    assert body["name"] == "Run Folder"
    assert body["parents"] == ["parent_2"]
    assert body["mimeType"] == "application/vnd.google-apps.folder"


def test_download_file_calls_get_media_with_file_id(monkeypatch, tmp_path) -> None:
    calls = {"file_id": ""}

    class _FakeReq:
        pass

    class _FakeFilesDownload:
        def get_media(self, *, fileId: str):
            calls["file_id"] = fileId
            return _FakeReq()

    class _FakeServiceDownload:
        def __init__(self):
            self._files = _FakeFilesDownload()

        def files(self):
            return self._files

    class _FakeDownloader:
        def __init__(self, fh, request):
            self._fh = fh
            self._done = False

        def next_chunk(self):
            if not self._done:
                self._fh.write(b"abc")
                self._done = True
            return None, self._done

    _install_fake_googleapiclient_http(monkeypatch)
    import googleapiclient.http

    monkeypatch.setattr(googleapiclient.http, "MediaIoBaseDownload", _FakeDownloader)
    client = DriveUploadClient(service=_FakeServiceDownload())
    out = tmp_path / "download.bin"
    dst = client.download_file("file_123", str(out))
    assert dst == str(out)
    assert calls["file_id"] == "file_123"
    assert out.read_bytes() == b"abc"


def test_delete_file_calls_delete_with_file_id() -> None:
    service = _FakeService()
    client = DriveUploadClient(service=service)
    client.delete_file("file_456")
    assert service.files().last_delete_kwargs["fileId"] == "file_456"


def test_get_file_metadata_reads_expected_fields() -> None:
    service = _FakeService()
    client = DriveUploadClient(service=service)
    meta = client.get_file_metadata("file_123")
    assert service.files().last_get_kwargs["fileId"] == "file_123"
    assert service.files().last_get_kwargs["fields"] == "id,size,name,parents,trashed"
    assert meta["id"] == "file_meta_1"
    assert meta["size"] == "3"
    assert meta["name"] == "n"
    assert meta["parents"] == ["p1"]
    assert meta["trashed"] is False


def test_confirm_upload_size_passes_on_exact_match() -> None:
    service = _FakeService()
    service.files().get_payload = {"id": "file_meta_1", "size": "42", "name": "n", "parents": [], "trashed": False}
    client = DriveUploadClient(service=service)
    client.confirm_upload_size("file_123", 42)


def test_confirm_upload_size_raises_on_mismatch() -> None:
    service = _FakeService()
    service.files().get_payload = {"id": "file_meta_1", "size": "41", "name": "n", "parents": [], "trashed": False}
    client = DriveUploadClient(service=service)
    try:
        client.confirm_upload_size("file_123", 42)
        assert False, "expected size mismatch"
    except Exception as exc:
        assert "size mismatch" in str(exc)


def test_confirm_upload_size_raises_when_size_missing() -> None:
    service = _FakeService()
    service.files().get_payload = {"id": "file_meta_1", "name": "n", "parents": [], "trashed": False}
    client = DriveUploadClient(service=service)
    try:
        client.confirm_upload_size("file_123", 42)
        assert False, "expected missing size failure"
    except Exception as exc:
        assert "missing size metadata" in str(exc)


def test_upload_file_retries_on_transient_error(monkeypatch, tmp_path) -> None:
    _install_fake_googleapiclient_http(monkeypatch)
    monkeypatch.setenv("DRIVE_API_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("DRIVE_API_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("DRIVE_API_RETRY_MAX_DELAY_SECONDS", "0")
    out = tmp_path / "x.mp4"
    out.write_bytes(b"abc")

    class _FlakyFiles(_FakeFiles):
        def __init__(self):
            super().__init__()
            self.create_calls = 0

        def create(self, **kwargs):
            self.last_create_kwargs = kwargs
            self.create_calls += 1
            if self.create_calls == 1:
                return _RaisingExec(TimeoutError("timed out"))
            return _FakeExec({"id": "ok_after_retry"})

    class _FlakyService:
        def __init__(self):
            self._files = _FlakyFiles()

        def files(self):
            return self._files

    client = DriveUploadClient(service=_FlakyService())
    file_id = client.upload_file(str(out), "parent_1", "video/mp4")
    assert file_id == "ok_after_retry"
    assert client.service.files().create_calls == 2


def test_upload_file_does_not_retry_on_non_retryable_error(monkeypatch, tmp_path) -> None:
    _install_fake_googleapiclient_http(monkeypatch)
    monkeypatch.setenv("DRIVE_API_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("DRIVE_API_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("DRIVE_API_RETRY_MAX_DELAY_SECONDS", "0")
    out = tmp_path / "x.mp4"
    out.write_bytes(b"abc")

    class _FailingFiles(_FakeFiles):
        def __init__(self):
            super().__init__()
            self.create_calls = 0

        def create(self, **kwargs):
            self.last_create_kwargs = kwargs
            self.create_calls += 1
            return _RaisingExec(ValueError("bad request payload"))

    class _FailingService:
        def __init__(self):
            self._files = _FailingFiles()

        def files(self):
            return self._files

    client = DriveUploadClient(service=_FailingService())
    try:
        client.upload_file(str(out), "parent_1", "video/mp4")
        assert False, "expected upload failure"
    except Exception as exc:
        assert "bad request payload" in str(exc)
    assert client.service.files().create_calls == 1
