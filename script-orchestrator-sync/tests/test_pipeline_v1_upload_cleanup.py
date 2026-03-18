from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_agent.py"
_SPEC = importlib.util.spec_from_file_location("render_agent", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import render_agent from {_SCRIPT_PATH}")
render_agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_agent)


def test_upload_confirm_record_and_delete_removes_local_file(monkeypatch, tmp_path: Path) -> None:
    local = tmp_path / "short.mp4"
    local.write_bytes(b"abc")
    row = render_agent._build_output_row_from_file(
        run_id="r1",
        render_job_id="j1",
        campaign_id="c1",
        artifact_type="pipeline_short_01",
        file_path=str(local),
        created_utc="2026-03-17T00:00:00+00:00",
    )
    seen: dict[str, object] = {}

    def _fake_append(_gw, _sheet_id, rows):
        seen["rows"] = rows
        return 1

    class _Client:
        def upload_file(self, local_path: str, folder_id: str, mime: str) -> str:
            assert local_path == str(local)
            assert folder_id == "folder_1"
            assert mime == "video/mp4"
            return "drive_123"

        def confirm_upload_size(self, file_id: str, expected_bytes: int) -> None:
            assert file_id == "drive_123"
            assert expected_bytes == 3

    monkeypatch.setattr(render_agent, "append_outputs_index_rows", _fake_append)
    out = render_agent._upload_confirm_record_and_delete(
        gw=object(),
        control_sheet_id="sheet_1",
        client=_Client(),
        artifact_row=row,
        drive_folder_id="folder_1",
        verify_mode="size",
    )
    assert out["drive_file_id"] == "drive_123"
    assert out["drive_folder_id"] == "folder_1"
    assert not local.exists()
    assert seen["rows"][0]["drive_file_id"] == "drive_123"


def test_upload_confirm_record_and_delete_keeps_local_when_verify_fails(monkeypatch, tmp_path: Path) -> None:
    local = tmp_path / "short.mp4"
    local.write_bytes(b"abc")
    row = render_agent._build_output_row_from_file(
        run_id="r1",
        render_job_id="j1",
        campaign_id="c1",
        artifact_type="pipeline_short_01",
        file_path=str(local),
        created_utc="2026-03-17T00:00:00+00:00",
    )

    class _Client:
        def upload_file(self, *_args, **_kwargs) -> str:
            return "drive_123"

        def confirm_upload_size(self, *_args, **_kwargs) -> None:
            raise RuntimeError("size mismatch")

    monkeypatch.setattr(render_agent, "append_outputs_index_rows", lambda *_args, **_kwargs: 1)
    try:
        render_agent._upload_confirm_record_and_delete(
            gw=object(),
            control_sheet_id="sheet_1",
            client=_Client(),
            artifact_row=row,
            drive_folder_id="folder_1",
            verify_mode="size",
        )
        assert False, "expected verify failure"
    except Exception as exc:
        assert "size mismatch" in str(exc)
    assert local.exists()


def test_upload_confirm_record_and_delete_rolls_back_drive_on_append_failure(monkeypatch, tmp_path: Path) -> None:
    local = tmp_path / "short.mp4"
    local.write_bytes(b"abc")
    row = render_agent._build_output_row_from_file(
        run_id="r1",
        render_job_id="j1",
        campaign_id="c1",
        artifact_type="pipeline_short_01",
        file_path=str(local),
        created_utc="2026-03-17T00:00:00+00:00",
    )
    calls = {"deleted": ""}

    class _Client:
        def upload_file(self, *_args, **_kwargs) -> str:
            return "drive_123"

        def confirm_upload_size(self, *_args, **_kwargs) -> None:
            return None

        def delete_file(self, drive_file_id: str) -> None:
            calls["deleted"] = drive_file_id

    monkeypatch.setattr(render_agent, "append_outputs_index_rows", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("append failed")))
    try:
        render_agent._upload_confirm_record_and_delete(
            gw=object(),
            control_sheet_id="sheet_1",
            client=_Client(),
            artifact_row=row,
            drive_folder_id="folder_1",
            verify_mode="size",
        )
        assert False, "expected append failure"
    except Exception as exc:
        assert "append failed" in str(exc)
    assert calls["deleted"] == "drive_123"
    assert local.exists()


def test_cleanup_pipeline_local_artifacts_failure_keeps_logs_only(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "so_work" / "run_1"
    logs = root / "logs"
    inputs = root / "inputs"
    outputs = root / "outputs"
    logs.mkdir(parents=True)
    inputs.mkdir(parents=True)
    outputs.mkdir(parents=True)
    (logs / "cut_01.log").write_text("x", encoding="utf-8")
    (inputs / "audio.wav").write_bytes(b"x")
    (outputs / "short.mp4").write_bytes(b"x")

    monkeypatch.setenv("PIPELINE_LOCAL_CLEANUP_ON_FAILURE", "logs_only")
    render_agent._cleanup_pipeline_local_artifacts(
        {"root": str(root), "logs": str(logs)},
        success=False,
    )
    assert logs.exists()
    assert (logs / "cut_01.log").exists()
    assert not inputs.exists()
    assert not outputs.exists()
