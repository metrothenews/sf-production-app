from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compile_config_to_render_job.py"
_SPEC = importlib.util.spec_from_file_location("compile_config_to_render_job", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import compile script from {_SCRIPT_PATH}")
compiler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compiler)


def test_short_filename_preserves_trailing_width() -> None:
    assert compiler.short_filename_from_naming("Test_Video_v1_000", 0) == "short_000.mp4"
    assert compiler.short_filename_from_naming("Test_Video_v1_000", 1) == "short_001.mp4"
    assert compiler.short_filename_from_naming("MyVideo09", 2) == "short_02.mp4"


def test_video_filename_from_naming() -> None:
    assert compiler.video_filename_from_naming("Test_Video_v2_000", 0) == "Test_Video_v2_000.mp4"
    assert compiler.video_filename_from_naming("Test_Video_v2_000", 11) == "Test_Video_v2_011.mp4"
    assert compiler.video_filename_from_naming("MyVideo", 5) == "MyVideo_005.mp4"


def test_drive_reference_kind() -> None:
    assert compiler.drive_reference_kind("YOUR_GOOGLE_SHEET_ID") == "id"
    assert compiler.drive_reference_kind("bad news (explicit_v3).wav") == "name"
    assert compiler.drive_reference_kind("") == "empty"


def test_build_manifest_clips_is_deterministic_and_labeled() -> None:
    files = [
        {"id": "id_b", "name": "b.mp4", "mimeType": "video/mp4"},
        {"id": "id_a", "name": "a.mp4", "mimeType": "video/mp4"},
        {"id": "id_txt", "name": "readme.txt", "mimeType": "text/plain"},
    ]
    clips1 = compiler.build_manifest_clips(files, seed=42, max_clips=10)
    clips2 = compiler.build_manifest_clips(files, seed=42, max_clips=10)
    assert clips1 == clips2
    assert len(clips1) == 2
    assert clips1[0]["label"] == "c001"
    assert clips1[1]["label"] == "c002"


def test_collect_files_from_folder_tree_recurses_subfolders() -> None:
    class _Drive:
        def __init__(self) -> None:
            self._rows = {
                "root": [
                    {"id": "sub1", "name": "collection-1", "mimeType": "application/vnd.google-apps.folder"},
                    {"id": "sub2", "name": "collection-2", "mimeType": "application/vnd.google-apps.folder"},
                ],
                "sub1": [
                    {"id": "v1", "name": "a.mp4", "mimeType": "video/mp4"},
                ],
                "sub2": [
                    {"id": "deep", "name": "nested", "mimeType": "application/vnd.google-apps.folder"},
                ],
                "deep": [
                    {"id": "v2", "name": "b.mov", "mimeType": "video/quicktime"},
                ],
            }

        def list_files_in_folder(self, folder_id: str):
            return list(self._rows.get(folder_id, []))

    out = compiler.collect_files_from_folder_tree(_Drive(), "root", max_depth=3)
    ids = [row["id"] for row in out]
    assert ids == ["v1", "v2"]


def test_enabled_collection_numbers_detects_checked_toggles() -> None:
    cfg = {
        "V Collection 1": "TRUE",
        "V Collection 2": "false",
        "V Collection 3": "1",
    }
    assert compiler._enabled_collection_numbers(cfg) == [1, 3]


def test_resolve_collection_folder_ids_by_name() -> None:
    class _Drive:
        def list_files_in_folder(self, folder_id: str):
            assert folder_id == "clips_root"
            return [
                {"id": "fold_1", "name": "V Collection 1", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "fold_2", "name": "Collection 2", "mimeType": "application/vnd.google-apps.folder"},
            ]

    cfg = {
        "V Collection 1": "TRUE",
        "V Collection 2": "TRUE",
    }
    out = compiler.resolve_collection_folder_ids(cfg, drive_client=_Drive(), root_folder_id="clips_root")
    assert out == [
        {"collection_num": "1", "folder_id": "fold_1"},
        {"collection_num": "2", "folder_id": "fold_2"},
    ]


def test_resolve_collection_folder_ids_prefers_explicit_id_column() -> None:
    class _Drive:
        def list_files_in_folder(self, folder_id: str):
            assert folder_id == "clips_root"
            return []

    cfg = {
        "V Collection 2": "TRUE",
        "V Collection 2 Folder ID": "1abcdefghijklmnopqrstuv",
    }
    out = compiler.resolve_collection_folder_ids(cfg, drive_client=_Drive(), root_folder_id="clips_root")
    assert out == [{"collection_num": "2", "folder_id": "1abcdefghijklmnopqrstuv"}]
