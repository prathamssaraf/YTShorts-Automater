"""Kokoro-ONNX TTS wrapper.

Model + voices files are downloaded into ./vendor/kokoro/ by run.sh.
Output is one WAV per scene + one concatenated narration.wav for whisper.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


_kokoro = None


def _get_kokoro():
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    cfg = load_settings()["tts"]
    model = resolve_path(cfg["model_path"])
    voices = resolve_path(cfg["voices_path"])
    if not model.exists() or not voices.exists():
        raise TTSError(
            f"Kokoro model files missing. Expected:\n  {model}\n  {voices}\n"
            "Run ./run.sh --bootstrap-only to download them."
        )
    try:
        from kokoro_onnx import Kokoro
    except ImportError as exc:
        raise TTSError("kokoro-onnx not installed (pip install kokoro-onnx)") from exc
    log.info("loading Kokoro TTS from %s", model)
    _kokoro = Kokoro(str(model), str(voices))
    return _kokoro


def synthesize(text: str, out_path: Path) -> Path:
    """Synthesise `text` to a WAV file at `out_path`. Returns the path."""
    cfg = load_settings()["tts"]
    if not text.strip():
        raise TTSError("empty narration text")
    kokoro = _get_kokoro()
    import soundfile as sf
    samples, sr = kokoro.create(
        text,
        voice=cfg.get("voice", "af_heart"),
        speed=float(cfg.get("speed", 1.0)),
        lang=cfg.get("language", "en-us"),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), samples, sr)
    return out_path


def concat_wavs(wavs: list[Path], out_path: Path) -> Path:
    """Concatenate multiple WAVs (re-encoded to a single WAV) using FFmpeg."""
    if not wavs:
        raise TTSError("no audio files to concatenate")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(wavs) == 1:
        # Just copy
        subprocess.run(["ffmpeg", "-y", "-i", str(wavs[0]), "-c", "copy", str(out_path)],
                       check=False, capture_output=True)
        return out_path
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text("\n".join(f"file '{w.resolve().as_posix()}'" for w in wavs))
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out_path),
    ], check=False, capture_output=True)
    list_file.unlink(missing_ok=True)
    return out_path


def probe_duration(path: Path) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0
