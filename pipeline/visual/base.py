"""Visual provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class VisualProvider(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True iff this provider has the credentials/models it needs."""

    @abstractmethod
    def generate(self, *, prompt: str, duration_seconds: float, out_path: Path) -> Path:
        """Generate a video matching `prompt`, save to `out_path`, return that path.

        Should raise an exception on failure so the manager can fall through.
        """
