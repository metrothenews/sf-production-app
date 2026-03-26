from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_preset(stem: str) -> dict[str, Any]:
    preset_path = (
        Path(__file__).resolve().parents[2]
        / "vse-text"
        / "scripts"
        / "presets"
        / f"{stem}.py"
    )
    spec = importlib.util.spec_from_file_location(f"preset_{stem}", preset_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import preset from {preset_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    if hasattr(module, "get_preset"):
        preset = module.get_preset()
    else:
        preset = getattr(module, "PRESET")
    if not isinstance(preset, dict):
        raise RuntimeError(f"Preset {stem} must be a dict, got {type(preset)}")
    return preset


def test_option2_derives_from_v007_with_only_size_and_y_changes() -> None:
    v007 = _load_preset("brat_words_manual_lightgrey_narrow_pixelate_v007")
    option2 = _load_preset("brat_words_manual_lightgrey_narrow_pixelate_v007_option2")

    expected_size = max(1, int(round(int(v007.get("font_size", 64)) * 0.70)))
    expected_pos_y = max(0.0, min(1.0, float(v007.get("pos_y", 0.5)) - 0.20))

    assert option2["font_size"] == expected_size
    assert option2["pos_y"] == pytest.approx(expected_pos_y, rel=1e-9, abs=1e-9)

    for key, value in v007.items():
        if key in {"font_size", "pos_y"}:
            continue
        assert option2.get(key) == value
