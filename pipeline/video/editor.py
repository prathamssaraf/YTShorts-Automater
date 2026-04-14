"""FFmpeg orchestration: trim → portrait crop → burn subs → overlay PNG → mix music."""
from __future__ import annotations

import logging
import re
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


_HAS_SUBTITLE_FILTER: bool | None = None


def _has_subtitle_filter() -> bool:
    """Return True iff this ffmpeg build has the ass/subtitles filter (libass)."""
    global _HAS_SUBTITLE_FILTER
    if _HAS_SUBTITLE_FILTER is not None:
        return _HAS_SUBTITLE_FILTER
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        _HAS_SUBTITLE_FILTER = False
        return False
    # Filter listing format: " .. <name> ..."
    _HAS_SUBTITLE_FILTER = bool(re.search(r"^\s*\.\.\s+(ass|subtitles)\s+", res.stdout, re.M))
    if not _HAS_SUBTITLE_FILTER:
        log.warning(
            "ffmpeg has no libass support — subtitles will be skipped. "
            "Reinstall full ffmpeg with: brew uninstall --ignore-dependencies ffmpeg && brew install ffmpeg"
        )
    return _HAS_SUBTITLE_FILTER


_SRT_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def _to_ass_time(h: str, m: str, s: str, ms: str) -> str:
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{int(ms) // 10:02d}"


def _srt_to_ass(srt_text: str, out_path: Path, *, font_size: int, play_w: int, play_h: int) -> Path:
    """Convert SRT text to an ASS subtitle file with styling baked in.

    Using ASS instead of force_style= avoids ffmpeg's finicky filter-arg
    escaping — the subtitles filter's force_style handling is inconsistent
    across ffmpeg builds once commas are involved.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_w}\n"
        f"PlayResY: {play_h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,3,1,2,60,60,180,1\n"
        "\n"
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
        match = _SRT_TIME_RE.search(lines[time_idx])
        if not match:
            continue
        g = match.groups()
        start, end = _to_ass_time(*g[:4]), _to_ass_time(*g[4:])
        text = " ".join(lines[time_idx + 1:]).replace("\n", r"\N").replace(",", "\\,")
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


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

    # Step 3 — burn subtitles as ASS (styling baked into the file; no filter-arg escaping).
    # Gracefully skip if this ffmpeg build lacks libass.
    if (
        srt_path and srt_path.exists() and srt_path.stat().st_size > 0
        and _has_subtitle_filter()
    ):
        fontsize = int(cfg.get("subtitle_font_size", 48))
        ass_path = srt_path.with_suffix(".ass")
        _srt_to_ass(
            srt_path.read_text(encoding="utf-8"),
            ass_path,
            font_size=fontsize,
            play_w=w, play_h=h,
        )
        _run([
            "ffmpeg", "-y", "-i", str(portrait),
            "-vf", f"ass={ass_path.as_posix()}",
            "-c:a", "copy", str(subbed),
        ])
    else:
        if srt_path and srt_path.exists() and srt_path.stat().st_size > 0:
            log.warning("Subtitles produced but skipped (no libass in this ffmpeg build).")
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
