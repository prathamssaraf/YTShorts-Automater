"""Loads settings.yaml + overlays env-var secrets."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from . import REPO_ROOT

_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
_cache: dict[str, Any] | None = None


def _apply_env_overrides(data: dict[str, Any]) -> None:
    """Pull secrets from env vars so users can `export KEY=...` instead of editing yaml."""
    visual = data.setdefault("visual", {})
    kling = visual.setdefault("kling", {})
    if os.environ.get("KLING_ACCESS_KEY"):
        kling["access_key"] = os.environ["KLING_ACCESS_KEY"]
    if os.environ.get("KLING_SECRET_KEY"):
        kling["secret_key"] = os.environ["KLING_SECRET_KEY"]
    fal = visual.setdefault("fal", {})
    if os.environ.get("FAL_KEY"):
        fal["api_key"] = os.environ["FAL_KEY"]
    music = data.setdefault("music", {})
    if os.environ.get("PIXABAY_API_KEY"):
        music["pixabay_api_key"] = os.environ["PIXABAY_API_KEY"]


def load_settings(path: Path | None = None) -> dict[str, Any]:
    global _cache
    if _cache is not None and path is None:
        return _cache
    target = Path(path) if path else _SETTINGS_PATH
    if not target.exists():
        raise FileNotFoundError(f"Settings file not found: {target}")
    with target.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    _apply_env_overrides(data)
    if path is None:
        _cache = data
    return data


def resolve_path(value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)
