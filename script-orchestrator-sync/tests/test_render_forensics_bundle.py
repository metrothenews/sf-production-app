from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_forensics_bundle.py"
_SPEC = importlib.util.spec_from_file_location("render_forensics_bundle", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to import script from {_SCRIPT_PATH}")
bundle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bundle)


def test_forensics_bundle_writes_summary_and_zip(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "run_1"
    logs = run_dir / "logs"
    outputs = run_dir / "outputs"
    logs.mkdir(parents=True)
    outputs.mkdir(parents=True)
    (logs / "cut_01_stitch.log").write_text("ok\n", encoding="utf-8")
    (logs / "events.jsonl").write_text('{"event":"run_start"}\n', encoding="utf-8")
    (outputs / "cut_01_effects_plan.json").write_text('{"version":1,"frame_ops":[]}', encoding="utf-8")

    monkeypatch.setattr(bundle, "_write_probes", lambda _media, _out_dir: [])
    monkeypatch.setattr(bundle, "_sample_frames", lambda _media, _out_dir, frame_samples: [])
    monkeypatch.setattr(
        bundle,
        "_capture_env_snapshot",
        lambda out_path: out_path.write_text("{}", encoding="utf-8"),
    )

    out_dir = tmp_path / "diag"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_forensics_bundle.py",
            "--run-dir",
            str(run_dir),
            "--run-id",
            "r1",
            "--render-job-id",
            "j1",
            "--error",
            "boom",
            "--output-dir",
            str(out_dir),
            "--frame-samples",
            "3",
        ],
    )
    rc = bundle.main()
    assert rc == 0
    summary_path = out_dir / "summary.json"
    bundle_path = out_dir / "bundle.zip"
    assert summary_path.exists()
    assert bundle_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == "r1"
    assert "logs/cut_01_stitch.log" in summary["logs_copied"]
    assert "outputs/cut_01_effects_plan.json" in summary["effects_plans_copied"]

