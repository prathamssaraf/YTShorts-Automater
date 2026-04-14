"""Loads and validates settings.yaml. One place for config access."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import REPO_ROOT

_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
_cache: dict[str, Any] | None = None


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Return the parsed settings dict, caching on first load."""
    global _cache
    if _cache is not None and path is None:
        return _cache
    target = Path(path) if path else _SETTINGS_PATH
    if not target.exists():
        raise FileNotFoundError(f"Settings file not found: {target}")
    with target.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if path is None:
        _cache = data
    return data


def resolve_path(value: str | Path) -> Path:
    """Resolve a config path relative to REPO_ROOT if not absolute."""
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)
