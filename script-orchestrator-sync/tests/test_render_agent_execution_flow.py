from __future__ import annotations

import importlib
from pathlib import Path


run_mod = importlib.import_module("render_agent.run")


def test_download_selected_writes_manifest_and_paths(monkeypatch, tmp_path: Path) -> None:
    class _Proxy:
        def download_file(self, file_id: str, dst_path: str) -> str:
            Path(dst_path).write_bytes(b"x")
            return dst_path

    monkeypatch.setattr(run_mod, "DriveProxyClient", lambda: _Proxy())
    paths, manifest = run_mod._download_selected(["id1", "id2"], tmp_path, names_by_id={"id1": "a.mp4", "id2": "b.mp4"})
    assert len(paths) == 2
    assert Path(manifest).exists()
    assert Path(paths[0]).exists()
    assert "id1" in Path(paths[0]).name


def test_run_selected_job_happy_path_with_mocks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_mod, "expand_source_pool", lambda folder_ids, file_ids: ["f1", "f2", "f3"])
    seen = {"k": 0}
    def _fake_select(pool, k, mode, seed):
        seen["k"] = k
        return pool[:2]
    monkeypatch.setattr(run_mod, "select_k", _fake_select)
    monkeypatch.setattr(
        run_mod,
        "_download_selected",
        lambda selected, run_dir, names_by_id=None: ([str(run_dir / "inputs" / "x.mp4")], str(run_dir / "inputs_manifest.json")),
    )
    monkeypatch.setattr(run_mod, "render_outputs", lambda job, input_paths, work_dir: [str(work_dir / "outputs" / "final.mp4")])
    monkeypatch.setattr(run_mod, "ensure_folder_accessible", lambda folder_id: None)
    monkeypatch.setattr(
        run_mod,
        "_upload_confirm_and_delete_local",
        lambda local_path, folder_id: {"file_id": "out1", "name": "final.mp4", "webViewLink": "https://x"},
    )
    marked = {"success": 0}
    monkeypatch.setattr(run_mod, "mark_job_running", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_mod, "mark_job_success", lambda *args, **kwargs: marked.__setitem__("success", marked["success"] + 1))
    job = {
        "render_job_id": "job_1",
        "target_count": "2",
        "source_folder_ids": "",
        "source_file_ids": "f1,f2,f3",
        "output_folder_id": "folder_out",
    }
    rc = run_mod._run_selected_job(job, leased=True)
    assert rc == 0
    assert marked["success"] == 1
    assert seen["k"] == 2


def test_run_selected_job_defaults_target_count_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEFAULT_TARGET_COUNT", "1")
    monkeypatch.setattr(run_mod, "expand_source_pool", lambda folder_ids, file_ids: ["f1", "f2"])
    seen = {"k": 0}
    monkeypatch.setattr(run_mod, "select_k", lambda pool, k, mode, seed: seen.__setitem__("k", k) or pool[:k])
    monkeypatch.setattr(
        run_mod,
        "_download_selected",
        lambda selected, run_dir, names_by_id=None: ([str(run_dir / "inputs" / "x.mp4")], str(run_dir / "inputs_manifest.json")),
    )
    monkeypatch.setattr(run_mod, "render_outputs", lambda job, input_paths, work_dir: [str(work_dir / "outputs" / "final.mp4")])
    monkeypatch.setattr(run_mod, "ensure_folder_accessible", lambda folder_id: None)
    monkeypatch.setattr(run_mod, "_upload_confirm_and_delete_local", lambda local_path, folder_id: {"file_id": "out1", "name": "final.mp4"})
    monkeypatch.setattr(run_mod, "mark_job_running", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_mod, "mark_job_success", lambda *args, **kwargs: None)
    job = {"render_job_id": "job_2", "target_count": "", "source_file_ids": "f1,f2", "output_folder_id": "out"}
    rc = run_mod._run_selected_job(job, leased=True)
    assert rc == 0
    assert seen["k"] == 1


def test_run_selected_job_with_file_ids_only_skips_listing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_mod, "ensure_folder_readable", lambda folder_id: (_ for _ in ()).throw(AssertionError("should not list")))
    monkeypatch.setattr(run_mod, "expand_source_pool", lambda folder_ids, file_ids: list(file_ids))
    monkeypatch.setattr(run_mod, "select_k", lambda pool, k, mode, seed: pool[:k])
    monkeypatch.setattr(
        run_mod,
        "_download_selected",
        lambda selected, run_dir, names_by_id=None: ([str(run_dir / "inputs" / "x.mp4")], str(run_dir / "inputs_manifest.json")),
    )
    monkeypatch.setattr(run_mod, "render_outputs", lambda job, input_paths, work_dir: [str(work_dir / "outputs" / "final.mp4")])
    monkeypatch.setattr(run_mod, "ensure_folder_accessible", lambda folder_id: None)
    monkeypatch.setattr(run_mod, "_upload_confirm_and_delete_local", lambda local_path, folder_id: {"file_id": "out1", "name": "final.mp4"})
    job = {"render_job_id": "job_file_only", "target_count": "1", "source_file_ids": "f1,f2", "output_folder_id": "out"}
    rc = run_mod._run_selected_job(job, leased=False)
    assert rc == 0


