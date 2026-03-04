from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import bpy

try:
    import blf  # type: ignore
except Exception:
    blf = None  # type: ignore

# Practical ceiling: Blender/VSE can clamp or behave oddly at high channels.
# Keeping everything <= 128 avoids "phantom overlaps" from clamped channels.
MAX_VSE_CHANNEL = 128

# ============================================================
# vse_bpm_preset_deterministic_bl456.py
#
# Blender target: 4.5.6 LTS
#
# Purpose:
# Overlay timed text (SRT) onto a base video deterministically, with:
# - Manual per-word layout (each word is its own TEXT strip, fixed position)
# - Optional two-stage TRANSFORM "pixelate chain" (downscale then upscale)
# - Optional blur (best-effort)
# - Preset-driven behavior via --preset
#
# Key reliability rules:
# - Set FPS before adding strips.
# - Use integer ms timing (no float drift).
# - Map ms->frame deterministically: frame = 1 + floor((ms*fps + 500)/1000)
# - Treat end as exclusive [start, end_excl)
#
# Presets:
# - Searched relative to this script's directory:
# scripts/presets/<NAME>.py
# scripts/preset_<NAME>.py
# - Preset file must define PRESET (dict) or get_preset() -> dict
# - Preset merged over DEFAULT_PRESET
# ============================================================

# ----------------------------
# Preset loader
# ----------------------------

_PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


def _script_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd().resolve()


