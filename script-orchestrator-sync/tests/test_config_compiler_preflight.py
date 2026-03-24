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


def test_preflight_accepts_google_application_credentials(monkeypatch, tmp_path) -> None:
    key_path = tmp_path / "drive-sa.json"
    key_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_path))
    assert compiler.ensure_drive_upload_credentials() == str(key_path)


def test_main_auth_preflight_failure_does_not_call_upsert(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_SHEET_ID", "sheet_1")
    monkeypatch.setenv("SHEETS_JSON_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    called = {"upsert": 0}

    def _fake_upsert(*args, **kwargs):
        called["upsert"] += 1

    monkeypatch.setattr(compiler, "upsert_render_job", _fake_upsert)
    monkeypatch.setattr(compiler, "ensure_drive_upload_credentials", lambda: (_ for _ in ()).throw(RuntimeError("auth preflight failed")))
    with pytest.raises(Exception, match=r"auth preflight failed"):
        compiler.main()
    assert called["upsert"] == 0


def test_build_render_job_upsert_payload_sets_queued_and_clears_lease() -> None:
    existing = {
        "priority": "7",
        "output_filename": "x.mp4",
        "attempts": "1",
        "max_attempts": "3",
        "created_utc": "2026-03-03T00:00:00+00:00",
        "lease_owner": "h",
        "lease_until_utc": "x",
        "run_id": "r",
        "error_msg": "err",
    }
    preset = {
        "stitch_preset": "4a4b4c",
        "text_preset": "preset_x",
        "seed": 42,
        "fps": 30,
        "aspect": "9:16",
        "bg_mode": "none",
    }
    row = compiler.build_render_job_upsert_payload(
        existing,
        render_job_id="cfg_1",
        naming_convention="camp",
        clips_library_folder_id="clips_folder",
        manifest_drive_file_id="manifest_id",
        audio_id="audio_id",
        lyrics_id="lyrics_id",
        copy_id="",
        preset=preset,
        bpm="136",
        output_parent_folder_id="out_parent",
        now_iso="2026-03-03T01:00:00+00:00",
    )
    assert row["status"] == "queued"
    assert row["enabled"] == "TRUE"
    assert row["lease_owner"] == ""
    assert row["lease_until_utc"] == ""
    assert row["run_id"] == ""
    assert row["error_msg"] == ""
    assert row["attempts"] == "0"
    assert row["max_attempts"] == "3"
    assert row["bpm"] == "136"
    assert row["edit_mode"] == "found_footage"
    assert row["performance_mode"] == ""
    assert row["performance_preset"] == ""


def test_build_render_job_upsert_payload_sets_performance_fields() -> None:
    preset = {
        "stitch_preset": "4a4b4c",
        "text_preset": "preset_x",
        "seed": 42,
        "fps": 30,
        "aspect": "9:16",
        "bg_mode": "none",
    }
    row = compiler.build_render_job_upsert_payload(
        {},
        render_job_id="cfg_perf",
        naming_convention="camp",
        clips_library_folder_id="clips_folder",
        manifest_drive_file_id="manifest_id",
        audio_id="audio_id",
        lyrics_id="lyrics_id",
        copy_id="",
        preset=preset,
        bpm="128",
        edit_mode="performance",
        performance_mode="preset",
        performance_preset="4a2br2cw",
        output_parent_folder_id="out_parent",
        now_iso="2026-03-03T01:00:00+00:00",
    )
    assert row["edit_mode"] == "performance"
    assert row["performance_mode"] == "preset"
    assert row["performance_preset"] == "4a2br2cw"
