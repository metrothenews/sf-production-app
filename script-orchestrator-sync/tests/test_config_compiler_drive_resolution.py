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
    def __init__(self, scoped_hits: dict[str, str] | None = None, unscoped_hits: list[str] | None = None):
        self.scoped_hits = scoped_hits or {}
        self.unscoped_hits = unscoped_hits or []

    def search_file_id_by_name(self, name: str, *, parent_drive_folder_id: str = "", mime_type: str = "") -> str:
        return self.scoped_hits.get(parent_drive_folder_id, "")

    def search_file_ids_by_name(self, name: str, *, parent_drive_folder_id: str = "", mime_type: str = "") -> list[str]:
        if parent_drive_folder_id:
            hit = self.scoped_hits.get(parent_drive_folder_id, "")
            return [hit] if hit else []
        return list(self.unscoped_hits)


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
