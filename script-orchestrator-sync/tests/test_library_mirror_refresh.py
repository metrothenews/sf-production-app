from __future__ import annotations

import importlib.util
import json
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "library_mirror_refresh.py"
_SPEC = importlib.util.spec_from_file_location("library_mirror_refresh", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import mirror script from {_SCRIPT_PATH}")
mirror = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mirror)


def test_collection_num_from_folder_name_pattern() -> None:
    assert mirror._collection_num_from_folder_name("collection-1") == 1
    assert mirror._collection_num_from_folder_name("Collection-08") == 8
    assert mirror._collection_num_from_folder_name("old-collection-1") is None


def test_build_object_name_is_stable() -> None:
    obj = mirror._build_object_name(
        prefix="library-mirror",
        collection_num="3",
        category="m",
        drive_file_id="abc123",
        name='luxury car "shot".mp4',
    )
    assert obj == "library-mirror/clips/collection-3/M/abc123__luxury_car_shot_.mp4"


def test_ensure_unique_entries_dedupes_on_collection_and_drive_id() -> None:
    rows = [
        {"collection_num": "1", "drive_file_id": "a"},
        {"collection_num": "1", "drive_file_id": "a", "name": "dup"},
        {"collection_num": "1", "drive_file_id": "b"},
    ]
    out = mirror._ensure_unique_entries(rows)
    assert out == [
        {"collection_num": "1", "drive_file_id": "a"},
        {"collection_num": "1", "drive_file_id": "b"},
    ]


def test_main_refresh_quarantine_and_purge_cycle(monkeypatch, tmp_path, capsys) -> None:
    class _FakeDrive:
        def __init__(self) -> None:
            self.include_second = True
            self.rows = {
                "root": [{"id": "folder_1", "name": "collection-1", "mimeType": "application/vnd.google-apps.folder"}],
                "folder_1": [
                    {"id": "id_1", "name": "first.mp4", "mimeType": "video/mp4", "size": "10"},
                    {"id": "id_2", "name": "second.mp4", "mimeType": "video/mp4", "size": "12"},
                ],
            }
            self.payload = {
                "id_1": b"0123456789",
                "id_2": b"abcdefghijkl",
            }

        def list_files_in_folder(self, folder_id: str):
            if folder_id == "folder_1" and not self.include_second:
                return [self.rows["folder_1"][0]]
            return list(self.rows.get(folder_id, []))

        def download_file(self, drive_file_id: str, dst_path: str) -> str:
            Path(dst_path).write_bytes(self.payload[drive_file_id])
            return dst_path

    class _FakeGcs:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.texts: dict[str, str] = {}

        def list_objects(self, prefix: str = ""):
            out = []
            for name, data in self.objects.items():
                if not str(name).startswith(prefix):
                    continue
                out.append({"name": name, "size": len(data), "updated": "", "generation": "1"})
            for name, text in self.texts.items():
                if not str(name).startswith(prefix):
                    continue
                out.append({"name": name, "size": len(text.encode("utf-8")), "updated": "", "generation": "1"})
            out.sort(key=lambda row: str(row["name"]))
            return out

        def object_exists(self, object_name: str) -> bool:
            return object_name in self.objects or object_name in self.texts

        def download_text(self, object_name: str) -> str:
            return str(self.texts[object_name])

        def upload_file(self, local_path: str, object_name: str, *, content_type: str = "", metadata: dict[str, str] | None = None):
            _ = content_type
            _ = metadata
            self.objects[object_name] = Path(local_path).read_bytes()
            return f"gs://bucket/{object_name}"

        def upload_text(self, text: str, object_name: str, *, content_type: str = "application/json", metadata: dict[str, str] | None = None):
            _ = content_type
            _ = metadata
            self.texts[object_name] = str(text)
            return f"gs://bucket/{object_name}"

        def copy_object(self, src_object_name: str, dst_object_name: str) -> str:
            self.objects[dst_object_name] = bytes(self.objects[src_object_name])
            return f"gs://bucket/{dst_object_name}"

        def delete_object(self, object_name: str) -> None:
            self.objects.pop(object_name, None)
            self.texts.pop(object_name, None)

    fake_drive = _FakeDrive()
    fake_gcs = _FakeGcs()

    monkeypatch.setattr(mirror, "drive_upload_from_env", lambda: fake_drive)
    monkeypatch.setattr(mirror.GcsStorageClient, "from_adc", lambda bucket: fake_gcs)
    monkeypatch.setenv("CLIPS_LIBRARY_FOLDER_ID", "root")
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_BUCKET", "bucket")
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_PREFIX", "library-mirror")
    monkeypatch.setenv("CLIPS_LIBRARY_MIRROR_QUARANTINE_TTL_DAYS", "7")

    monkeypatch.setattr(mirror.sys, "argv", ["library_mirror_refresh.py"])
    rc = mirror.main()
    assert rc == 0
    out1 = capsys.readouterr().out
    assert "mirror_refresh_done" in out1
    active_prefix = "library-mirror/clips/"
    assert len([k for k in fake_gcs.objects if k.startswith(active_prefix)]) == 2

    # Idempotent second run: no changes expected.
    monkeypatch.setattr(mirror.sys, "argv", ["library_mirror_refresh.py"])
    rc = mirror.main()
    assert rc == 0
    out2 = capsys.readouterr().out
    assert "upload_needed=0" in out2
    assert "quarantine_needed=0" in out2

    # Remove one Drive clip -> it should move to quarantine.
    fake_drive.include_second = False
    monkeypatch.setattr(mirror.sys, "argv", ["library_mirror_refresh.py"])
    rc = mirror.main()
    assert rc == 0
    out3 = capsys.readouterr().out
    assert "mirror_refresh_quarantine moved=1" in out3
    active_objects = [k for k in fake_gcs.objects if k.startswith(active_prefix)]
    assert len(active_objects) == 1
    quarantine_objects = [k for k in fake_gcs.objects if k.startswith("library-mirror/quarantine/")]
    assert len(quarantine_objects) == 1

    # Force quarantine expiration, then purge on next run.
    q_manifest_obj = "library-mirror/indexes/quarantine_manifest.json"
    payload = json.loads(fake_gcs.texts[q_manifest_obj])
    assert len(payload["entries"]) == 1
    payload["entries"][0]["purge_after_utc"] = "2000-01-01T00:00:00+00:00"
    fake_gcs.texts[q_manifest_obj] = json.dumps(payload)

    monkeypatch.setattr(mirror.sys, "argv", ["library_mirror_refresh.py"])
    rc = mirror.main()
    assert rc == 0
    out4 = capsys.readouterr().out
    assert "mirror_refresh_purge purged=1" in out4
    quarantine_objects_after = [k for k in fake_gcs.objects if k.startswith("library-mirror/quarantine/")]
    assert quarantine_objects_after == []
