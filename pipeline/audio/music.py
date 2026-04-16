"""Background music sourcing.

Source = "pixabay" (uses free Pixabay music search) or "silence" (default fallback).
Pixabay key is OPTIONAL — if missing, we fall through to silence with a warning.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import requests

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


def _silence(out_path: Path, seconds: float) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(int(seconds) + 1), str(out_path),
    ], check=False, capture_output=True)
    return out_path


def _pixabay_search(api_key: str, query: str) -> str | None:
    """Return a URL to a downloadable music track or None."""
    url = "https://pixabay.com/api/videos/"  # fallback if music endpoint unavailable
    # Pixabay's music API isn't publicly documented as a v1 endpoint. The reliable path is
    # the audio search via their HTML page, which we'd have to scrape. To keep things simple
    # and robust, we use Pixabay's video API to grab any video tagged with the query and
    # extract its audio track. If you want a richer music library, swap this for FreeSound
    # or a curated local pool.
    try:
        r = requests.get(url, params={"key": api_key, "q": query, "per_page": 5}, timeout=15)
    except requests.RequestException as exc:
        log.warning("Pixabay request failed: %s", exc)
        return None
    if r.status_code != 200:
        log.warning("Pixabay returned %d", r.status_code)
        return None
    try:
        hits = r.json().get("hits") or []
    except ValueError:
        return None
    for hit in hits:
        videos = hit.get("videos") or {}
        for size in ("medium", "small", "tiny", "large"):
            v = videos.get(size, {}).get("url")
            if v:
                return v
    return None


def fetch_music(mood: str, seconds: float, run_id: str) -> Path:
    cfg = load_settings()["music"]
    out_dir = resolve_path(cfg.get("output_dir", "workspace/audio"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}_music.wav"
    source = cfg.get("source", "silence")

    if source == "silence":
        return _silence(out_path, seconds)

    if source == "pixabay":
        key = cfg.get("pixabay_api_key", "")
        if not key:
            log.warning("Pixabay key not set — falling back to silent track")
            return _silence(out_path, seconds)
        query = cfg.get("pixabay_query_template", "{mood}").format(mood=mood)
        url = _pixabay_search(key, query)
        if not url:
            log.warning("Pixabay returned no usable hit for %r — silent track", query)
            return _silence(out_path, seconds)
        # Download then extract audio
        tmp_video = out_dir / f"{run_id}_music_src.mp4"
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with tmp_video.open("wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            fh.write(chunk)
            subprocess.run([
                "ffmpeg", "-y", "-i", str(tmp_video), "-vn", "-acodec", "pcm_s16le",
                "-ar", "44100", str(out_path),
            ], check=False, capture_output=True)
            return out_path
        except Exception as exc:  # noqa: BLE001
            log.warning("Pixabay download failed (%s) — silent track", exc)
            return _silence(out_path, seconds)
        finally:
            tmp_video.unlink(missing_ok=True)

    log.warning("unknown music source %r — silent track", source)
    return _silence(out_path, seconds)
