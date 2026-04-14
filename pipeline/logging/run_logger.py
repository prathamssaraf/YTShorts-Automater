"""Per-run structured logger. One JSON object per line in logs/runs.jsonl."""
from __future__ import annotations

import json
import logging as stdlib_logging
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import load_settings, resolve_path

stdlib_logging.basicConfig(
    level=stdlib_logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = stdlib_logging.getLogger("pipeline")


class RunLogger:
    """Accumulates structured data about a single pipeline run and writes it at finish()."""

    def __init__(self) -> None:
        settings = load_settings()
        self._path = resolve_path(settings["logging"]["log_file"])
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = str(uuid.uuid4())
        self._start_ts = time.time()
        self._record: dict[str, Any] = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "match": None,
            "decision": None,
            "video_source": None,
            "output": None,
            "status": "running",
            "error": None,
            "stage_timings": {},
        }
        self._stage_starts: dict[str, float] = {}

    def set(self, key: str, value: Any) -> None:
        self._record[key] = value

    def update(self, key: str, value: dict[str, Any]) -> None:
        current = self._record.get(key) or {}
        if not isinstance(current, dict):
            current = {}
        current.update(value)
        self._record[key] = current

    def start_stage(self, name: str) -> None:
        self._stage_starts[name] = time.time()
        log.info("stage %s: start", name)

    def end_stage(self, name: str) -> None:
        started = self._stage_starts.pop(name, None)
        if started is None:
            return
        elapsed = round(time.time() - started, 2)
        self._record["stage_timings"][name] = elapsed
        log.info("stage %s: done in %.2fs", name, elapsed)

    def fail(self, exc: BaseException) -> None:
        self._record["status"] = "failed"
        self._record["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        log.error("pipeline failed: %s", exc)

    def succeed(self) -> None:
        self._record["status"] = "success"

    def finish(self) -> Path:
        self._record["total_duration_seconds"] = round(time.time() - self._start_ts, 2)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(self._record, default=str) + "\n")
        log.info("run %s recorded (%s)", self.run_id, self._record["status"])
        return self._path

    def has_processed_match(self, match_id: str) -> bool:
        """Scan the run log to see whether a match has already been processed successfully."""
        if not self._path.exists():
            return False
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    match = rec.get("match") or {}
                    if str(match.get("id")) == str(match_id) and rec.get("status") == "success":
                        return True
        except OSError:
            return False
        return False
