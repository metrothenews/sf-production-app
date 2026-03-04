# =============================================================================
# preset: brat_words_manual_grey_v001
#
# Requires main script support for:
#   reveal.mode="words", reveal.style="manual", reveal.show="cumulative"
# (manual per-word positioning with measured widths; no re-centering/jumping)
# =============================================================================

PRESET = {
  # --- SRT normalization ---
  "srt_origin": "auto",
  "srt_offset_ms": 0,
  "strip_tags": True,
  "text_transform": "lower",

  # Preserve exact line breaks/spaces from the SRT
  "wrap_chars": 0,
  "max_lines": 0,

  "min_cue_ms": 450,
  "chunk_words": 0,

  # Heavy-overlap safe
  "stack_overlaps": True,
  "max_stack_channels": 512,

  # Newline mode (CR tends to be safest inside VSE text strips)
  "vse_newline": "cr",

  # Render end selection
  "end_mode": "min",

  # --- Typography ---
  "font_path": "",
  "font_size": 92,

  # Manual layout engine knobs
  "line_spacing": 1.15,
  "tracking_px": 0.0,
  "tab_spaces": 4,

  # Layout box anchor (where the whole block sits on screen)
  # If pos_x/pos_y are None, script will derive a default from align_x/align_y + safe_margin.
  "align_x": "CENTER",   # LEFT|CENTER|RIGHT
  "align_y": "CENTER",   # BOTTOM|CENTER|TOP
  "pos_x": None,
  "pos_y": None,
  "safe_margin": 0.08,

  # Styling
  "use_box": False,
  "box_padding": 0.08,
  "outline": 0,
  "shadow": 0,

  "text_color": "#E6E6E6",
  "text_alpha": 1.0,

  # Blur (best-effort; script will fall back if effect type not available)
  "blur_enable": True,
  "blur_px": 5.0,
  "blur_channel_offset": 200,

  # Manual per-word cumulative reveal (stable)
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
