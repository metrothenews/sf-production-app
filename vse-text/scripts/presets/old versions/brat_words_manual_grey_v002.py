# =============================================================================
# preset: brat_words_manual_grey_v002
#
# Manual per-word layout + cumulative reveal.
# This version uses the new PRESET["manual_layout"] controls so spacing/measurement
# can be tuned from the preset (no main-script edits).
#
# If you still see overlaps:
#   - increase manual_layout.width_scale (e.g., 1.20 -> 1.35)
#   - increase manual_layout.space_scale (e.g., 1.15 -> 1.30)
#   - increase manual_layout.word_pad_px (e.g., 2.0 -> 6.0)   # blur/outline compensation
#   - increase manual_layout.min_word_gap_px (e.g., 2.0 -> 6.0)
# =============================================================================

PRESET = {
  # --- SRT normalization ---
  "srt_origin": "auto",
  "srt_offset_ms": 0,
  "strip_tags": True,
  "text_transform": "lower",

  # Preserve exact manual typesetting from SRT
  "wrap_chars": 0,
  "max_lines": 0,

  "min_cue_ms": 450,
  "chunk_words": 0,

  # Overlap safety (manual mode allocates a per-word channel block per cue)
  "stack_overlaps": True,
  "max_stack_channels": 768,

  "vse_newline": "cr",
  "end_mode": "min",

  # --- Typography ---
  "font_path": "",
  "font_size": 92,

  # Layout anchor for the whole lyric block
  "align_x": "CENTER",   # LEFT|CENTER|RIGHT (anchors the entire layout box)
  "align_y": "CENTER",   # BOTTOM|CENTER|TOP
  "pos_x": None,
  "pos_y": None,
  "safe_margin": 0.08,

  # --- Manual layout math (PRESET-owned) ---
  "manual_layout": {
    # Measurement backend:
    #   "blf"   = use Blender's blf.dimensions (preferred)
    #   "approx"= deterministic fallback (useful if blf feels inconsistent)
    "backend": "blf",
    "dpi": 72,

    # Measurement reconciliation knobs
    # (If your rendered glyphs are wider than the measured widths, bump these.)
    "size_scale": 1.00,
    "width_scale": 1.25,      # <-- primary overlap killer
    "height_scale": 1.00,
    "space_scale": 1.20,      # <-- whitespace overlap killer

    # Optional explicit overrides (px). Leave None to measure.
    "space_px": None,
    "tab_px": None,
    "tab_spaces": 4,

    # Inter-word spacing controls (px)
    "tracking_px": 0.0,
    "min_word_gap_px": 3.0,   # <-- guaranteed gap after each word

    # Expand each word's measured width by this many pixels
    # (helps with blur making glyphs effectively “fatter”)
    "word_pad_px": 3.0,

    # Vertical layout
    "line_spacing": 1.15,
    "baseline_text": "Ag",

    # Approximation factors (only used if backend="approx" or blf unavailable)
    "approx_char_width_factor": 0.55,
    "approx_space_width_factor": 0.55,

    # Optional fixed layout box (px). Keep None for tight bounds.
    "fixed_box_w_px": None,
    "fixed_box_h_px": None,
  },

  # Style (strip-level)
  "use_box": False,
  "box_padding": 0.08,
  "outline": 0,
  "shadow": 0,

  "text_color": "#E6E6E6",
  "text_alpha": 1.0,

  # Blur
  "blur_enable": True,
  "blur_px": 5.0,
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
}
