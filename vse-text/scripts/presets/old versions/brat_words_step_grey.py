# presets/brat_words_step_grey.py
#
# AE tutorial style:
# - reveal based on words
# - smoothness = 0 (hard step reveal)
# - Gaussian blur ~5px
# - very light grey text
#
# Plugin contract:
# - export PRESET (dict) OR get_preset() -> dict

PRESET = {
  "srt_origin": "auto",
  "srt_offset_ms": 0,
  "strip_tags": True,
  "text_transform": "lower",

  "wrap_chars": 0,
  "max_lines": 0,

  "min_cue_ms": 450,
  "chunk_words": 0,

  "stack_overlaps": True,
  "max_stack_channels": 128,

  "vse_newline": "cr",
  "end_mode": "min",

  "font_path": "",
  "font_size": 92,
  "align_x": "CENTER",
  "align_y": "CENTER",
  "safe_margin": 0.08,
  "pos_x": None,
  "pos_y": None,
  "use_box": False,
  "box_padding": 0.08,
  "outline": 0,
  "shadow": 0,

  "text_color": "#E6E6E6",
  "text_alpha": 1.0,

  "blur_enable": True,
  "blur_px": 5.0,
  "blur_channel_offset": 200,

  "reveal": {
    "mode": "words",
    "style": "step",
    "show": "cumulative",
    "preserve_spacing": True,
  },

  "bg_mode": "none",
  "bg_color": "#000000",
  "bg_alpha": 0.0,
  "bg_channel": 3,
}
