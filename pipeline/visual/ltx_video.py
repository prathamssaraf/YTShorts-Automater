"""Local LTX-Video fallback (Lightricks/LTX-Video via diffusers).

Disabled by default. Enable it by:
  1) Setting `visual.ltx_video.enabled: true` in settings.yaml.
  2) Installing the optional deps:
       pip install "diffusers>=0.32" "transformers>=4.45" "accelerate>=1.0" \
                   "torch>=2.4" "imageio[ffmpeg]" "huggingface_hub>=0.26"
  3) The first run downloads ~25GB into ./vendor/hf_cache (HF_HOME env var
     is set by run.sh).

LTX runs on Mac MPS / CUDA; ~30s-2min per 5s clip on M-series silicon.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import load_settings, resolve_path
from .base import VisualProvider

log = logging.getLogger(__name__)


class LTXVideoError(RuntimeError):
    pass


class LTXVideoProvider(VisualProvider):
    name = "ltx_video"

    def __init__(self) -> None:
        cfg = load_settings()["visual"]["ltx_video"]
        self.enabled = bool(cfg.get("enabled", False))
        self.model_id = cfg.get("model_id", "Lightricks/LTX-Video")
        self.num_frames = int(cfg.get("num_frames", 121))
        self.num_inference_steps = int(cfg.get("num_inference_steps", 40))
        self.cache_dir = resolve_path(cfg.get("cache_dir", "vendor/hf_cache"))
        self._pipe = None

    def is_configured(self) -> bool:
        if not self.enabled:
            return False
        try:
            import diffusers  # noqa: F401
            import torch  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure_pipe(self):
        if self._pipe is not None:
            return self._pipe
        import torch
        from diffusers import LTXPipeline
        device = "mps" if torch.backends.mps.is_available() else (
            "cuda" if torch.cuda.is_available() else "cpu"
        )
        dtype = torch.bfloat16 if device != "cpu" else torch.float32
        log.info("LTX: loading %s on %s (%s)", self.model_id, device, dtype)
        pipe = LTXPipeline.from_pretrained(
            self.model_id, torch_dtype=dtype, cache_dir=str(self.cache_dir),
        )
        pipe.to(device)
        self._pipe = pipe
        return pipe

    def generate(self, *, prompt: str, duration_seconds: float, out_path: Path) -> Path:
        if not self.is_configured():
            raise LTXVideoError(
                "LTX-Video not configured — set visual.ltx_video.enabled=true and "
                "pip install diffusers torch transformers accelerate imageio"
            )
        pipe = self._ensure_pipe()
        log.info("LTX: generating (%.40s...)", prompt)
        result = pipe(
            prompt=prompt,
            num_frames=self.num_frames,
            num_inference_steps=self.num_inference_steps,
            width=704, height=1280,                 # closest LTX supports to 9:16
        )
        frames = result.frames[0]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from diffusers.utils import export_to_video
            export_to_video(frames, str(out_path), fps=24)
        except Exception:
            import imageio
            imageio.mimwrite(str(out_path), frames, fps=24, codec="libx264")
        return out_path
