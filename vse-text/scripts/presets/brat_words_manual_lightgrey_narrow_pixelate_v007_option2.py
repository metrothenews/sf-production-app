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
    """
    Option 2, derived from v007:
      - Keep all style/layout behavior unchanged.
      - Make text 30% smaller than v007.
      - Move the lyric block 20% lower in frame.
    """
    base = _load_sibling_preset("brat_words_manual_lightgrey_narrow_pixelate_v007")
    p: Dict[str, Any] = deepcopy(base)

    base_size = int(p.get("font_size", 64))
    p["font_size"] = max(1, int(round(base_size * 0.70)))

    base_pos_y = float(p.get("pos_y", 0.5))
    p["pos_y"] = max(0.0, min(1.0, base_pos_y - 0.20))
    return p


PRESET = get_preset()
