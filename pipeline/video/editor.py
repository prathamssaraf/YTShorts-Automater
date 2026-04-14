"""FFmpeg orchestration: trim → portrait crop → burn subs → overlay PNG → mix music."""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


class EditorError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    log.info("ffmpeg: %s", shlex.join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise EditorError(f"ffmpeg failed ({res.returncode}): {res.stderr[-800:]}")


def _probe_duration(path: Path) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def assemble(
    *,
    run_id: str,
    source_video: Path,
    start: float,
    end: float,
    srt_path: Path | None,
    overlay_png: Path | None,
    music_wav: Path | None,
) -> Path:
    cfg = load_settings()["video"]
    w, h = cfg["output_resolution"]
    duration = max(0.1, end - start)

    clips_dir = resolve_path("workspace/clips")
    out_dir = resolve_path("workspace/output")
    clips_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    trimmed = clips_dir / f"{run_id}_trim.mp4"
    portrait = clips_dir / f"{run_id}_9x16.mp4"
    subbed = clips_dir / f"{run_id}_subs.mp4"
    overlaid = clips_dir / f"{run_id}_overlay.mp4"
    final = out_dir / f"final_{run_id}.mp4"

    # Step 1 — trim (accurate seek, re-encode so downstream filters line up)
    _run([
        "ffmpeg", "-y", "-ss", f"{start}", "-i", str(source_video),
        "-t", f"{duration}", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-c:a", "aac", str(trimmed),
    ])

    # Step 2 — centre-crop to 9:16, scale to target
    _run([
        "ffmpeg", "-y", "-i", str(trimmed),
        "-vf", f"crop=ih*9/16:ih,scale={w}:{h}:flags=lanczos",
        "-c:a", "copy", str(portrait),
    ])

    # Step 3 — burn subtitles (or copy through)
    if srt_path and srt_path.exists() and srt_path.stat().st_size > 0:
        fontsize = int(cfg.get("subtitle_font_size", 48))
        # ffmpeg filter-arg value: commas (and colons inside paths) must be escaped
        # with backslash. Absolute macOS paths have no colons, so only commas matter.
        style = f"FontSize={fontsize}\\,Alignment=2\\,Outline=2\\,Shadow=1\\,MarginV=120"
        sub_arg = f"subtitles={srt_path.as_posix()}:force_style={style}"
        _run([
            "ffmpeg", "-y", "-i", str(portrait),
            "-vf", sub_arg, "-c:a", "copy", str(subbed),
        ])
    else:
        _run(["ffmpeg", "-y", "-i", str(portrait), "-c", "copy", str(subbed)])

    # Step 4 — composite overlay PNG
    if overlay_png and overlay_png.exists():
        _run([
            "ffmpeg", "-y", "-i", str(subbed), "-i", str(overlay_png),
            "-filter_complex", "[0:v][1:v]overlay=0:0",
            "-c:a", "copy", str(overlaid),
        ])
    else:
        _run(["ffmpeg", "-y", "-i", str(subbed), "-c", "copy", str(overlaid)])

    # Step 5 — mix music (optional)
    if music_wav and music_wav.exists():
        music_vol = float(cfg.get("music_volume", 0.2))
        commentary_vol = float(cfg.get("commentary_volume", 1.0))
        fade_start = max(0.0, duration - 3.0)
        filter_complex = (
            f"[0:a]volume={commentary_vol}[v];"
            f"[1:a]volume={music_vol},afade=t=out:st={fade_start}:d=3[m];"
            f"[v][m]amix=inputs=2:duration=first:dropout_transition=3[aout]"
        )
        _run([
            "ffmpeg", "-y", "-i", str(overlaid), "-i", str(music_wav),
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(final),
        ])
    else:
        _run(["ffmpeg", "-y", "-i", str(overlaid), "-c", "copy", str(final)])

    # --- verify
    out_duration = _probe_duration(final)
    if not final.exists() or final.stat().st_size < 2_000_000:
        raise EditorError(f"Final file missing or too small: {final}")
    target = int(cfg.get("target_duration_seconds", 55))
    if abs(out_duration - duration) > 5:
        log.warning("Final duration (%.2fs) drifts >5s from trimmed duration (%.2fs).",
                    out_duration, duration)
    log.info("final Short ready: %s (%.1fs, %.1f MB)",
             final, out_duration, final.stat().st_size / 1e6)
    return final