def load_preset_plugin(name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    if not _PRESET_NAME_RE.match(name):
        raise ValueError(f"Invalid preset name {name!r}. Use letters/numbers/_/- only.")
    sd = _script_dir()
    candidates = [
        sd / "presets" / f"{name}.py",
        sd / f"preset_{name}.py",
    ]
    for path in candidates:
        if not path.exists():
            continue
        mod_name = f"vse_text_preset_{name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not import preset from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        if hasattr(module, "get_preset"):
            preset = module.get_preset()
        elif hasattr(module, "PRESET"):
            preset = getattr(module, "PRESET")
        else:
            raise RuntimeError(f"Preset file {path} must define PRESET or get_preset().")
        if not isinstance(preset, dict):
            raise RuntimeError(f"Preset {name} must be a dict, got {type(preset)}")
        return preset
    return None


def merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow merge with one-level nested dict merge."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


DEFAULT_PRESET: Dict[str, Any] = {
    # SRT handling
    "srt_origin": "auto",  # auto|resolve_hour|first_to_zero|none
    "srt_offset_ms": 0,
    "strip_tags": True,
    "text_transform": "none",  # none|lower|upper
    # Classic wrapping (used mainly for non-manual modes)
    "wrap_chars": 32,
    "max_lines": 2,
    "min_cue_ms": 450,
    "chunk_words": 0,
    # Out-of-range SRT guardrails
    "allow_out_of_range_srt": False,  # if True, do not auto-fix Resolve-hour offsets
    "clamp_oob_cues": False,  # if True, clamp out-of-range cues into end window (legacy)
    # Overlap strategy
    "stack_overlaps": True,
    "max_stack_channels": 128,
    # VSE newline style for TEXT strips (cr is more reliable in many builds)
    "vse_newline": "cr",  # cr|lf
    # Render end selection
    "end_mode": "min",  # min|video|audio|max
    # Typography + block anchor
    "font_path": "",
    "font_size": 64,
    "align_x": "CENTER",  # LEFT|CENTER|RIGHT
    "align_y": "BOTTOM",  # TOP|CENTER|BOTTOM
    "pos_x": None,  # if None, derived from align + safe_margin
    "pos_y": None,
    "safe_margin": 0.08,
    "use_box": False,
    "box_padding": 0.08,
    "outline": 0,
    "shadow": 0,
    # Color
    "text_color": "#FFFFFF",
    "text_alpha": 1.0,
    # Blur (best-effort)
    "blur_enable": False,
    "blur_px": 0.0,
    "blur_channel_offset": 12,  # keep small; channels are capped
    # Reveal
    "reveal": {
        "mode": "none",  # none|words
        "style": "manual",  # manual|step
        "show": "cumulative",
        "preserve_spacing": False,
    },
    # Manual per-word layout knobs
    "manual_layout": {
        "backend": "approx",  # blf|approx
        "dpi": 72,
        "size_scale": 1.0,
        "width_scale": 1.0,
        "height_scale": 1.0,
        "space_scale": 1.0,
        "space_px": None,
        "tab_px": None,
        "tab_spaces": 4,
        "tracking_px": 0.0,
        "min_word_gap_px": 0.0,
        "word_pad_px": 0.0,
        "line_spacing": 1.15,
        "baseline_text": "Ag",
        "approx_char_width_factor": 0.55,
        "approx_space_width_factor": 0.55,
        "fixed_box_w_px": None,
        "fixed_box_h_px": None,
        # Linebreak policy (manual only)
        "linebreak_mode": "srt",  # srt|reflow|hybrid
        "max_line_width_px": None,
        "max_line_width_norm": None,
        "collapse_spaces": False,
        "fixed_lines": None,
    },
    # Background
    "bg_mode": "none",  # none|full|per_cue
    "bg_color": "#000000",
    "bg_alpha": 0.0,
    "bg_channel": 3,
    # Pixelate chain (two TRANSFORM effects)
    "pixelate": {
        "enable": False,
        "down_sx": 0.22,
        "down_sy": 0.22,
        "up_sx": 4.5,
        "up_sy": 4.5,
        "ch_offset_1": 1,
        "ch_offset_2": 2,
        "interpolation": "NEAREST",  # NEAREST|BILINEAR|BICUBIC
        "hide_source_alpha": 0.0,
        "hide_stage1_alpha": 0.0,
    },
}


def resolve_preset(name: str) -> Dict[str, Any]:
    # A couple tiny built-ins for convenience
    if not name or name == "sub_basic":
        return {}
    plugin = load_preset_plugin(name)
    if plugin is None:
        raise FileNotFoundError(
            f"Preset {name!r} not found. Looked in scripts/presets/{name}.py and scripts/preset_{name}.py"
        )
    return plugin


# ----------------------------
# Timing + parsing
# ----------------------------

@dataclass(frozen=True)
class Cue:
    idx: int
    start_ms: int
    end_ms: int
    text: str


_TAG_RE = re.compile(r"<[^>]+>")


def _parse_timecode_to_ms(tc: str) -> int:
    # 00:00:12,345
    m = re.match(r"^\s*(\d+):(\d+):(\d+),(\d+)\s*$", tc)
    if not m:
        raise ValueError(f"Bad timecode: {tc!r}")
    hh, mm, ss, ms = [int(x) for x in m.groups()]
    return ((hh * 3600 + mm * 60 + ss) * 1000) + ms


def parse_srt(path: Path) -> List[Cue]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    lines = [ln.rstrip("\n") for ln in raw.splitlines()]

    cues: List[Cue] = []
    i = 0
    synth = 1

    while i < len(lines):
        # Skip blank lines
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break

        # Index line (optional / forgiving)
        idx_val = None
        try:
            idx_val = int(lines[i].strip())
            i += 1
        except Exception:
            idx_val = synth
            synth += 1
            # do not advance i; treat current line as timecode

        if i >= len(lines) or "-->" not in lines[i]:
            continue

        left, right = [x.strip() for x in lines[i].split("-->", 1)]
        s = _parse_timecode_to_ms(left)
        e = _parse_timecode_to_ms(right)
        if e <= s:
            i += 1
            continue
        i += 1

        text_lines: List[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        text_block = "\n".join(text_lines).strip()
        cues.append(
            Cue(
                idx=idx_val if idx_val is not None else synth,
                start_ms=s,
                end_ms=e,
                text=text_block,
            )
        )

    cues.sort(key=lambda c: c.start_ms)
    return cues


def normalize_cues(
    cues: List[Cue],
    *,
    origin: str,
    offset_ms: int,
    strip_tags: bool,
    text_transform: str,
) -> List[Cue]:
    if not cues:
        return []

    origin = origin.lower().strip()
    first = cues[0].start_ms

    base_shift = 0
    if origin == "none":
        base_shift = 0
    elif origin == "first_to_zero":
        base_shift = -first
    elif origin == "resolve_hour":
        base_shift = -(first // 3_600_000) * 3_600_000
    elif origin == "auto":
        base_shift = -(first // 3_600_000) * 3_600_000 if first >= 1_800_000 else 0
    else:
        raise ValueError(f"Unknown srt_origin {origin!r}.")

    shift = int(base_shift) + int(offset_ms)

    out: List[Cue] = []
    for c in cues:
        txt = c.text
        if strip_tags:
            txt = _TAG_RE.sub("", txt).strip()
        if text_transform == "lower":
            txt = txt.lower()
        elif text_transform == "upper":
            txt = txt.upper()

        s = c.start_ms + shift
        e = c.end_ms + shift
        if e <= 0:
            continue
        if s < 0:
            s = 0
        out.append(Cue(idx=c.idx, start_ms=int(s), end_ms=int(e), text=txt))

    out.sort(key=lambda x: x.start_ms)
    return out


def maybe_autofix_resolve_hour(
    cues: List[Cue],
    *,
    end_ms_floor: int,
    allow_out_of_range_srt: bool,
) -> List[Cue]:
    """Guardrail for Resolve-hour SRTs accidentally treated as absolute time.

    If the first cue starts far beyond the media end, it's usually because the SRT uses
    Resolve-style timestamps starting at 01:00:00,000 (or later) and srt_origin was set
    to 'none' (or otherwise failed to shift). If we then clamp cues to the media end,
    many cues collapse into the same final window and channel stacking explodes.

    This helper triggers only when it's very likely the cue timeline is out-of-range.
    """
    if allow_out_of_range_srt or not cues:
        return cues

    first = int(cues[0].start_ms)
    end_ms_floor = int(end_ms_floor)

    # Trigger only when the first cue is >= 30 minutes and still well beyond the media end.
    if end_ms_floor > 0 and first >= 1_800_000 and first > (end_ms_floor + 60_000):
        hour_shift = -((first // 3_600_000) * 3_600_000)
        if hour_shift != 0:
            out: List[Cue] = []
            for c in cues:
                s = int(c.start_ms) + int(hour_shift)
                e = int(c.end_ms) + int(hour_shift)
                if e <= 0:
                    continue
                if s < 0:
                    s = 0
                out.append(Cue(idx=c.idx, start_ms=int(s), end_ms=int(e), text=c.text))
            out.sort(key=lambda x: x.start_ms)
            print(
                "WARN: SRT timestamps appear far beyond media duration; "
                f"applying resolve-hour shift ({hour_shift} ms). "
                "If this is intentional, set allow_out_of_range_srt=true in the preset.",
                file=sys.stderr,
            )
            return out

    return cues


def enforce_min_duration(cues: List[Cue], *, min_cue_ms: int) -> List[Cue]:
    if min_cue_ms <= 0:
        return cues
    out: List[Cue] = []
    for i, c in enumerate(cues):
        s, e = c.start_ms, c.end_ms
        if (e - s) >= min_cue_ms:
            out.append(c)
            continue
        target = s + min_cue_ms
        if i + 1 < len(cues):
            target = min(target, cues[i + 1].start_ms)
        if target <= s:
            target = s + 1
        out.append(Cue(idx=c.idx, start_ms=s, end_ms=int(target), text=c.text))
    return out


def split_into_word_chunks(cue: Cue, *, chunk_words: int) -> List[Cue]:
    if chunk_words <= 0:
        return [cue]
    words = [w for w in re.split(r"\s+", cue.text.strip()) if w]
    if not words:
        return []

    n = len(words)
    parts: List[Cue] = []
    dur = cue.end_ms - cue.start_ms
    if dur <= 0:
        return []

    # Split time evenly across chunks (best-effort)
    num_parts = (n + chunk_words - 1) // chunk_words
    for pi in range(num_parts):
        a = pi * chunk_words
        b = min(n, (pi + 1) * chunk_words)
        frac_a = a / max(1, n)
        frac_b = b / max(1, n)
        s = cue.start_ms + int(round(frac_a * dur))
        e = cue.start_ms + int(round(frac_b * dur))
        if e <= s:
            e = s + 1
        txt = " ".join(words[a:b])
        parts.append(Cue(idx=cue.idx, start_ms=int(s), end_ms=int(e), text=txt))
    return parts


def ms_to_frame(ms: int, fps: int) -> int:
    # deterministic rounding to nearest ms frame
    return 1 + ((int(ms) * int(fps) + 500) // 1000)


def seconds_to_frame_excl(seconds: float, fps: int) -> int:
    ms = max(0, int(round(float(seconds) * 1000.0)))
    # end_excl needs to be one frame past the last included frame.
    return max(2, ms_to_frame(ms, int(fps)) + 1)


def frame_excl_to_ms_floor(frame_excl: int, fps: int) -> int:
    # floor ms at end_excl boundary
    return int(((int(frame_excl) - 1) * 1000) // int(fps))


def normalize_vse_newlines(s: str, *, mode: str) -> str:
    mode = (mode or "cr").lower()
    if mode == "lf":
        return s.replace("\r\n", "\n").replace("\r", "\n")
    # cr (default)
    return s.replace("\r\n", "\r").replace("\n", "\r")


def wrap_text(s: str, *, wrap_chars: int, max_lines: int) -> str:
    s = s.strip()
    if not s:
        return ""
    if wrap_chars <= 0:
        return s
    lines = textwrap.wrap(
        s,
        width=int(wrap_chars),
        break_long_words=False,
        break_on_hyphens=False,
    )
    if max_lines > 0:
        lines = lines[: int(max_lines)]
    return "\n".join(lines)


# ----------------------------
# VSE helpers
# ----------------------------

def _ensure_sequence_editor(scene: bpy.types.Scene) -> None:
    if scene.sequence_editor is None:
        scene.sequence_editor_create()


def _top_level_collection(se: bpy.types.SequenceEditor) -> Any:
    # 4.5+ uses se.strips / se.strips_all
    if hasattr(se, "strips"):
        return se.strips
    return se.sequences  # fallback


def _all_strips(se: bpy.types.SequenceEditor) -> Any:
    if hasattr(se, "strips_all"):
        return se.strips_all
    return se.sequences_all  # fallback


def clear_vse(scene: bpy.types.Scene, *, best_effort: bool) -> None:
    se = scene.sequence_editor
    if se is None:
        return
    coll = _top_level_collection(se)
    allc = _all_strips(se)

    # Remove in reverse to avoid dependency issues
    to_remove = list(allc)
    to_remove.reverse()
    for st in to_remove:
        try:
            coll.remove(st)
        except Exception:
            if not best_effort:
                raise


def _strip_start_end_excl(strip: bpy.types.Sequence) -> Tuple[int, int]:
    a = int(strip.frame_start)
    b = int(strip.frame_final_end)
    return a, b


def add_movie(
    scene: bpy.types.Scene,
    filepath: str,
    *,
    channel: int,
    frame_start: int,
) -> bpy.types.Sequence:
    se = scene.sequence_editor
    assert se is not None
    return se.strips.new_movie(
        name=Path(filepath).name,
        filepath=filepath,
        channel=int(channel),
        frame_start=int(frame_start),
    )


def add_sound(
    scene: bpy.types.Scene,
    filepath: str,
    *,
    channel: int,
    frame_start: int,
) -> bpy.types.Sequence:
    se = scene.sequence_editor
    assert se is not None
    return se.strips.new_sound(
        name=Path(filepath).name,
        filepath=filepath,
        channel=int(channel),
        frame_start=int(frame_start),
    )


def mute_movie_audio(movie_strip: bpy.types.Sequence) -> None:
    # movie_strip.sound is not reliable; easiest is mute strip-level volume if present
    if hasattr(movie_strip, "volume"):
        try:
            movie_strip.volume = 0.0
        except Exception:
            pass


def remove_non_master_audio(
    scene: bpy.types.Scene,
    *,
    master_audio_path: str,
    preserve_aux: bool,
) -> None:
    # Remove audio streams from movie strips and extra sound strips (best-effort).
    # In many pipelines, the master audio is explicit and all other audio should be removed.
    se = scene.sequence_editor
    if se is None:
        return
    for st in list(_all_strips(se)):
        try:
            if st.type == "SOUND":
                fp = ""
                if hasattr(st, "sound") and getattr(st, "sound") is not None:
                    fp = str(getattr(st.sound, "filepath", ""))
                if fp and Path(fp).resolve() == Path(master_audio_path).resolve():
                    continue
                if preserve_aux:
                    continue
                _top_level_collection(se).remove(st)
            elif st.type == "MOVIE":
                # mute movie audio
                if hasattr(st, "volume"):
                    st.volume = 0.0
        except Exception:
            continue


def aspect_to_resolution(aspect: str) -> Tuple[int, int]:
    a = aspect.strip().lower()
    if a in {"9:16", "9x16", "vertical"}:
        return (1080, 1920)
    if a in {"16:9", "16x9", "horizontal"}:
        return (1920, 1080)
    raise ValueError(f"Unsupported --aspect {aspect!r}. Use '16:9' or '9:16'.")


def pick_end_excl(*, end_mode: str, video_end_excl: int, audio_end_excl: Optional[int]) -> int:
    m = (end_mode or "min").lower().strip()
    if m == "video":
        return int(video_end_excl)
    if m == "audio":
        return int(audio_end_excl) if audio_end_excl is not None else int(video_end_excl)
    if m == "max":
        return int(max(video_end_excl, audio_end_excl or 0))
    # min
    if audio_end_excl is None:
        return int(video_end_excl)
    return int(min(video_end_excl, audio_end_excl))


def try_set(obj: Any, attr: str, value: Any) -> bool:
    if not hasattr(obj, attr):
        return False
    try:
        setattr(obj, attr, value)
        return True
    except Exception:
        return False


def _effective_render_res_px(scene: bpy.types.Scene) -> Tuple[int, int]:
    rx = int(scene.render.resolution_x)
    ry = int(scene.render.resolution_y)
    pct = float(scene.render.resolution_percentage) / 100.0
    return int(round(rx * pct)), int(round(ry * pct))


def add_effect_with_input(
    scene: bpy.types.Scene,
    *,
    type_: str,
    name: str,
    channel: int,
    frame_start: int,
    frame_end: int,
    seq1: Optional[bpy.types.Sequence],
    seq2: Optional[bpy.types.Sequence],
) -> bpy.types.Sequence:
    """Compatibility wrapper for VSE new_effect inputs across builds."""
    se = scene.sequence_editor
    assert se is not None
    # Blender 4.5+ accepts seq1/seq2 kwargs for effect inputs.
    try:
        st = se.strips.new_effect(
            name=name,
            type=type_,
            channel=int(channel),
            frame_start=int(frame_start),
            frame_end=int(frame_end),
            seq1=seq1,
            seq2=seq2,
        )
        return st
    except TypeError:
        st = se.strips.new_effect(
            name=name,
            type=type_,
            channel=int(channel),
            frame_start=int(frame_start),
            frame_end=int(frame_end),
        )
        # Fallback property names
        if seq1 is not None:
            try_set(st, "input_1", seq1) or try_set(st, "seq1", seq1)
        if seq2 is not None:
            try_set(st, "input_2", seq2) or try_set(st, "seq2", seq2)
        return st


def _set_transform_constant(
    strip: bpy.types.Sequence,
    *,
    sx: float,
    sy: float,
    tx_px: float,
    ty_px: float,
    interpolation: str,
) -> None:
    # Transform strip properties can vary, but 4.5 should have these:
    try_set(strip, "scale_start_x", float(sx))
    try_set(strip, "scale_start_y", float(sy))
    try_set(strip, "translate_start_x", float(tx_px))
    try_set(strip, "translate_start_y", float(ty_px))
    # enum differs between builds; best-effort
    try_set(strip, "interpolation", str(interpolation))


def apply_pixelate_chain(
    scene: bpy.types.Scene,
    *,
    base_strip: bpy.types.Sequence,
    frame_start: int,
    frame_end_excl: int,
    cfg: Dict[str, Any],
    off1: int,
    off2: int,
) -> Tuple[bpy.types.Sequence, Optional[bpy.types.Sequence], Optional[bpy.types.Sequence]]:
    """
    Wrap base_strip:
      PX1: Transform downscale
      PX2: Transform upscale + drift compensation (Transform scales around frame center)

    Drift compensation:
      dx = (x0 - cx) * (1 - net_sx)
      dy = (y0 - cy) * (1 - net_sy)
    """
    if not cfg or not bool(cfg.get("enable", False)):
        return base_strip, None, None

    down_sx = float(cfg.get("down_sx", 1.0))
    down_sy = float(cfg.get("down_sy", 1.0))
    up_sx = float(cfg.get("up_sx", 1.0))
    up_sy = float(cfg.get("up_sy", 1.0))
    interp = str(cfg.get("interpolation", "NEAREST"))

    res_x, res_y = _effective_render_res_px(scene)
    cx = float(res_x) * 0.5
    cy = float(res_y) * 0.5

    x_norm = float(getattr(base_strip, "location_x", 0.5))
    y_norm = float(getattr(base_strip, "location_y", 0.5))
    x0 = x_norm * float(res_x)
    y0 = y_norm * float(res_y)

    net_sx = down_sx * up_sx
    net_sy = down_sy * up_sy
    dx = (x0 - cx) * (1.0 - net_sx)
    dy = (y0 - cy) * (1.0 - net_sy)

    base_ch = int(getattr(base_strip, "channel", 10))

    px1 = add_effect_with_input(
        scene,
        type_="TRANSFORM",
        name=f"PX1_{base_strip.name}",
        channel=int(base_ch) + int(off1),
        frame_start=int(frame_start),
        frame_end=int(frame_end_excl),
        seq1=base_strip,
        seq2=None,
    )
    _set_transform_constant(px1, sx=down_sx, sy=down_sy, tx_px=0.0, ty_px=0.0, interpolation=interp)

    px2 = add_effect_with_input(
        scene,
        type_="TRANSFORM",
        name=f"PX2_{base_strip.name}",
        channel=int(base_ch) + int(off2),
        frame_start=int(frame_start),
        frame_end=int(frame_end_excl),
        seq1=px1,
        seq2=None,
    )
    _set_transform_constant(px2, sx=up_sx, sy=up_sy, tx_px=dx, ty_px=dy, interpolation=interp)

    if cfg.get("hide_source_alpha", None) is not None:
        try_set(base_strip, "blend_alpha", float(cfg.get("hide_source_alpha")))
    if cfg.get("hide_stage1_alpha", None) is not None:
        try_set(px1, "blend_alpha", float(cfg.get("hide_stage1_alpha")))

    return px2, px1, px2


def add_blur_effect(
    scene: bpy.types.Scene,
    *,
    input_strip: bpy.types.Sequence,
    blur_px: float,
    channel: int,
    frame_start: int,
    frame_end_excl: int,
    name: str,
) -> Optional[bpy.types.Sequence]:
    # Best-effort: blur effect availability varies by build.
    # We'll try a few known effect types and set whatever "size" property exists.
    candidates = ["GAUSSIAN_BLUR", "BLUR", "GLOW"]
    for eff_type in candidates:
        try:
            eff = add_effect_with_input(
                scene,
                type_=eff_type,
                name=str(name),
                channel=int(channel),
                frame_start=int(frame_start),
                frame_end=int(frame_end_excl),
                seq1=input_strip,
                seq2=None,
            )
            for attr in ("size_x", "size_y", "sigma", "blur_size", "glow_size"):
                if hasattr(eff, attr):
                    try:
                        setattr(eff, attr, float(blur_px))
                    except Exception:
                        pass
            return eff
        except Exception:
            continue
    return None


def hide_alpha_only(strip: bpy.types.Sequence) -> None:
    # Try to hide original by making it fully transparent but preserve as input.
    if hasattr(strip, "blend_alpha"):
        try:
            strip.blend_alpha = 0.0
            return
        except Exception:
            pass
    if hasattr(strip, "opacity"):
        try:
            strip.opacity = 0.0
        except Exception:
            pass


def parse_hex_color(s: str) -> Tuple[float, float, float]:
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        raise ValueError(f"Bad hex color: {s!r}")
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return float(r), float(g), float(b)


def add_color_strip(
    scene: bpy.types.Scene,
    *,
    rgb: Tuple[float, float, float],
    alpha: float,
    channel: int,
    frame_start: int,
    frame_end_excl: int,
    name: str,
) -> bpy.types.Sequence:
    se = scene.sequence_editor
    assert se is not None
    st = se.strips.new_effect(
        name=str(name),
        type="COLOR",
        channel=int(channel),
        frame_start=int(frame_start),
        frame_end=int(frame_end_excl),
    )
    try_set(st, "color", (float(rgb[0]), float(rgb[1]), float(rgb[2])))
    try_set(st, "blend_alpha", float(alpha))
    return st


def _resolve_position(align_x: str, align_y: str, safe_margin: float) -> Tuple[float, float]:
    ax = align_x.upper()
    ay = align_y.upper()
    if ax == "LEFT":
        x = safe_margin
    elif ax == "RIGHT":
        x = 1.0 - safe_margin
    else:
        x = 0.5
    if ay == "TOP":
        y = 1.0 - safe_margin
    elif ay == "CENTER":
        y = 0.5
    else:
        y = safe_margin
    return x, y


def add_text_effect(
    scene: bpy.types.Scene,
    *,
    text: str,
    channel: int,
    frame_start: int,
    frame_end_excl: int,
    name: str,
) -> bpy.types.Sequence:
    se = scene.sequence_editor
    assert se is not None
    st = se.strips.new_effect(
        name=str(name),
        type="TEXT",
        channel=int(channel),
        frame_start=int(frame_start),
        frame_end=int(frame_end_excl),
    )
    try_set(st, "text", text)
    return st


def apply_text_style(
    strip: bpy.types.Sequence,
    *,
    font_path: str,
    font_size: int,
    align_x: str,
    align_y: str,
    pos_x: float,
    pos_y: float,
    use_box: bool,
    box_padding: float,
    outline: int,
    shadow: int,
    text_rgb: Tuple[float, float, float],
    text_alpha: float,
) -> None:
    if font_path:
        try:
            strip.font = bpy.data.fonts.load(font_path)
        except Exception:
            pass
    # Size property name differs; try both
    for attr in ("font_size", "size"):
        if hasattr(strip, attr):
            try:
                setattr(strip, attr, int(font_size))
            except Exception:
                pass
    try_set(strip, "align_x", str(align_x).upper())
    try_set(strip, "align_y", str(align_y).upper())

    # Position: newer builds use location_x/location_y (normalized)
    if hasattr(strip, "location_x"):
        try_set(strip, "location_x", float(pos_x))
        try_set(strip, "location_y", float(pos_y))
    elif hasattr(strip, "location"):
        try:
            strip.location[0] = float(pos_x)
            strip.location[1] = float(pos_y)
        except Exception:
            pass
    else:
        try_set(strip, "x", float(pos_x))
        try_set(strip, "y", float(pos_y))

    try_set(strip, "use_box", bool(use_box))
    try_set(strip, "box_margin", float(box_padding))
    try_set(strip, "outline_width", int(outline))
    try_set(strip, "shadow", int(shadow))

    # Color / opacity properties vary
    if hasattr(strip, "color"):
        try:
            strip.color = (float(text_rgb[0]), float(text_rgb[1]), float(text_rgb[2]), float(text_alpha))
        except Exception:
            pass
    try_set(strip, "opacity", float(text_alpha))


# ----------------------------
# Manual layout
# ----------------------------

@dataclass
class ManualLayoutConfig:
    backend: str
    dpi: int
    size_scale: float
    width_scale: float
    height_scale: float
    space_scale: float
    space_px: Optional[float]
    tab_px: Optional[float]
    tab_spaces: int
    tracking_px: float
    min_word_gap_px: float
    word_pad_px: float
    line_spacing: float
    baseline_text: str
    approx_char_width_factor: float
    approx_space_width_factor: float
    fixed_box_w_px: Optional[float]
    fixed_box_h_px: Optional[float]
    linebreak_mode: str
    max_line_width_px: Optional[float]
    max_line_width_norm: Optional[float]
    collapse_spaces: bool
    fixed_lines: Optional[int]

    # --- Per-word strip anchoring (manual mode only) ---
    # The "weird spacing" you observed is consistent with a mismatch between
    # layout math (assuming left/top anchoring) and the VSE TEXT strip's actual
    # anchoring semantics. These keys let presets choose the interpretation:
    #   - word_location_mode="left_top": location_x/y correspond to word's left/top
    #   - word_location_mode="center":   location_x/y correspond to word's center
    # And align_x/y are applied per *word strip* (not the overall layout box).
    word_location_mode: str            # "left_top" | "center"
    word_align_x: str                  # LEFT | CENTER | RIGHT
    word_align_y: str                  # TOP  | CENTER | BOTTOM


def resolve_manual_layout_config(preset: Dict[str, Any]) -> ManualLayoutConfig:
    d = dict(preset.get("manual_layout", {}))
    return ManualLayoutConfig(
        backend=str(d.get("backend", "approx")).lower().strip(),
        dpi=int(d.get("dpi", 72)),
        size_scale=float(d.get("size_scale", 1.0)),
        width_scale=float(d.get("width_scale", 1.0)),
        height_scale=float(d.get("height_scale", 1.0)),
        space_scale=float(d.get("space_scale", 1.0)),
        space_px=d.get("space_px", None),
        tab_px=d.get("tab_px", None),
        tab_spaces=int(d.get("tab_spaces", 4)),
        tracking_px=float(d.get("tracking_px", 0.0)),
        min_word_gap_px=float(d.get("min_word_gap_px", 0.0)),
        word_pad_px=float(d.get("word_pad_px", 0.0)),
        line_spacing=float(d.get("line_spacing", 1.15)),
        baseline_text=str(d.get("baseline_text", "Ag")),
        approx_char_width_factor=float(d.get("approx_char_width_factor", 0.55)),
        approx_space_width_factor=float(d.get("approx_space_width_factor", 0.55)),
        fixed_box_w_px=d.get("fixed_box_w_px", None),
        fixed_box_h_px=d.get("fixed_box_h_px", None),
        linebreak_mode=str(d.get("linebreak_mode", "srt")).lower().strip(),
        max_line_width_px=d.get("max_line_width_px", None),
        max_line_width_norm=d.get("max_line_width_norm", None),
        collapse_spaces=bool(d.get("collapse_spaces", False)),
        fixed_lines=d.get("fixed_lines", None),

        # Per-word strip anchoring controls (manual mode)
        word_location_mode=str(d.get("word_location_mode", "left_top")).lower().strip(),
        word_align_x=str(d.get("word_align_x", "LEFT")).upper().strip(),
        word_align_y=str(d.get("word_align_y", "TOP")).upper().strip(),
    )


def _manual_wrap_width_px(scene: bpy.types.Scene, safe_margin: float, cfg: ManualLayoutConfig) -> Optional[float]:
    if cfg.max_line_width_px is not None:
        try:
            return float(cfg.max_line_width_px)
        except Exception:
            pass
    if cfg.max_line_width_norm is not None:
        try:
            return float(cfg.max_line_width_norm) * float(scene.render.resolution_x)
        except Exception:
            pass
    return (1.0 - 2.0 * float(safe_margin)) * float(scene.render.resolution_x)


def _collapse_runs(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s)


def _collapse_lines(s: str) -> str:
    lines = []
    for ln in s.splitlines():
        lines.append(_collapse_runs(ln.strip()))
    return "\n".join([ln for ln in lines if ln])


def _reflow_words_to_lines(words: List[str], *, max_width_chars: int, fixed_lines: Optional[int]) -> str:
    # crude: reflow by approximate character count
    out_lines: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for w in words:
        add = len(w) + (1 if cur else 0)
        if cur and (cur_len + add) > max_width_chars:
            out_lines.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += add
    if cur:
        out_lines.append(" ".join(cur))

    if fixed_lines and fixed_lines > 0:
        if len(out_lines) > fixed_lines:
            keep = out_lines[: fixed_lines - 1]
            tail = " ".join(out_lines[fixed_lines - 1 :])
            out_lines = keep + [tail]
        elif len(out_lines) < fixed_lines:
            out_lines += [""] * (fixed_lines - len(out_lines))

    return "\n".join(out_lines)


def preprocess_text_for_manual_layout(
    s: str,
    *,
    scene: bpy.types.Scene,
    safe_margin: float,
    font_id: Optional[int],
    font_size: int,
    cfg: ManualLayoutConfig,
) -> str:
    s = _TAG_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.strip("\n")
    if cfg.collapse_spaces:
        s = _collapse_lines(s)

    mode = cfg.linebreak_mode
    if mode == "srt":
        return s

    words = [w for w in re.split(r"\s+", s.strip()) if w]
    if not words:
        return ""

    wrap_px = _manual_wrap_width_px(scene, safe_margin, cfg)
    approx_char_w = max(1.0, float(font_size) * float(cfg.approx_char_width_factor))
    max_chars = max(8, int((wrap_px or 600.0) / approx_char_w))
    reflowed = _reflow_words_to_lines(words, max_width_chars=max_chars, fixed_lines=cfg.fixed_lines)

    if mode == "reflow":
        return reflowed

    too_long = False
    for ln in s.splitlines():
        if len(ln) > max_chars:
            too_long = True
            break
    return reflowed if too_long else s


@dataclass
class WordBox:
    text: str
    x_px: float
    y_px: float
    w_px: float
    h_px: float


def _blf_font_id(font_path: str) -> Optional[int]:
    if blf is None:
        return None
    if not font_path:
        return 0
    try:
        return blf.load(font_path)
    except Exception:
        return 0


def _measure_text_px(font_id: Optional[int], font_size: int, s: str, *, cfg: ManualLayoutConfig) -> Tuple[float, float]:
    if cfg.backend == "blf" and blf is not None and font_id is not None:
        try:
            blf.size(font_id, int(font_size), int(cfg.dpi))
            w, h = blf.dimensions(font_id, s)
            return float(w), float(h)
        except Exception:
            pass

    char_w = float(font_size) * float(cfg.approx_char_width_factor)
    space_w = float(font_size) * float(cfg.approx_space_width_factor)
    w = 0.0
    for ch in s:
        w += space_w if ch.isspace() else char_w
    h = float(font_size)
    return w, h


def layout_words_px(
    s: str,
    *,
    font_id: Optional[int],
    font_size: int,
    cfg: ManualLayoutConfig,
) -> Tuple[List[WordBox], float, float]:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = s.split("\n") if s else []
    if not lines:
        return ([], 0.0, 0.0)

    _, base_h = _measure_text_px(font_id, font_size, cfg.baseline_text, cfg=cfg)
    line_h = float(base_h) * float(cfg.line_spacing) * float(cfg.height_scale)

    boxes: List[WordBox] = []
    max_w = 0.0
    total_h = line_h * max(1, len(lines))

    for li, line in enumerate(lines):
        y = (len(lines) - 1 - li) * line_h
        x = 0.0
        for m in re.finditer(r"\s+|\S+", line):
            tok = m.group(0)
            if tok.isspace():
                if "\t" in tok:
                    if cfg.tab_px is not None:
                        x += float(cfg.tab_px)
                    else:
                        sw, _ = _measure_text_px(font_id, font_size, " ", cfg=cfg)
                        x += float(sw) * float(cfg.tab_spaces)
                else:
                    if cfg.space_px is not None:
                        x += float(cfg.space_px) * float(cfg.space_scale) * len(tok)
                    else:
                        sw, _ = _measure_text_px(font_id, font_size, " ", cfg=cfg)
                        x += float(sw) * float(cfg.space_scale) * len(tok)
                continue

            w, h = _measure_text_px(font_id, font_size, tok, cfg=cfg)
            w = float(w) * float(cfg.width_scale)
            h = float(h) * float(cfg.height_scale)
            w += float(cfg.tracking_px) * max(0, len(tok) - 1)
            w += float(cfg.word_pad_px) * 2.0

            boxes.append(WordBox(text=tok, x_px=float(x), y_px=float(y), w_px=float(w), h_px=float(h)))
            x += w + float(cfg.min_word_gap_px)
            max_w = max(max_w, float(x))

    if cfg.fixed_box_w_px is not None:
        max_w = float(cfg.fixed_box_w_px)
    if cfg.fixed_box_h_px is not None:
        total_h = float(cfg.fixed_box_h_px)

    return boxes, float(max_w), float(total_h)


def place_layout_box(
    *,
    scene: bpy.types.Scene,
    box_w_px: float,
    box_h_px: float,
    anchor_x_norm: float,
    anchor_y_norm: float,
    align_x: str,
    align_y: str,
    safe_margin: float,
) -> Tuple[float, float]:
    w = float(scene.render.resolution_x)
    h = float(scene.render.resolution_y)

    ax = (align_x or "CENTER").upper()
    ay = (align_y or "BOTTOM").upper()

    anchor_x_px = float(anchor_x_norm) * w
    anchor_y_px = float(anchor_y_norm) * h

    if ax == "LEFT":
        left = anchor_x_px
    elif ax == "RIGHT":
        left = anchor_x_px - float(box_w_px)
    else:
        left = anchor_x_px - float(box_w_px) * 0.5

    if ay == "TOP":
        top_from_bottom = anchor_y_px - float(box_h_px)
    elif ay == "CENTER":
        top_from_bottom = anchor_y_px - float(box_h_px) * 0.5
    else:
        top_from_bottom = anchor_y_px

    min_x = float(safe_margin) * w
    max_x = (1.0 - float(safe_margin)) * w - float(box_w_px)
    min_y = float(safe_margin) * h
    max_y = (1.0 - float(safe_margin)) * h - float(box_h_px)

    left = max(min_x, min(left, max_x))
    top_from_bottom = max(min_y, min(top_from_bottom, max_y))
    return float(left), float(top_from_bottom)


def word_locations_norm(
    *,
    scene: bpy.types.Scene,
    boxes: List[WordBox],
    box_left_px: float,
    box_top_from_bottom_px: float,
    cfg: ManualLayoutConfig,
) -> List[Tuple[str, float, float]]:
    w = float(scene.render.resolution_x)
    h = float(scene.render.resolution_y)

    mode = (cfg.word_location_mode or "left_top").lower().strip()

    out: List[Tuple[str, float, float]] = []
    for b in boxes:
        if mode == "center":
            # location_x/y should point to the *center* of the rendered word box.
            x_px = float(box_left_px) + float(b.x_px) + 0.5 * float(b.w_px)
            y_px = float(box_top_from_bottom_px) + float(b.y_px) + 0.5 * float(b.h_px)
            x_norm = x_px / w
            y_norm = y_px / h
        else:
            # legacy: location_x is word left; location_y is word top (from bottom)
            x_norm = (float(box_left_px) + float(b.x_px)) / w
            y_top_from_bottom = float(box_top_from_bottom_px) + float(b.y_px) + float(b.h_px)
            y_norm = y_top_from_bottom / h

        out.append((b.text, float(x_norm), float(y_norm)))
    return out


# ----------------------------
# Channel allocation
# ----------------------------

def _channel_overlaps(windows: List[Tuple[int, int]], start: int, end_excl: int) -> bool:
    for a, b in windows:
        if start < b and a < end_excl:
            return True
    return False


def pick_channel_block(
    occupied: Dict[int, List[Tuple[int, int]]],
    *,
    base_channel: int,
    max_stack_channels: int,
    block_len: int,
    start_f: int,
    end_f_excl: int,
    blur_enable: bool,
    blur_offset: int,
    pixelate_enable: bool,
    pixelate_offset_1: int,
    pixelate_offset_2: int,
) -> int:
    if block_len <= 0:
        return int(base_channel)

    lo = int(base_channel)
    hi = int(base_channel) + int(max_stack_channels) - int(block_len)

    max_rel = block_len - 1
    if pixelate_enable:
        max_rel = max(max_rel, (block_len - 1) + int(pixelate_offset_1))
        max_rel = max(max_rel, (block_len - 1) + int(pixelate_offset_2))
    if blur_enable:
        vis_off = int(pixelate_offset_2) if pixelate_enable else 0
        max_rel = max(max_rel, (block_len - 1) + vis_off + int(blur_offset))

    hi = min(hi, int(MAX_VSE_CHANNEL) - int(max_rel))
    if hi < lo:
        raise RuntimeError(
            f"Not enough VSE channels for this cue (need rel={max_rel} from base). "
            f"Lower --text-channel or reduce effects."
        )

    def _overlaps(ch: int) -> bool:
        wins = occupied.setdefault(int(ch), [])
        return _channel_overlaps(wins, int(start_f), int(end_f_excl))

    for cand in range(lo, hi + 1):
        ok = True
        for base_ch in range(cand, cand + block_len):
            if base_ch < 1 or base_ch > MAX_VSE_CHANNEL or _overlaps(base_ch):
                ok = False
                break

            if pixelate_enable:
                c1 = base_ch + int(pixelate_offset_1)
                c2 = base_ch + int(pixelate_offset_2)
                if c1 < 1 or c1 > MAX_VSE_CHANNEL or _overlaps(c1):
                    ok = False
                    break
                if c2 < 1 or c2 > MAX_VSE_CHANNEL or _overlaps(c2):
                    ok = False
                    break

            if blur_enable:
                vis = base_ch + (int(pixelate_offset_2) if pixelate_enable else 0)
                bc = vis + int(blur_offset)
                if bc < 1 or bc > MAX_VSE_CHANNEL or _overlaps(bc):
                    ok = False
                    break

        if ok:
            return int(cand)

    raise RuntimeError(
        f"Too many overlaps; could not allocate channel block of {block_len} within max_stack_channels={max_stack_channels}."
    )


def render_animation(scene: bpy.types.Scene) -> None:
    bpy.ops.render.render(animation=True)


# ----------------------------
# Build timeline
# ----------------------------

def build_timeline(args: argparse.Namespace) -> bpy.types.Scene:
    video_path = Path(args.video).expanduser().resolve()
    srt_path = Path(args.srt).expanduser().resolve()

    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")
    if not srt_path.exists():
        raise FileNotFoundError(f"SRT not found: {srt_path}")

    p = merge_dicts(DEFAULT_PRESET, resolve_preset(str(args.preset)))
    if args.bg_mode is not None:
        p["bg_mode"] = args.bg_mode

    scene = bpy.context.scene
    if not args.no_clear:
        clear_vse(scene, best_effort=bool(args.best_effort))
    _ensure_sequence_editor(scene)

    # FPS before strips
    scene.render.fps = int(args.fps)
    scene.render.fps_base = 1.0

    # Resolution
    if args.width and args.height:
        scene.render.resolution_x = int(args.width)
        scene.render.resolution_y = int(args.height)
    else:
        w, h = aspect_to_resolution(str(args.aspect))
        scene.render.resolution_x = int(w)
        scene.render.resolution_y = int(h)
    scene.render.resolution_percentage = int(args.resolution_percentage)

    # Base video
    movie = add_movie(scene, str(video_path), channel=int(args.video_channel), frame_start=1)
    if args.mute_video_audio:
        mute_movie_audio(movie)

    _, video_end_excl = _strip_start_end_excl(movie)
    if video_end_excl <= 1:
        raise RuntimeError("Base video duration unknown/zero. Transcode to CFR H.264 yuv420p and retry.")

    # Optional audio
    audio_end_excl: Optional[int] = None
    if args.audio:
        audio_path = Path(args.audio).expanduser().resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio not found: {audio_path}")
        snd = add_sound(scene, str(audio_path), channel=int(args.audio_channel), frame_start=1)
        remove_non_master_audio(scene, master_audio_path=str(audio_path), preserve_aux=bool(args.preserve_aux_audio))
        _, ae = _strip_start_end_excl(snd)
        if ae > 1:
            audio_end_excl = ae

    end_excl = pick_end_excl(
        end_mode=str(p["end_mode"]),
        video_end_excl=int(video_end_excl),
        audio_end_excl=audio_end_excl,
    )
    if float(getattr(args, "render_end_seconds", 0.0) or 0.0) > 0.0:
        cap_end_excl = seconds_to_frame_excl(float(args.render_end_seconds), int(args.fps))
        end_excl = min(int(end_excl), int(cap_end_excl))
    if end_excl <= 1:
        raise RuntimeError("Computed render end is invalid after applying render-end-seconds cap.")
    end_ms_floor = frame_excl_to_ms_floor(int(end_excl), int(args.fps))

    # Parse cues
    cues = parse_srt(srt_path)
    cues = normalize_cues(
        cues,
        origin=str(p["srt_origin"]),
        offset_ms=int(p["srt_offset_ms"]),
        strip_tags=bool(p["strip_tags"]),
        text_transform=str(p["text_transform"]),
    )
    cues = maybe_autofix_resolve_hour(
        cues,
        end_ms_floor=int(end_ms_floor),
        allow_out_of_range_srt=bool(p.get("allow_out_of_range_srt", False)),
    )
    cues = enforce_min_duration(cues, min_cue_ms=int(p["min_cue_ms"]))

    cooked: List[Cue] = []
    for c in cues:
        if not c.text.strip():
            continue
        parts = split_into_word_chunks(c, chunk_words=int(p["chunk_words"]))
        for pc in parts:
            if bool(p.get("clamp_oob_cues", False)) or bool(p.get("allow_out_of_range_srt", False)):
                # Legacy behavior: clamp out-of-range cues into the final media window.
                s = max(0, min(int(pc.start_ms), end_ms_floor))
                e = max(s + 1, min(int(pc.end_ms), end_ms_floor))
            else:
                # Standard behavior: drop cues that start after media end; truncate those that cross it.
                s = max(0, int(pc.start_ms))
                e = max(s + 1, int(pc.end_ms))
                if s >= end_ms_floor:
                    continue
                if e > end_ms_floor:
                    e = end_ms_floor

            if e <= s:
                continue

            txt = wrap_text(pc.text, wrap_chars=int(p["wrap_chars"]), max_lines=int(p["max_lines"]))
            txt = normalize_vse_newlines(txt, mode=str(p["vse_newline"]))
            if txt.strip():
                cooked.append(Cue(idx=pc.idx, start_ms=s, end_ms=e, text=txt))

    cooked.sort(key=lambda x: x.start_ms)

    # Background
    bg_mode = str(p["bg_mode"]).lower().strip()
    rgb_bg = parse_hex_color(str(p["bg_color"]))
    alpha_bg = float(p["bg_alpha"])
    bg_base_ch = int(p["bg_channel"])

    if bg_mode == "full":
        add_color_strip(
            scene,
            rgb=rgb_bg,
            alpha=alpha_bg,
            channel=bg_base_ch,
            frame_start=1,
            frame_end_excl=int(end_excl),
            name="BG_FULL",
        )

    # Typography / position
    align_x = str(p["align_x"])
    align_y = str(p["align_y"])
    safe_margin = float(p["safe_margin"])
    pos_x = p["pos_x"]
    pos_y = p["pos_y"]
    if pos_x is None or pos_y is None:
        dx, dy = _resolve_position(align_x, align_y, safe_margin)
        if pos_x is None:
            pos_x = dx
        if pos_y is None:
            pos_y = dy

    text_rgb = parse_hex_color(str(p["text_color"]))
    text_alpha = float(p["text_alpha"])

    # Effects config
    blur_enable = bool(p["blur_enable"])
    blur_px = float(p["blur_px"])
    blur_off = int(p["blur_channel_offset"])
    pix_cfg = dict(p.get("pixelate", {}))
    pix_enable = bool(pix_cfg.get("enable", False))

    occupied: Dict[int, List[Tuple[int, int]]] = {}
    occupied_bg: Dict[int, List[Tuple[int, int]]] = {}

    base_text_ch = int(args.text_channel)
    max_stack = int(p["max_stack_channels"])
    if max_stack > (MAX_VSE_CHANNEL - base_text_ch + 1):
        max_stack = max(1, MAX_VSE_CHANNEL - base_text_ch + 1)

    reveal = dict(p.get("reveal", {}))
    reveal_mode = str(reveal.get("mode", "none")).lower().strip()
    reveal_style = str(reveal.get("style", "manual")).lower().strip()
    reveal_show = str(reveal.get("show", "cumulative")).lower().strip()

    for cue in cooked:
        start_f = ms_to_frame(int(cue.start_ms), int(args.fps))
        end_f_excl = ms_to_frame(int(cue.end_ms), int(args.fps))
        start_f = max(1, min(start_f, int(end_excl)))
        end_f_excl = max(start_f + 1, min(end_f_excl, int(end_excl)))

        # Per-cue pixelate locals: do NOT carry offsets across cues.
        pix_cfg_local = dict(pix_cfg) if isinstance(pix_cfg, dict) else {}
        pix_off1_local = int(pix_cfg_local.get("ch_offset_1", 1)) if pix_enable else 1
        pix_off2_local = int(pix_cfg_local.get("ch_offset_2", 2)) if pix_enable else 2
        if pix_enable:
            pix_cfg_local["ch_offset_1"] = int(pix_off1_local)
            pix_cfg_local["ch_offset_2"] = int(pix_off2_local)

        do_manual_words = (reveal_mode == "words" and reveal_style == "manual" and reveal_show == "cumulative")
        if do_manual_words:
            ml = resolve_manual_layout_config(p)
            font_id = _blf_font_id(str(p["font_path"]) if p["font_path"] else "") if (ml.backend == "blf") else None

            manual_text = preprocess_text_for_manual_layout(
                cue.text,
                scene=scene,
                safe_margin=safe_margin,
                font_id=font_id,
                font_size=int(p["font_size"]),
                cfg=ml,
            )
            boxes, box_w_px, box_h_px = layout_words_px(
                manual_text,
                font_id=font_id,
                font_size=int(p["font_size"]),
                cfg=ml,
            )
            n_words = len(boxes)
            if n_words <= 0:
                continue

            if pix_enable:
                pix_off1_local = int(n_words)
                pix_off2_local = int(2 * n_words)
                pix_cfg_local["ch_offset_1"] = int(pix_off1_local)
                pix_cfg_local["ch_offset_2"] = int(pix_off2_local)

            base_word_ch = pick_channel_block(
                occupied,
                base_channel=int(base_text_ch),
                max_stack_channels=int(max_stack),
                block_len=int(n_words),
                start_f=int(start_f),
                end_f_excl=int(end_f_excl),
                blur_enable=bool(blur_enable and blur_px > 0),
                blur_offset=int(blur_off),
                pixelate_enable=bool(pix_enable),
                pixelate_offset_1=int(pix_off1_local),
                pixelate_offset_2=int(pix_off2_local),
            )

            if bg_mode == "per_cue":
                bg_ch = bg_base_ch + (base_word_ch - base_text_ch)
                wins = occupied_bg.setdefault(bg_ch, [])
                if _channel_overlaps(wins, start_f, end_f_excl):
                    raise RuntimeError(f"BG overlap on channel {bg_ch} at cue {cue.idx}")
                add_color_strip(
                    scene,
                    rgb=rgb_bg,
                    alpha=alpha_bg,
                    channel=bg_ch,
                    frame_start=start_f,
                    frame_end_excl=end_f_excl,
                    name=f"BG_{cue.idx}",
                )
                wins.append((start_f, end_f_excl))

            box_left_px, box_top_px = place_layout_box(
                scene=scene,
                box_w_px=float(box_w_px),
                box_h_px=float(box_h_px),
                anchor_x_norm=float(pos_x),
                anchor_y_norm=float(pos_y),
                align_x=str(align_x),
                align_y=str(align_y),
                safe_margin=float(safe_margin),
            )
            word_locs = word_locations_norm(
                scene=scene,
                boxes=boxes,
                box_left_px=float(box_left_px),
                box_top_from_bottom_px=float(box_top_px),
                cfg=ml,
            )

            total_frames = int(end_f_excl) - int(start_f)
            for j, (wtxt, wx, wy) in enumerate(word_locs):
                w_start = int(start_f) + (int(j) * int(total_frames)) // max(1, int(n_words))
                w_start = max(int(start_f), min(int(w_start), int(end_f_excl) - 1))
                w_end_excl = int(end_f_excl)

                ch = int(base_word_ch) + int(j)
                w_strip = add_text_effect(
                    scene,
                    text=str(wtxt),
                    channel=int(ch),
                    frame_start=int(w_start),
                    frame_end_excl=int(w_end_excl),
                    name=f"TXT_{cue.idx}_W{j+1}",
                )
                apply_text_style(
                    w_strip,
                    font_path=str(p["font_path"]) if p["font_path"] else "",
                    font_size=int(p["font_size"]),
                    align_x=str(ml.word_align_x),
                    align_y=str(ml.word_align_y),
                    pos_x=float(wx),
                    pos_y=float(wy),
                    use_box=False,
                    box_padding=float(p["box_padding"]),
                    outline=int(p["outline"]),
                    shadow=int(p["shadow"]),
                    text_rgb=text_rgb,
                    text_alpha=text_alpha,
                )
                occupied.setdefault(int(ch), []).append((int(w_start), int(w_end_excl)))

                visible = w_strip
                px1 = None
                px2 = None
                if pix_enable:
                    visible, px1, px2 = apply_pixelate_chain(
                        scene,
                        base_strip=w_strip,
                        frame_start=int(w_start),
                        frame_end_excl=int(w_end_excl),
                        cfg=pix_cfg_local,
                        off1=int(pix_off1_local),
                        off2=int(pix_off2_local),
                    )
                    if px1 is not None:
                        occupied.setdefault(int(px1.channel), []).append((int(w_start), int(w_end_excl)))
                    if px2 is not None:
                        occupied.setdefault(int(px2.channel), []).append((int(w_start), int(w_end_excl)))

                if blur_enable and blur_px > 0:
                    blur_ch = int(getattr(visible, "channel", ch)) + int(blur_off)
                    if blur_ch > MAX_VSE_CHANNEL:
                        raise RuntimeError(f"Blur channel exceeds MAX_VSE_CHANNEL: {blur_ch}")
                    eff = add_blur_effect(
                        scene,
                        input_strip=visible,
                        blur_px=float(blur_px),
                        channel=int(blur_ch),
                        frame_start=int(w_start),
                        frame_end_excl=int(w_end_excl),
                        name=f"BLUR_{cue.idx}_W{j+1}",
                    )
                    if eff is not None:
                        hide_alpha_only(visible)
                        occupied.setdefault(int(blur_ch), []).append((int(w_start), int(w_end_excl)))

        else:
            # Non-manual fallback: single strip for whole cue
            text_ch = base_text_ch
            if bool(p["stack_overlaps"]):

                def _cand_ok(cand: int) -> bool:
                    chans: List[int] = [int(cand)]
                    if pix_enable:
                        chans.append(int(cand) + int(pix_off1_local))
                        chans.append(int(cand) + int(pix_off2_local))
                    if blur_enable and blur_px > 0:
                        vis_base = int(cand) + (int(pix_off2_local) if pix_enable else 0)
                        chans.append(int(vis_base) + int(blur_off))
                    for cch in chans:
                        if cch < 1 or cch > MAX_VSE_CHANNEL:
                            return False
                        wins = occupied.setdefault(int(cch), [])
                        if _channel_overlaps(wins, start_f, end_f_excl):
                            return False
                    return True

                for cand in range(base_text_ch, base_text_ch + max_stack):
                    if _cand_ok(cand):
                        text_ch = cand
                        break
                else:
                    raise RuntimeError("Too many overlapping cues; increase max_stack_channels or reduce effects.")

            else:
                wins = occupied.setdefault(int(text_ch), [])
                if _channel_overlaps(wins, start_f, end_f_excl):
                    raise RuntimeError("Overlapping cue detected and stack_overlaps is False.")

            if bg_mode == "per_cue":
                bg_ch = bg_base_ch + (text_ch - base_text_ch)
                wins = occupied_bg.setdefault(bg_ch, [])
                if _channel_overlaps(wins, start_f, end_f_excl):
                    raise RuntimeError(f"BG overlap on channel {bg_ch} at cue {cue.idx}")
                add_color_strip(
                    scene,
                    rgb=rgb_bg,
                    alpha=alpha_bg,
                    channel=bg_ch,
                    frame_start=start_f,
                    frame_end_excl=end_f_excl,
                    name=f"BG_{cue.idx}",
                )
                wins.append((start_f, end_f_excl))

            st = add_text_effect(
                scene,
                text=cue.text,
                channel=int(text_ch),
                frame_start=int(start_f),
                frame_end_excl=int(end_f_excl),
                name=f"TXT_{cue.idx}",
            )
            apply_text_style(
                st,
                font_path=str(p["font_path"]) if p["font_path"] else "",
                font_size=int(p["font_size"]),
                align_x=str(align_x),
                align_y=str(align_y),
                pos_x=float(pos_x),
                pos_y=float(pos_y),
                use_box=bool(p["use_box"]),
                box_padding=float(p["box_padding"]),
                outline=int(p["outline"]),
                shadow=int(p["shadow"]),
                text_rgb=text_rgb,
                text_alpha=text_alpha,
            )
            occupied.setdefault(int(text_ch), []).append((int(start_f), int(end_f_excl)))

            visible = st
            px1 = None
            px2 = None
            if pix_enable:
                visible, px1, px2 = apply_pixelate_chain(
                    scene,
                    base_strip=st,
                    frame_start=int(start_f),
                    frame_end_excl=int(end_f_excl),
                    cfg=pix_cfg_local,
                    off1=int(pix_off1_local),
                    off2=int(pix_off2_local),
                )
                if px1 is not None:
                    occupied.setdefault(int(px1.channel), []).append((int(start_f), int(end_f_excl)))
                if px2 is not None:
                    occupied.setdefault(int(px2.channel), []).append((int(start_f), int(end_f_excl)))

            if blur_enable and blur_px > 0:
                blur_ch = int(getattr(visible, "channel", text_ch)) + int(blur_off)
                if blur_ch > MAX_VSE_CHANNEL:
                    raise RuntimeError(f"Blur channel exceeds MAX_VSE_CHANNEL: {blur_ch}")
                eff = add_blur_effect(
                    scene,
                    input_strip=visible,
                    blur_px=float(blur_px),
                    channel=int(blur_ch),
                    frame_start=int(start_f),
                    frame_end_excl=int(end_f_excl),
                    name=f"BLUR_{cue.idx}",
                )
                if eff is not None:
                    hide_alpha_only(visible)
                    occupied.setdefault(int(blur_ch), []).append((int(start_f), int(end_f_excl)))

    scene.frame_start = 1
    scene.frame_end = int(end_excl) - 1
    scene.render.filepath = str(Path(args.output).expanduser().resolve())
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = args.ffmpeg_format
    scene.render.ffmpeg.codec = args.vcodec
    scene.render.ffmpeg.audio_codec = args.acodec
    return scene


# ----------------------------
# CLI
# ----------------------------

def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--srt", required=True)
    ap.add_argument("--audio", default="")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument(
        "--render-end-seconds",
        type=float,
        default=0.0,
        help="Optional cap for render duration in seconds from frame 1. 0 disables cap.",
    )
    ap.add_argument("--video-channel", type=int, default=2)
    ap.add_argument("--audio-channel", type=int, default=1)
    ap.add_argument("--text-channel", type=int, default=10)
    ap.add_argument("--preset", default="sub_basic")
    ap.add_argument("--bg-mode", dest="bg_mode", default=None, choices=["none", "full", "per_cue"])
    ap.add_argument("--output", required=True)
    ap.add_argument("--ffmpeg-format", default="MPEG4")
    ap.add_argument("--vcodec", default="H264")
    ap.add_argument("--acodec", default="AAC")
    ap.add_argument("--resolution-percentage", type=int, default=100)
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--best-effort", action="store_true")
    ap.add_argument("--no-clear", action="store_true")
    ap.add_argument("--mute-video-audio", action="store_true")
    ap.add_argument("--preserve-aux-audio", action="store_true")
    ap.add_argument("--no-render", action="store_true")

    args = ap.parse_args(argv)

    if not getattr(bpy.app.build_options, "codec_ffmpeg", False):
        raise RuntimeError("This Blender build does not have FFmpeg enabled (codec_ffmpeg is False).")

    scene = build_timeline(args)
    if not args.no_render:
        render_animation(scene)
    return 0


if __name__ == "__main__":
    try:
        user_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
        raise SystemExit(main(user_args))
    except SystemExit:
        raise
    except Exception as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        raise
