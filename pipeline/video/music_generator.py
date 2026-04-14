"""MusicGen (MLX) wrapper. Falls back to silence if musicgen-mlx is missing."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


def _write_silence(out_path: Path, seconds: int) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(seconds), str(out_path),
    ]
    subprocess.run(cmd, check=False, capture_output=True)
    return out_path


def _generate_with_mlx(prompt: str, seconds: int, out_path: Path) -> Optional[Path]:
    try:
        from musicgen_mlx import generate  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.info("musicgen-mlx not importable (%s)", exc)
        return None
    try:
        generate(prompt=prompt, duration=seconds, output=str(out_path))
        if out_path.exists() and out_path.stat().st_size > 1000:
            return out_path
    except Exception as exc:  # noqa: BLE001
        log.warning("musicgen-mlx generation failed: %s", exc)
    return None


def generate_music(music_prompt: str, run_id: str) -> Path:
    """Produce a background-music WAV. Always returns a valid file (silence on failure)."""
    cfg = load_settings()["music"]
    out_dir = resolve_path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}_music.wav"
    seconds = int(cfg["duration_seconds"]) + 5
    prompt = cfg["prompt_template"].format(mood=music_prompt)

    log.info("generating music: %r (%ds)", prompt, seconds)
    result = _generate_with_mlx(prompt, seconds, out_path)
    if result:
        return result
    log.warning("Falling back to silent music track.")
    return _write_silence(out_path, seconds)