def test_run_selected_job_folder_only_failure_includes_auth_diag(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_mod, "ensure_folder_readable", lambda folder_id: (_ for _ in ()).throw(RuntimeError("permission denied")))
    monkeypatch.setattr(run_mod, "describe_google_auth", lambda: {"mode": "adc", "error": "invalid_grant"})
    job = {"render_job_id": "job_folder_only", "target_count": "1", "source_folder_ids": "1folder12345", "output_folder_id": "out"}
    try:
        run_mod._run_selected_job(job, leased=False)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        msg = str(exc)
        assert "auth_diag=" in msg
        assert "invalid_grant" in msg
        assert "read/list access" in msg


def test_run_selected_job_mixed_sources_allow_no_listing_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALLOW_NO_LISTING", "1")
    monkeypatch.setattr(run_mod, "ensure_folder_readable", lambda folder_id: (_ for _ in ()).throw(RuntimeError("no auth")))
    seen = {"args": None}

    def _fake_expand(folder_ids, file_ids):
        seen["args"] = (list(folder_ids), list(file_ids))
        return list(file_ids)

    monkeypatch.setattr(run_mod, "expand_source_pool", _fake_expand)
    monkeypatch.setattr(run_mod, "select_k", lambda pool, k, mode, seed: pool[:k])
    monkeypatch.setattr(
        run_mod,
        "_download_selected",
        lambda selected, run_dir, names_by_id=None: ([str(run_dir / "inputs" / "x.mp4")], str(run_dir / "inputs_manifest.json")),
    )
    monkeypatch.setattr(run_mod, "render_outputs", lambda job, input_paths, work_dir: [str(work_dir / "outputs" / "final.mp4")])
    monkeypatch.setattr(run_mod, "ensure_folder_accessible", lambda folder_id: None)
    monkeypatch.setattr(run_mod, "_upload_confirm_and_delete_local", lambda local_path, folder_id: {"file_id": "out1", "name": "final.mp4"})
    job = {
        "render_job_id": "job_mixed",
        "target_count": "1",
        "source_folder_ids": "1folder12345",
        "source_file_ids": "id_file_001a,id_file_002b",
        "output_folder_id": "out",
    }
    rc = run_mod._run_selected_job(job, leased=False)
    assert rc == 0
    assert seen["args"] == ([], ["id_file_001a", "id_file_002b"])


def test_upload_confirm_and_delete_local_removes_output(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    out.write_bytes(b"abc")
    monkeypatch.setattr(run_mod, "upload_file_to_folder", lambda _p, _f: {"file_id": "drive_1", "name": "final.mp4"})

    class _DriveClient:
        def confirm_upload_size(self, file_id: str, expected_bytes: int) -> None:
            assert file_id == "drive_1"
            assert expected_bytes == 3

    monkeypatch.setattr(run_mod, "drive_upload_from_env", lambda: _DriveClient())
    meta = run_mod._upload_confirm_and_delete_local(str(out), "folder_1")
    assert meta["file_id"] == "drive_1"
    assert not out.exists()


def test_upload_confirm_and_delete_local_keeps_output_on_verify_failure(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    out.write_bytes(b"abc")
    monkeypatch.setattr(run_mod, "upload_file_to_folder", lambda _p, _f: {"file_id": "drive_1", "name": "final.mp4"})

    class _DriveClient:
        def confirm_upload_size(self, _file_id: str, _expected_bytes: int) -> None:
            raise RuntimeError("size mismatch")

    monkeypatch.setattr(run_mod, "drive_upload_from_env", lambda: _DriveClient())
    try:
        run_mod._upload_confirm_and_delete_local(str(out), "folder_1")
        assert False, "expected verify failure"
    except Exception as exc:
        assert "size mismatch" in str(exc)
    assert out.exists()


def test_cleanup_run_dir_failure_keeps_logs_only(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "work" / "run_1"
    logs = run_dir / "logs"
    inputs = run_dir / "inputs"
    outputs = run_dir / "outputs"
    logs.mkdir(parents=True)
    inputs.mkdir(parents=True)
    outputs.mkdir(parents=True)
    (logs / "render.log").write_text("x", encoding="utf-8")
    (inputs / "clip.mp4").write_bytes(b"x")
    (outputs / "final.mp4").write_bytes(b"x")
    monkeypatch.setenv("PIPELINE_LOCAL_CLEANUP_ON_FAILURE", "logs_only")
    run_mod._cleanup_run_dir(run_dir, success=False)
    assert logs.exists()
    assert (logs / "render.log").exists()
    assert not inputs.exists()
    assert not outputs.exists()
