# =============================================================================
# README (preset): brat_words_step_grey_pinned
#
# Goal:
#   Word-by-word reveal (hard steps, like AE “Based on Words” with Smoothness=0),
#   but WITHOUT the phrase re-centering / jumping as new words appear.
#
# How it works (conceptually):
#   - Preserve the line breaks already present in the .srt file (no auto-wrap).
#   - Pin the text block to a stable anchor so existing words stay in place:
#       * horizontal pin: LEFT aligned (text grows to the right)
#       * vertical pin:   TOP aligned  (new lines grow downward)
#   - Apply a soft blur (~5px) and very light grey color.
#
# Result:
#   The first visible word establishes the block position. Subsequent words
#   “pop in” to the right (and onto new lines where the SRT already breaks),
#   without shifting previously shown text.
# =============================================================================

PRESET = {
  # Timing / parsing
  "srt_origin": "auto",
  "srt_offset_ms": 0,
  "strip_tags": True,
  "text_transform": "lower",

  # Preserve SRT formatting exactly (line breaks & spacing)
  "wrap_chars": 0,
  "max_lines": 0,

  "min_cue_ms": 450,
  "chunk_words": 0,

  # Overlap behavior
  "stack_overlaps": True,
  "max_stack_channels": 128,

  # Newline handling (VSE tends to be more reliable with CR)
  "vse_newline": "cr",

  # Render length selection
  "end_mode": "min",

  # Text style/layout
  "font_path": "",
  "font_size": 92,

  # CRITICAL: pin the block so it doesn't “jump” as it grows
  "align_x": "LEFT",    # grows to the right
  "align_y": "TOP",     # grows downward when SRT line breaks appear

  # Pinned anchor position (no extra CLI flags required)
  "pos_x": 0.18,
  "pos_y": 0.78,

  "safe_margin": 0.08,
  "use_box": False,
  "box_padding": 0.08,
  "outline": 0,
  "shadow": 0,

  # Very light grey
  "text_color": "#E6E6E6",
  "text_alpha": 1.0,

  # Blur (Gaussian-ish ~5px)
  "blur_enable": True,
  "blur_px": 5.0,
  "blur_channel_offset": 200,

  # Reveal: based on words, hard steps (smoothness=0 feel)
  "reveal": {
    "mode": "words",
    "style": "step",
    "show": "cumulative",
    "preserve_spacing": True,
  },

  # Background (off)
  "bg_mode": "none",
  "bg_color": "#000000",
  "bg_alpha": 0.0,
  "bg_channel": 3,
}
