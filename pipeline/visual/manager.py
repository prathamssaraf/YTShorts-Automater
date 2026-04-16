"""Tries each configured visual provider in order until one succeeds."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import load_settings
from .base import VisualProvider
from .kling import KlingProvider
from .ltx_video import LTXVideoProvider

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[VisualProvider]] = {
    "kling": KlingProvider,
    "ltx_video": LTXVideoProvider,
}


class NoVisualProvider(RuntimeError):
    pass


class VisualManager:
    def __init__(self) -> None:
        cfg = load_settings()["visual"]
        order = cfg.get("providers") or ["kling", "ltx_video"]
        self._providers: list[VisualProvider] = []
        for name in order:
            cls = _REGISTRY.get(name)
            if not cls:
                log.warning("unknown visual provider: %s", name)
                continue
            try:
                self._providers.append(cls())
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to init provider %s: %s", name, exc)
        if not self._providers:
            raise NoVisualProvider("no visual providers initialised")

    def configured_names(self) -> list[str]:
        return [p.name for p in self._providers if p.is_configured()]

    def generate(self, *, prompt: str, duration_seconds: float, out_path: Path) -> Path:
        last_err: Exception | None = None
        for provider in self._providers:
            if not provider.is_configured():
                log.info("provider %s not configured — skipping", provider.name)
                continue
            try:
                return provider.generate(
                    prompt=prompt, duration_seconds=duration_seconds, out_path=out_path,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("provider %s failed: %s — trying next", provider.name, exc)
                last_err = exc
        raise NoVisualProvider(
            f"all visual providers failed (last error: {last_err}). "
            f"Configured: {self.configured_names() or 'NONE'}. "
            "At minimum set visual.kling.access_key + secret_key in settings.yaml."
        )
