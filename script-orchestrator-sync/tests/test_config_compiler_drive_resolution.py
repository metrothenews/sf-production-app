from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compile_config_to_render_job.py"
_SPEC = importlib.util.spec_from_file_location("compile_config_to_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import compile script from {_SCRIPT_PATH}")
compiler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compiler)


class _FakeDriveClient:
    def __init__(
        self,
        scoped_hits: dict[str, str] | None = None,
        unscoped_hits: list[str] | None = None,
        folder_rows: dict[str, list[dict[str, str]]] | None = None,
    ):
        self.scoped_hits = scoped_hits or {}
        self.unscoped_hits = unscoped_hits or []
        self.folder_rows = folder_rows or {}

    def search_file_id_by_name(self, name: str, *, parent_drive_folder_id: str = "", mime_type: str = "") -> str:
        return self.scoped_hits.get(parent_drive_folder_id, "")

    def search_file_ids_by_name(self, name: str, *, parent_drive_folder_id: str = "", mime_type: str = "") -> list[str]:
        if parent_drive_folder_id:
            hit = self.scoped_hits.get(parent_drive_folder_id, "")
            return [hit] if hit else []
        return list(self.unscoped_hits)

    def list_files_in_folder(self, parent_folder_id: str, mime_prefix: str = "") -> list[dict[str, str]]:
        _ = mime_prefix
        return list(self.folder_rows.get(parent_folder_id, []))


def test_resolve_drive_file_id_returns_id_as_is() -> None:
    out = compiler.resolve_drive_file_id(
        "YOUR_GOOGLE_SHEET_ID",
        label="Audio File",
        assets_folder_id="assets",
        clips_library_folder_id="clips",
    )
    assert out == "YOUR_GOOGLE_SHEET_ID"


def test_resolve_drive_file_id_raises_for_name_lookup() -> None:
    with pytest.raises(RuntimeError, match=r"must be a Drive file id"):
        compiler.resolve_drive_file_id(
            "bad news (explicit_v3).wav",
            label="Audio File",
            assets_folder_id="assets",
            clips_library_folder_id="clips",
        )


def test_resolve_performance_source_folder_id_defaults() -> None:
    out = compiler._resolve_performance_source_folder_id({})
    assert out == compiler.PERFORMANCE_SOURCE_FOLDER_ID_DEFAULT


def test_resolve_performance_source_folder_id_accepts_folder_url() -> None:
    cfg = {"Performance Source Folder ID": "https://drive.google.com/drive/folders/YOUR_GOOGLE_SHEET_ID"}
    out = compiler._resolve_performance_source_folder_id(cfg)
    assert out == "YOUR_GOOGLE_SHEET_ID"


def test_resolve_performance_source_folder_id_accepts_legacy_clips_folder_column() -> None:
    cfg = {"Performance Clips Folder ID": "YOUR_GOOGLE_SHEET_ID"}
    out = compiler._resolve_performance_source_folder_id(cfg)
    assert out == "YOUR_GOOGLE_SHEET_ID"


def test_resolve_performance_ref_by_exact_name(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_CLIP_NAME_RESOLUTION", "1")
    drive_client = _FakeDriveClient(
        folder_rows={
            "folder_perf": [
                {"id": "A" * 22, "name": "clip_a.mp4"},
                {"id": "B" * 22, "name": "clip_b.mp4"},
            ]
        }
    )
    out = compiler._resolve_performance_drive_file_ref(
        value="clip_a.mp4",
        label="Performance Clip A File ID",
        drive_client=drive_client,
        source_folder_id="folder_perf",
        listing_cache={},
    )
    assert out == "A" * 22


def test_resolve_performance_ref_ambiguous_name_raises(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_CLIP_NAME_RESOLUTION", "1")
    drive_client = _FakeDriveClient(
        folder_rows={
            "folder_perf": [
                {"id": "A" * 22, "name": "clip_a.mp4"},
                {"id": "B" * 22, "name": "clip_a.mp4"},
            ]
        }
    )
    with pytest.raises(RuntimeError, match=r"ambiguous"):
        compiler._resolve_performance_drive_file_ref(
            value="clip_a.mp4",
            label="Performance Clip A File ID",
            drive_client=drive_client,
            source_folder_id="folder_perf",
            listing_cache={},
        )


def test_resolve_performance_master_clip_supports_snake_case_alias() -> None:
    out = compiler._resolve_performance_master_clip_id({"performance_master_clip_drive_file_id": "A" * 22})
    assert out == "A" * 22


def test_resolve_performance_master_clip_auto_resolves_first_video_from_source_folder() -> None:
    drive_client = _FakeDriveClient(
        folder_rows={
            "folder_perf": [
                {"id": "B" * 22, "name": "perf-b.mp4", "mimeType": "video/mp4"},
                {"id": "A" * 22, "name": "perf-a.mp4", "mimeType": "video/mp4"},
            ]
        }
    )
    out = compiler._resolve_performance_master_clip_id(
        {},
        drive_client=drive_client,
        source_folder_id="folder_perf",
        listing_cache={},
    )
    assert out == "A" * 22


def test_parse_performance_clip_map_name_lookup(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_CLIP_NAME_RESOLUTION", "1")
    drive_client = _FakeDriveClient(
        folder_rows={
            "folder_perf": [
                {"id": "A" * 22, "name": "clip_a.mp4"},
                {"id": "B" * 22, "name": "clip_b.mp4"},
            ]
        }
    )
    cfg = {
        "Performance Clip A File ID": "clip_a.mp4",
        "performance_clip_b_drive_file_id": "clip_b.mp4",
    }
    out = compiler._parse_performance_clip_map(
        cfg,
        drive_client=drive_client,
        source_folder_id="folder_perf",
        listing_cache={},
    )
    assert out == {"a": "A" * 22, "b": "B" * 22}


def test_performance_name_resolution_disabled_rejects_name(monkeypatch) -> None:
    monkeypatch.setenv("PERFORMANCE_CLIP_NAME_RESOLUTION", "0")
    with pytest.raises(RuntimeError, match=r"PERFORMANCE_CLIP_NAME_RESOLUTION=0"):
        compiler._resolve_performance_drive_file_ref(
            value="clip_a.mp4",
            label="Performance Clip A File ID",
            drive_client=_FakeDriveClient(),
            source_folder_id="folder_perf",
            listing_cache={},
        )
