# =============================================================================
# preset: brat_words_manual_grey_v003_srtbreaks_normalspaces
#
# Goal:
#   - Keep SRT *line breaks* (manual typesetting / intentional newlines)
#   - IGNORE SRT *spacing* (collapse runs of spaces/tabs into single spaces)
#   - Still use manual per-word positioning (no jumping), with blur + light grey
#
# How it works:
#   manual_layout.linebreak_mode = "srt"
#       -> respects the SRT's explicit line breaks
#
#   manual_layout.collapse_spaces = True
#       -> collapses ALL whitespace (including line breaks) into single spaces
#          *UNLESS* we are in "srt" mode, where we preserve line breaks first.
#          In this preset, we want to keep line breaks, so the script preserves
#          line breaks and collapses spaces *within each line* (see note below).
#
# NOTE:
#   This preset assumes your updated main script treats:
#     - linebreak_mode="srt" as "preserve newlines"
#     - collapse_spaces as "collapse whitespace but keep explicit newlines"
#   If collapse_spaces currently collapses newlines too, set it False here and
#   we’ll adjust the main script next to make it "per-line" when mode="srt".
# =============================================================================

PRESET = {
  # --- SRT normalization ---
  "srt_origin": "auto",
  "srt_offset_ms": 0,
  "strip_tags": True,
  "text_transform": "lower",

  # Preserve SRT line breaks (manual mode handles wrapping itself)
  "wrap_chars": 0,
  "max_lines": 0,

  "min_cue_ms": 450,
  "chunk_words": 0,

  # Overlap safety
  "stack_overlaps": True,
  "max_stack_channels": 768,

  "vse_newline": "cr",
  "end_mode": "min",

  # --- Typography ---
  "font_path": "",
  "font_size": 92,

  # Layout anchor for the whole lyric block
  "align_x": "CENTER",
  "align_y": "CENTER",
  "pos_x": None,
  "pos_y": None,
  "safe_margin": 0.08,

  # --- Manual layout math (preset-owned) ---
  "manual_layout": {
    "backend": "blf",
    "dpi": 72,

    # Measurement reconciliation (tune if overlaps)
    "size_scale": 1.00,
    "width_scale": 1.25,
    "height_scale": 1.00,
    "space_scale": 1.15,
    "space_px": None,
    "tab_px": None,
    "tab_spaces": 4,

    # Inter-word spacing
    "tracking_px": 0.0,
    "min_word_gap_px": 3.0,
    "word_pad_px": 3.0,

    "line_spacing": 1.15,
    "baseline_text": "Ag",

    "approx_char_width_factor": 0.55,
    "approx_space_width_factor": 0.55,

    "fixed_box_w_px": None,
    "fixed_box_h_px": None,

    # --- NEW: line breaks vs spacing control ---
    "linebreak_mode": "srt",     # keep SRT newlines
    "collapse_spaces": True,     # collapse spacing (runs of spaces/tabs -> single spaces)
    "fixed_lines": None,         # no clamp

    # Not used in "srt" mode, but harmless to leave:
    "max_line_width_px": None,
    "max_line_width_norm": None,
  },

  # Style
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
