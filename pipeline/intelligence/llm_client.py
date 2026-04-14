"""Thin wrapper around the local Ollama HTTP API."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from ..config import load_settings

log = logging.getLogger(__name__)


class OllamaUnavailable(RuntimeError):
    pass


class OllamaClient:
    def __init__(self) -> None:
        cfg = load_settings()["llm"]
        self.host = cfg["host"].rstrip("/")
        self.model = cfg["model"]
        self.temperature = float(cfg.get("temperature", 0.3))
        self.max_tokens = int(cfg.get("max_tokens", 1000))
        self.timeout = int(cfg.get("request_timeout_seconds", 120))
        self.json_retries = int(cfg.get("json_retries", 3))

    def is_available(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _generate(self, prompt: str, *, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = requests.post(f"{self.host}/api/generate", json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OllamaUnavailable(
                f"Cannot reach Ollama at {self.host}. Start it with 'ollama serve'."
            ) from exc
        if r.status_code != 200:
            raise OllamaUnavailable(f"Ollama returned HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        return data.get("response", "")

    def complete(self, prompt: str) -> str:
        return self._generate(prompt, json_mode=False)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        """Ask the model for JSON. Retry on malformed output, each retry nudges the prompt."""
        last_err: Exception | None = None
        for attempt in range(1, self.json_retries + 1):
            suffix = "" if attempt == 1 else "\n\nYour previous reply was not valid JSON. Reply with ONLY a JSON object — no prose, no code fences."
            raw = self._generate(prompt + suffix, json_mode=True)
            parsed = _extract_json(raw)
            if parsed is not None:
                return parsed
            last_err = ValueError(f"Non-JSON response on attempt {attempt}: {raw[:200]}")
            log.warning("LLM JSON parse failed (attempt %d/%d)", attempt, self.json_retries)
        raise ValueError(f"LLM failed to return valid JSON after {self.json_retries} attempts: {last_err}")


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    # Fast path.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Attempt to pluck the first JSON object out of prose.
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
