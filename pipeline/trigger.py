"""Match-end trigger. Polls for a match transitioning to 'result' state."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from .config import load_settings, resolve_path

log = logging.getLogger(__name__)


def _load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _fetch_matches(series_id: str) -> list[dict[str, Any]]:
    try:
        import cricdata  # type: ignore
    except ImportError:
        log.warning("cricdata not installed — trigger has no matches to check.")
        return []
    try:
        return list(cricdata.series_matches(series_id))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.warning("cricdata series_matches failed: %s", exc)
        return []


def _is_completed(status: str | None) -> bool:
    if not status:
        return False
    s = status.lower()
    return any(k in s for k in ("result", "complete", "won by", "match drawn", "match abandon"))


def find_newly_completed() -> list[dict[str, Any]]:
    """Return matches that completed since the last poll. Stateful."""
    cfg = load_settings()
    state_path = resolve_path(cfg["logging"]["state_file"])
    state = _load_state(state_path)
    matches = _fetch_matches(cfg["schedule"]["ipl_series_id"])

    completed_now: list[dict[str, Any]] = []
    for m in matches:
        mid = str(m.get("id") or m.get("match_id") or "")
        if not mid:
            continue
        status = m.get("status") or m.get("status_text") or ""
        prev = state.get(mid, "")
        if _is_completed(status) and not _is_completed(prev):
            completed_now.append({
                "match_id": mid,
                "match_slug": m.get("slug") or "",
                "series_slug": cfg["schedule"]["ipl_series_id"],
                "team1": m.get("team1") or {},
                "team2": m.get("team2") or {},
                "venue": m.get("venue") or "",
                "match_date": m.get("date") or "",
            })
        state[mid] = status
    _save_state(state_path, state)
    return completed_now


def watch(callback: Callable[[dict[str, Any]], None]) -> None:
    """Infinite polling loop. Fires `callback` for each newly-completed match."""
    cfg = load_settings()["schedule"]
    interval = int(cfg.get("check_interval_minutes", 30)) * 60
    log.info("watcher started (interval=%ds)", interval)
    while True:
        try:
            for match in find_newly_completed():
                log.info("match newly completed: %s", match["match_id"])
                callback(match)
        except Exception as exc:  # noqa: BLE001
            log.error("watcher tick failed: %s", exc)
        time.sleep(interval)
