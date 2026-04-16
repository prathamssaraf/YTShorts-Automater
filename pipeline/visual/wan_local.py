"""Wan 2.1 local text-to-video via mlx-video.

Runs entirely on-device using Apple MLX — no API key, no credits, no limits.
Uses the 1.3B model by default (~5GB, fast). Switch to 14B for higher quality
(~28GB, slower) in settings.yaml.

First run converts HuggingFace weights to MLX format (~5min + 5GB download).
Subsequent runs reuse the cached MLX model at vendor/wan21_mlx/.

Generation speed on Apple Silicon:
  - 1.3B @ 480p: ~2-5 min per 5s clip (M3/M4/M5)
  - 14B @ 480p:  ~10-20 min per 5s clip (needs 48GB+ unified memory)
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from ..config import load_settings, resolve_path
from .base import VisualProvider

log = logging.getLogger(__name__)


class WanLocalError(RuntimeError):
    pass


class WanLocalProvider(VisualProvider):
    name = "wan_local"

    def __init__(self) -> None:
        cfg = load_settings()["visual"]
        wc = cfg.get("wan_local") or {}
        self.enabled = bool(wc.get("enabled", True))
        self.hf_model = wc.get("hf_model", "Wan-AI/Wan2.1-T2V-1.3B")
        self.model_dir = resolve_path(wc.get("model_dir", "vendor/wan21_mlx"))
        self.width = int(wc.get("width", 480))
        self.height = int(wc.get("height", 848))
        self.fps = int(wc.get("fps", 16))
        self.num_inference_steps = int(wc.get("num_inference_steps", 30))
        self.guidance_scale = float(wc.get("guidance_scale", 5.0))
        self.generate_timeout = int(wc.get("generate_timeout_seconds", 900))

    def is_configured(self) -> bool:
        if not self.enabled:
            return False
        try:
            import mlx_video  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_model(self) -> None:
        marker = self.model_dir / "config.json"
        if marker.exists():
            return

        # Step 1: download HuggingFace weights
        hf_cache = resolve_path("vendor/wan21_hf")
        hf_marker = hf_cache / "config.json"
        if not hf_marker.exists():
            log.info("Wan 2.1: downloading %s (~5GB, one-time)", self.hf_model)
            hf_cache.mkdir(parents=True, exist_ok=True)
            dl_cmd = [
                sys.executable, "-c",
                f"from huggingface_hub import snapshot_download; "
                f"snapshot_download('{self.hf_model}', local_dir='{hf_cache}')",
            ]
            result = subprocess.run(dl_cmd, capture_output=True, text=True, check=False, timeout=3600)
            if result.returncode != 0:
                raise WanLocalError(f"model download failed: {result.stderr[-600:]}")
            if not hf_marker.exists():
                raise WanLocalError(f"download completed but {hf_marker} not found")
            log.info("Wan 2.1: download complete at %s", hf_cache)

        # Step 2: convert to MLX format
        log.info("Wan 2.1: converting to MLX format at %s", self.model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, "-m", "mlx_video.models.wan_2.convert",
            "--checkpoint-dir", str(hf_cache),
            "--output-dir", str(self.model_dir),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1800)
        if result.returncode != 0:
            raise WanLocalError(f"model conversion failed: {result.stderr[-600:]}")
        if not marker.exists():
            raise WanLocalError(f"conversion ran but {marker} not found — check stderr above")
        log.info("Wan 2.1: model ready at %s", self.model_dir)

    def generate(self, *, prompt: str, duration_seconds: float, out_path: Path) -> Path:
        if not self.is_configured():
            raise WanLocalError(
                "mlx-video not installed. Run: pip install git+https://github.com/Blaizzy/mlx-video.git"
            )
        self._ensure_model()

        num_frames = max(17, int(duration_seconds * self.fps))
        # Round to nearest valid frame count (wan wants multiples of 4 + 1)
        num_frames = ((num_frames - 1) // 4) * 4 + 1

        out_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable, "-m", "mlx_video.models.wan_2.generate",
            "--model-dir", str(self.model_dir),
            "--prompt", prompt,
            "--num-frames", str(num_frames),
            "--width", str(self.width),
            "--height", str(self.height),
            "--fps", str(self.fps),
            "--steps", str(self.num_inference_steps),
            "--guide-scale", str(self.guidance_scale),
            "--output", str(out_path),
            "--seed", "42",
        ]

        log.info(
            "Wan 2.1: generating %d frames @ %dx%d (%.1fs, %d steps) — this may take a few minutes",
            num_frames, self.width, self.height, duration_seconds, self.num_inference_steps,
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=self.generate_timeout,
        )
        if result.returncode != 0:
            raise WanLocalError(f"generation failed: {result.stderr[-600:]}")

        # mlx-video may append .mp4 or use the exact path — check both
        if out_path.exists() and out_path.stat().st_size > 10_000:
            return out_path
        mp4_path = out_path.with_suffix(".mp4")
        if mp4_path.exists() and mp4_path.stat().st_size > 10_000:
            mp4_path.rename(out_path)
            return out_path

        raise WanLocalError(f"generation completed but output not found at {out_path}")
