"""FFmpeg compositor: per-scene clips + narration + music + subtitles → final Short."""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path

from ..audio.tts import probe_duration
from ..config import load_settings, resolve_path
from ..intelligence.script_writer import StoryScript

log = logging.getLogger(__name__)


class ComposerError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    log.info("ffmpeg: %s", shlex.join(cmd[:8]) + (" …" if len(cmd) > 8 else ""))
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise ComposerError(f"ffmpeg failed ({res.returncode}): {res.stderr[-800:]}")


_HAS_SUBTITLES: bool | None = None


def _has_subtitle_filter() -> bool:
    global _HAS_SUBTITLES
    if _HAS_SUBTITLES is not None:
        return _HAS_SUBTITLES
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        _HAS_SUBTITLES = False
        return False
    _HAS_SUBTITLES = bool(re.search(r"^\s*\.\.\s+(ass|subtitles)\s+", res.stdout, re.M))
    if not _HAS_SUBTITLES:
        log.warning(
            "ffmpeg has no libass — subtitles will be skipped. Install ffmpeg-full: "
            "brew uninstall --ignore-dependencies ffmpeg && brew install ffmpeg-full"
        )
    return _HAS_SUBTITLES


# --- SRT → ASS conversion (same rationale as before; force_style is fragile) ---

_SRT_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def _to_ass_time(h, m, s, ms) -> str:
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{int(ms) // 10:02d}"


def _srt_to_ass(srt_text: str, out_path: Path, *, font_size: int, w: int, h: int) -> Path:
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,3,2,2,80,80,260,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for block in re.split(r"\r?\n\r?\n", srt_text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue
        m = _SRT_TIME_RE.search(lines[time_idx])
        if not m:
            continue
        g = m.groups()
        text = " ".join(lines[time_idx + 1:]).replace("\n", r"\N").replace(",", "\\,")
        events.append(
            f"Dialogue: 0,{_to_ass_time(*g[:4])},{_to_ass_time(*g[4:])},Default,,0,0,0,,{text}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


# --- Per-scene clip prep: trim/loop to target duration, scale to portrait ---

def _prep_scene(src: Path, target_seconds: float, w: int, h: int, out_path: Path) -> Path:
    """Crop to 9:16, scale to (w,h), and trim/pad to exactly target_seconds."""
    src_dur = probe_duration(src)
    if src_dur <= 0:
        raise ComposerError(f"could not probe duration of {src}")

    if src_dur >= target_seconds:
        # Trim
        _run([
            "ffmpeg", "-y", "-ss", "0", "-i", str(src), "-t", f"{target_seconds:.3f}",
            "-vf", f"crop=ih*9/16:ih,scale={w}:{h}:flags=lanczos,fps=30",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(out_path),
        ])
    else:
        # Loop the source until target_seconds is reached
        _run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src), "-t", f"{target_seconds:.3f}",
            "-vf", f"crop=ih*9/16:ih,scale={w}:{h}:flags=lanczos,fps=30",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(out_path),
        ])
    return out_path


def compose(
    *,
    run_id: str,
    script: StoryScript,
    scene_videos: list[Path],
    scene_audios: list[Path],
    narration_audio: Path,
    music_audio: Path,
    srt_path: Path | None,
) -> Path:
    """Stitch everything into the final Short. Returns the output mp4 path."""
    cfg = load_settings()["composer"]
    w, h = cfg["output_resolution"]
    workspace = resolve_path(cfg["workspace_dir"]) / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    out_dir = resolve_path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"final_{run_id}.mp4"

    if len(scene_videos) != len(scene_audios) or len(scene_videos) != len(script.scenes):
        raise ComposerError(
            f"scene count mismatch: videos={len(scene_videos)}, "
            f"audios={len(scene_audios)}, scripted={len(script.scenes)}"
        )

    # Step 1 — prep each scene clip to the duration of its narration
    prepped: list[Path] = []
    for i, (vid, aud) in enumerate(zip(scene_videos, scene_audios)):
        target = max(0.5, probe_duration(aud))
        out = workspace / f"scene_{i:02d}.mp4"
        _prep_scene(vid, target, w, h, out)
        prepped.append(out)

    # Step 2 — concat scene videos
    concat_list = workspace / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in prepped))
    silent_video = workspace / "video.mp4"
    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", str(silent_video),
    ])

    # Step 3 — burn subtitles (or copy through)
    if srt_path and srt_path.exists() and srt_path.stat().st_size > 0 and \
       cfg.get("burn_subtitles", True) and _has_subtitle_filter():
        ass_path = workspace / "subs.ass"
        _srt_to_ass(srt_path.read_text(encoding="utf-8"), ass_path,
                    font_size=int(cfg.get("subtitle_font_size", 56)), w=w, h=h)
        with_subs = workspace / "video_subs.mp4"
        _run([
            "ffmpeg", "-y", "-i", str(silent_video),
            "-vf", f"ass={ass_path.as_posix()}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(with_subs),
        ])
        video_with_subs = with_subs
    else:
        video_with_subs = silent_video

    # Step 4 — mix narration + music
    narration_dur = probe_duration(narration_audio)
    fade_start = max(0.0, narration_dur - 3.0)
    music_volume = float(cfg.get("narration_volume", 1.0))
    narr_volume = float(cfg.get("narration_volume", 1.0))
    music_volume = float(load_settings()["music"].get("volume", 0.18))
    filter_complex = (
        f"[0:a]volume={narr_volume}[v];"
        f"[1:a]volume={music_volume},afade=t=out:st={fade_start:.2f}:d=3[m];"
        f"[v][m]amix=inputs=2:duration=first:dropout_transition=3[aout]"
    )
    mixed_audio = workspace / "mixed.m4a"
    _run([
        "ffmpeg", "-y", "-i", str(narration_audio), "-i", str(music_audio),
        "-filter_complex", filter_complex,
        "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", str(mixed_audio),
    ])

    # Step 5 — mux video + audio
    _run([
        "ffmpeg", "-y", "-i", str(video_with_subs), "-i", str(mixed_audio),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
        "-shortest", str(final),
    ])

    if not final.exists() or final.stat().st_size < 200_000:
        raise ComposerError(f"final file missing/too small: {final}")
    log.info("final Short ready: %s (%.1fs, %.1f MB)",
             final, probe_duration(final), final.stat().st_size / 1e6)
    return final
