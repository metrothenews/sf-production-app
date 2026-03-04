from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict


def _load_sibling_preset(stem: str) -> Dict[str, Any]:
    here = Path(__file__).resolve().parent
    path = here / f"{stem}.py"
    if not path.exists():
        raise FileNotFoundError(f"Missing sibling preset: {path}")

    spec = importlib.util.spec_from_file_location(f"_preset_{stem}", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import sibling preset: {path}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]

    if hasattr(mod, "get_preset"):
        p = mod.get_preset()
    elif hasattr(mod, "PRESET"):
        p = getattr(mod, "PRESET")
    else:
        raise RuntimeError(f"{path} must define PRESET or get_preset().")

    if not isinstance(p, dict):
        raise RuntimeError(f"{path} preset must be a dict, got {type(p)}")

    return p


def get_preset() -> Dict[str, Any]:
    # Inherit everything from v004 (keep the exact look)
    base = _load_sibling_preset("brat_words_manual_lightgrey_narrow_pixelate_v004")
    p: Dict[str, Any] = deepcopy(base)

    # Center the whole text block (manual layout box anchoring)
    p["align_x"] = "CENTER"
    p["align_y"] = "CENTER"
    p["pos_x"] = 0.5
    p["pos_y"] = 0.5

    # Make font 10% smaller
    base_size = int(p.get("font_size", 64))
    p["font_size"] = max(1, int(round(base_size * 0.9)))

    # Ensure no nudges fight centering
    ml = dict(p.get("manual_layout", {}) or {})
    ml["offset_x_norm"] = 0.0
    ml["offset_y_norm"] = 0.0
    p["manual_layout"] = ml

    return p
