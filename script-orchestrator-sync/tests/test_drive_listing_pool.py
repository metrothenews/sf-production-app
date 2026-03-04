from __future__ import annotations

from render_agent import drive_listing


def test_expand_source_pool_dedupes_direct_and_folder_ids(monkeypatch) -> None:
    def _fake_list(folder_id: str):
        if folder_id == "folder1":
            return [{"id": "fileA"}, {"id": "fileB"}]
        if folder_id == "folder2":
            return [{"id": "fileB"}, {"id": "fileC"}]
        return []

    monkeypatch.setattr(drive_listing, "list_folder_video_files", _fake_list)
    pool = drive_listing.expand_source_pool(["folder1", "folder2"], ["file0", "fileA"])
    assert pool == ["file0", "fileA", "fileB", "fileC"]
