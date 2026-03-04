from __future__ import annotations

from pathlib import Path


def _pick_font_path() -> str:
    # Prefer REGULAR Arial Narrow (not bold).
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Narrow.ttf",
        "/Library/Fonts/Arial Narrow.ttf",
        # fallback if regular isn't present
        "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
        "/Library/Fonts/Arial Narrow Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return ""


def get_preset():
    return {
        # --- SRT normalization ---
        "srt_origin": "auto",
        "srt_offset_ms": 0,
        "strip_tags": True,
        "text_transform": "lower",

        # Manual layout owns line breaks; keep classic wrap disabled.
        "wrap_chars": 0,
        "max_lines": 0,

        "min_cue_ms": 450,
        "chunk_words": 0,

        # Manual mode allocates per-cue channel blocks.
        "stack_overlaps": True,
        "max_stack_channels": 128,

        "vse_newline": "cr",
        "end_mode": "min",

        # --- Typography ---
        "font_path": _pick_font_path(),
        "font_size": 104,

        # Block anchor
        "align_x": "LEFT",
        "align_y": "TOP",
        "pos_x": None,
        "pos_y": None,
        "safe_margin": 0.09,

        # --- Manual per-word layout math ---
        "manual_layout": {
            # Deterministic character-based backend
            "backend": "approx",
            "dpi": 72,

            # Measurement reconciliation knobs
            "size_scale": 1.00,
            "width_scale": 1.12,
            "height_scale": 1.00,
            "space_scale": 1.18,
            "word_pad_px": 1.0,

            # Whitespace + gaps
            "space_px": None,
            "tab_px": None,
            "tab_spaces": 4,
            "tracking_px": 0.0,
            "min_word_gap_px": 0.0,

            # Line spacing
            "line_spacing": 1.02,
            "baseline_text": "Ag",

            # Approx metrics (narrow-ish)
            "approx_char_width_factor": 0.48,
            "approx_space_width_factor": 0.44,

            # Box overrides
            "fixed_box_w_px": None,
            "fixed_box_h_px": None,

            # Custom line breaks from SRT
            "linebreak_mode": "srt",
            "max_line_width_px": None,
            "max_line_width_norm": None,
            "collapse_spaces": False,
            "fixed_lines": None,
        },

        "use_box": False,
        "box_padding": 0.08,
        "outline": 0,
        "shadow": 0,

        # Very light grey
        "text_color": "#F2F2F2",
        "text_alpha": 1.0,

        # Blur off (pixelate chain is the look)
        "blur_enable": False,
        "blur_px": 0.0,
        "blur_channel_offset": 200,

        # Manual per-word cumulative reveal
        "reveal": {
            "mode": "words",
            "style": "manual",
            "show": "cumulative",
            "preserve_spacing": True,
        },

        # Background off
        "bg_mode": "none",
        "bg_color": "#000000",
        "bg_alpha": 0.0,
        "bg_channel": 3,

        # Two-stage TRANSFORM chain (pixelate + squish without drift)
        # IMPORTANT: keep offsets SMALL; the script will override them in manual mode
        # to n_words / 2*n_words anyway.
        "pixelate_chain": {
            "enable": True,

            # Stage 1: downscale (creates pixel blocks)
            "down_sx": 0.20,
            "down_sy": 1.00,

            # Stage 2: upscale back (preserves blocks)
            "up_sx": 4.00,
            "up_sy": 1.00,

            # Defaults for non-manual modes; manual mode will pack above word block.
            "ch_offset_1": 1,
            "ch_offset_2": 2,

            # Hide the input and stage-1 in final composite
            "hide_source_alpha": 0.0,
            "hide_stage1_alpha": 0.0,

            # Best-effort enum; script will try both interpolation fields
            "interpolation": "NEAREST",
        },
    }


PRESET = get_preset()
