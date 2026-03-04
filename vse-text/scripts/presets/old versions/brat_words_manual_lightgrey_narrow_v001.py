from __future__ import annotations

from pathlib import Path


def _pick_font_path() -> str:
    """
    Brat-ish look: narrow grotesk. On macOS, Arial Narrow is usually present in
    /System/Library/Fonts/Supplemental. We pick the first path that exists.
    """
    candidates = [
        # macOS common
        "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Narrow.ttf",
        "/Library/Fonts/Arial Narrow Bold.ttf",
        "/Library/Fonts/Arial Narrow.ttf",
        # fallback "condensed" vibes (may or may not exist as discrete files)
        "/System/Library/Fonts/Supplemental/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
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

        # We are using *custom line breaks* from the SRT.
        # Manual mode does NOT depend on wrap_chars/max_lines, so keep them off.
        "wrap_chars": 0,
        "max_lines": 0,

        "min_cue_ms": 450,
        "chunk_words": 0,

        # Overlap safety: manual mode allocates per-cue channel blocks.
        "stack_overlaps": True,
        "max_stack_channels": 768,

        "vse_newline": "cr",
        "end_mode": "min",

        # --- Typography ---
        "font_path": _pick_font_path(),
        "font_size": 104,

        # Layout anchor for the whole lyric block (like the reference: top/left-ish)
        "align_x": "LEFT",     # LEFT|CENTER|RIGHT
        "align_y": "TOP",      # BOTTOM|CENTER|TOP
        "pos_x": None,
        "pos_y": None,
        "safe_margin": 0.09,

        # --- Manual layout math (character-based, deterministic) ---
        "manual_layout": {
            # For "math based on number of characters", use the deterministic approx backend.
            "backend": "approx",
            "dpi": 72,

            # Reconciliation knobs (keep conservative; tune later if needed)
            "size_scale": 1.00,
            "width_scale": 1.12,
            "height_scale": 1.00,
            "space_scale": 1.18,

            # Let spacing be driven by actual spaces in the SRT line (including multi-spaces).
            "space_px": None,
            "tab_px": None,
            "tab_spaces": 4,

            # We want the "spacing feel" to come primarily from whitespace runs,
            # not forced post-word gaps.
            "tracking_px": 0.0,
            "min_word_gap_px": 0.0,

            # Small pad to avoid near-touches without bloating spacing too much.
            "word_pad_px": 1.0,

            # Tight-ish lines like the reference
            "line_spacing": 1.02,
            "baseline_text": "Ag",

            # Character/space width factors (narrow font vibe)
            "approx_char_width_factor": 0.48,
            "approx_space_width_factor": 0.44,

            "fixed_box_w_px": None,
            "fixed_box_h_px": None,

            # Preserve SRT line breaks exactly.
            "linebreak_mode": "srt",
            "max_line_width_px": None,
            "max_line_width_norm": None,
            "collapse_spaces": False,
            "fixed_lines": None,
        },

        # Strip-level styling
        "use_box": False,
        "box_padding": 0.08,
        "outline": 0,
        "shadow": 0,

        # Very light grey
        "text_color": "#F2F2F2",
        "text_alpha": 1.0,

        # “A bit pixelated”: keep it crisp (no blur). Compression/upscaling will do the rest.
        "blur_enable": False,
        "blur_px": 0.0,
        "blur_channel_offset": 200,

        # Manual per-word cumulative reveal (no jumping)
        "reveal": {
            "mode": "words",
            "style": "manual",
            "show": "cumulative",
            "preserve_spacing": True,  # keep multi-spaces if you type them in the SRT
        },

        # Background off
        "bg_mode": "none",
        "bg_color": "#000000",
        "bg_alpha": 0.0,
        "bg_channel": 3,
    }


# Optional: allow either contract style
PRESET = get_preset()
