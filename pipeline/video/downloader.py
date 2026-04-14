"""yt-dlp wrapper. Downloads a chosen video, tries fallbacks, validates result."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


class VideoUnavailableError(RuntimeError):
    pass


def _ydl_opts(output_template: str, rate_limit: str | None, format_str: str, retries: int) -> dict:
    opts = {
        "format": format_str,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": retries,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }
    if rate_limit:
        # yt-dlp expects bytes/sec; accept "5M" syntax by passing ratelimit key
        opts["ratelimit"] = _parse_rate(rate_limit)
    return opts


def _parse_rate(value: str) -> int:
    units = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
    try:
        if value[-1].upper() in units:
            return int(float(value[:-1]) * units[value[-1].upper()])
        return int(value)
    except (ValueError, IndexError):
        return 5 * 1024 * 1024


def download(video_id: str, fallbacks: list[str] | None = None) -> Path:
    """Download `video_id` (trying fallbacks on failure). Returns the final file path."""
    import yt_dlp  # local import — heavy
    cfg = load_settings()["downloader"]
    output_template = str(resolve_path(cfg["output_template"]))
    Path(output_template).parent.mkdir(parents=True, exist_ok=True)
    opts = _ydl_opts(
        output_template,
        cfg.get("rate_limit"),
        cfg.get("format", "best[height<=1080]"),
        int(cfg.get("retries", 3)),
    )
    candidates = [video_id, *(fallbacks or [])]
    last_exc: Exception | None = None
    for vid in candidates:
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
                if path.suffix != ".mp4":
                    path = path.with_suffix(".mp4")
            if path.exists() and path.stat().st_size > 1_000_000:
                log.info("downloaded %s -> %s (%.1f MB)", vid, path, path.stat().st_size / 1e6)
                return path
            last_exc = RuntimeError(f"download produced suspicious file: {path}")
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed for %s: %s", vid, exc)
            last_exc = exc
            continue
    raise VideoUnavailableError(
        f"Could not download any candidate (tried {candidates}): {last_exc}"
    )
