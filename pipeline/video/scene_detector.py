"""PySceneDetect + librosa-based selection of the best sub-clip window."""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from ..config import load_settings

log = logging.getLogger(__name__)


def _ffprobe_duration(path: Path) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def _extract_audio(video_path: Path) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-ac", "1", "-ar", "16000",
         "-vn", str(tmp)],
        check=False, capture_output=True,
    )
    return tmp


def _audio_energy_window(wav_path: Path, window_seconds: float = 10.0) -> tuple[float, float]:
    try:
        import librosa
        y, sr = librosa.load(str(wav_path), sr=16000, mono=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("librosa failed (%s). Returning (0,0).", exc)
        return (0.0, 0.0)
    if y.size == 0 or sr == 0:
        return (0.0, 0.0)
    hop = sr  # 1s bins
    bins = int(len(y) / hop)
    if bins < 1:
        return (0.0, 0.0)
    energies = np.array([np.sqrt(np.mean(y[i * hop:(i + 1) * hop] ** 2)) for i in range(bins)])
    win = int(window_seconds)
    if bins <= win:
        return (0.0, float(bins))
    sliding = np.convolve(energies, np.ones(win) / win, mode="valid")
    best = int(np.argmax(sliding))
    return (float(best), float(best + win))


def _pyscene_boundaries(video_path: Path) -> list[tuple[float, float]]:
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import AdaptiveDetector
    except Exception as exc:  # noqa: BLE001
        log.warning("scenedetect unavailable (%s); using single-scene fallback.", exc)
        return []
    video = open_video(str(video_path))
    mgr = SceneManager()
    mgr.add_detector(AdaptiveDetector())
    mgr.detect_scenes(video)
    scenes = mgr.get_scene_list()
    return [(s.get_seconds(), e.get_seconds()) for s, e in scenes]


def select_window(video_path: Path) -> tuple[float, float]:
    """Return (start_seconds, end_seconds) of the best window to clip."""
    cfg = load_settings()["video"]
    target = int(cfg["target_duration_seconds"])
    min_clip = int(cfg["min_clip_duration_seconds"])
    max_clip = int(cfg["max_clip_duration_seconds"])

    duration = _ffprobe_duration(video_path)
    if duration <= target + 10:
        end = min(duration, float(max_clip))
        log.info("video <=target+10 (%.1fs); using 0..%.1fs", duration, end)
        return (0.0, end)

    wav = _extract_audio(video_path)
    try:
        peak_start, peak_end = _audio_energy_window(wav)
    finally:
        try:
            wav.unlink()
        except OSError:
            pass

    scenes = _pyscene_boundaries(video_path)
    best_start = max(0.0, peak_start - 5.0)
    best_end = min(duration, best_start + float(target))

    if scenes:
        # snap to nearest scene boundary for the start
        candidates = [s for s, _e in scenes if s <= peak_start]
        if candidates:
            best_start = max(0.0, candidates[-1])
        best_end = min(duration, best_start + float(target))

    # Enforce min/max
    if best_end - best_start < min_clip:
        best_end = min(duration, best_start + min_clip)
    if best_end - best_start > max_clip:
        best_end = best_start + max_clip

    log.info("selected window: %.2f -> %.2f (duration=%.2fs)", best_start, best_end, best_end - best_start)
    return (best_start, best_end)
