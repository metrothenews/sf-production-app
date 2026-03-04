from __future__ import annotations

from so_sync.drive_upload import DriveUploadClient


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeFiles:
    def __init__(self):
        self.last_list_kwargs = {}
        self.last_create_kwargs = {}
        self.last_delete_kwargs = {}

    def list(self, **kwargs):
        self.last_list_kwargs = kwargs
        return _FakeExec({"files": [{"id": "folder123", "name": "n"}]})

    def create(self, **kwargs):
        self.last_create_kwargs = kwargs
        return _FakeExec({"id": "newfolder456"})

    def delete(self, **kwargs):
        self.last_delete_kwargs = kwargs
        return _FakeExec({})


class _FakeService:
    def __init__(self):
        self._files = _FakeFiles()

    def files(self):
        return self._files


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
