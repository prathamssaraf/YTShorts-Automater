"""whisper.cpp subprocess wrapper. Extracts audio, transcribes, cleans up cricket terms."""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


CRICKET_FIXES = {
    "donnie": "Dhoni",
    "donny": "Dhoni",
    "kovvy": "Kohli",
    "coli": "Kohli",
    "kohley": "Kohli",
    "walker": "yorker",
    "yorka": "yorker",
    "bumbrah": "Bumrah",
    "boomrah": "Bumrah",
    "rohit": "Rohit",
    "root sharma": "Rohit Sharma",
    "pant": "Pant",
    "hardik": "Hardik",
    "jadeja": "Jadeja",
    "rashid": "Rashid",
    "gill": "Gill",
}

_FILLERS = re.compile(r"\b(uh+m?|erm+|hmm+)\b", re.IGNORECASE)


def _apply_fixes(text: str) -> str:
    out = _FILLERS.sub("", text)
    for wrong, right in CRICKET_FIXES.items():
        out = re.sub(rf"\b{re.escape(wrong)}\b", right, out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip()


def _locate_binary(settings_path: str) -> Path:
    """whisper.cpp renamed the binary across versions: main -> whisper-cli."""
    candidates = [
        resolve_path(settings_path),
        resolve_path("vendor/whisper.cpp/main"),
        resolve_path("vendor/whisper.cpp/build/bin/whisper-cli"),
        resolve_path("vendor/whisper.cpp/build/bin/main"),
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    raise FileNotFoundError(
        f"whisper.cpp binary not found. Tried: {[str(c) for c in candidates]}. "
        "Run ./run.sh --bootstrap-only to build it."
    )


def _extract_wav(video_path: Path, start: float, duration: float) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start}", "-t", f"{duration}",
        "-i", str(video_path), "-ac", "1", "-ar", "16000", "-vn", str(tmp),
    ]
    subprocess.run(cmd, check=False, capture_output=True)
    return tmp


def _parse_srt(srt_text: str) -> list[dict]:
    out: list[dict] = []
    blocks = re.split(r"\r?\n\r?\n", srt_text.strip())
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        time_line = lines[1] if "-->" in lines[1] else (lines[0] if "-->" in lines[0] else None)
        if not time_line:
            continue
        text_lines = lines[2:] if "-->" in lines[1] else lines[1:]
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            time_line,
        )
        if not m:
            continue
        def ms(h, mm, s, ml):
            return int(h) * 3600000 + int(mm) * 60000 + int(s) * 1000 + int(ml)
        start_ms = ms(*m.groups()[:4])
        end_ms = ms(*m.groups()[4:])
        out.append({"start_ms": start_ms, "end_ms": end_ms, "text": " ".join(text_lines).strip()})
    return out


def transcribe(video_path: Path, start: float, duration: float) -> tuple[str, list[dict]]:
    """Return (srt_text, parsed_segments). Empty both on failure."""
    cfg = load_settings()["transcription"]
    try:
        binary = _locate_binary(cfg["whisper_cpp_binary"])
    except FileNotFoundError as exc:
        log.warning("%s", exc)
        return ("", [])

    model_path = resolve_path(cfg["whisper_model_path"])
    if not model_path.exists():
        model_path = resolve_path(cfg.get("fallback_model_path", ""))
    if not model_path.exists():
        log.warning("No whisper ggml model found — skipping subtitles.")
        return ("", [])

    wav = _extract_wav(video_path, start, duration)
    out_prefix = wav.with_suffix("")
    try:
        cmd = [
            str(binary),
            "-m", str(model_path),
            "-f", str(wav),
            "-l", cfg.get("language", "en"),
            "-osrt",
            "-of", str(out_prefix),
        ]
        log.info("running whisper.cpp: %s", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            log.warning("whisper.cpp exited %d: %s", res.returncode, res.stderr[-400:])
            return ("", [])
        srt_path = Path(str(out_prefix) + ".srt")
        if not srt_path.exists():
            log.warning("whisper.cpp did not produce an SRT at %s", srt_path)
            return ("", [])
        srt_text = srt_path.read_text(encoding="utf-8")
        srt_path.unlink(missing_ok=True)
        cleaned = _apply_fixes(srt_text)
        segments = _parse_srt(cleaned)
        return (cleaned, segments)
    finally:
        try:
            wav.unlink()
        except OSError:
            pass
