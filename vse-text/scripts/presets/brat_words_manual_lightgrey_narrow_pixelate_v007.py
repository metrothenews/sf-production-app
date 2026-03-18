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
    v007:
      - Keep v004 look.
      - Center the caption block (align=CENTER/CENTER, pos=0.5/0.5).
      - Make font 10% smaller.
      - Use BLF font metrics for manual layout.
      - Reset width_scale to 1.0 (approx-era workaround removed).
      - Declare the *future* anchor fix knobs:
          manual_layout.word_location_mode = "center"
          manual_layout.word_align_x/y = "CENTER"
        NOTE: Current runner will ignore these until we patch it.
    """
    base = _load_sibling_preset("brat_words_manual_lightgrey_narrow_pixelate_v004")
    p: Dict[str, Any] = deepcopy(base)

    # Block anchor (layout box placement)
    p["align_x"] = "CENTER"
    p["align_y"] = "CENTER"
    p["pos_x"] = 0.5
    p["pos_y"] = 0.5

    # Font size: -10%
    base_size = int(p.get("font_size", 64))
    p["font_size"] = max(1, int(round(base_size * 0.9)))

    # Manual layout config
    ml = dict(p.get("manual_layout", {}) or {})

    # Accurate measurement backend
    ml["backend"] = "blf"
    ml.setdefault("dpi", 72)

    # Remove approx-era reconciliation inflation
    ml["width_scale"] = 1.0

    # Optional: keep your existing aesthetic spacing knobs unless you want to retune later
    # (space_scale, word_pad_px, line_spacing, etc. remain inherited)

    # --- Anchor-fix knobs (runner patch will read these) ---
    # If the VSE text strip location_x/y anchors to the *center* of the text box,
    # then we must feed center coordinates per word (not left/top).
    ml["word_location_mode"] = "center"   # expected: "left_top" (current) or "center"
    ml["word_align_x"] = "CENTER"
    ml["word_align_y"] = "CENTER"

    reveal = dict(p.get("reveal", {}) or {})
    auto_speed = dict(reveal.get("auto_speed", {}) or {})
    auto_speed["enabled"] = True
    auto_speed["max_speedup"] = 2.4
    auto_speed["baseline_quantile"] = 0.70
    auto_speed["gamma"] = 1.2
    reveal["auto_speed"] = auto_speed
    p["reveal"] = reveal

    p["manual_layout"] = ml
    return p


PRESET = get_preset()
