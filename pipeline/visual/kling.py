"""Kling 3.0 / 1.6 text-to-video provider.

Uses the documented REST endpoint at https://api-singapore.klingai.com.
Auth is JWT(HS256) signed with the user's secret key, with the access key
as `iss`. We submit a job, then poll for completion (~1-3 min typical).

Free tier: 66 credits/day refresh on app.klingai.com — roughly 6 × 5s std-mode
clips per day.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import jwt
import requests

from ..config import load_settings
from .base import VisualProvider

log = logging.getLogger(__name__)


class KlingError(RuntimeError):
    pass


def _jwt_token(access_key: str, secret_key: str) -> str:
    headers = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": access_key,
        "exp": int(time.time()) + 1800,
        "nbf": int(time.time()) - 5,
    }
    return jwt.encode(payload, secret_key, headers=headers)


class KlingProvider(VisualProvider):
    name = "kling"

    def __init__(self) -> None:
        cfg = load_settings()["visual"]
        kc = cfg["kling"]
        self.enabled = bool(kc.get("enabled", True))
        self.access_key = kc.get("access_key", "") or ""
        self.secret_key = kc.get("secret_key", "") or ""
        self.model_name = kc.get("model_name", "kling-v1-6")
        self.mode = kc.get("mode", "std")
        self.duration = str(kc.get("duration_seconds", 5))
        self.aspect_ratio = cfg.get("aspect_ratio", "9:16")
        self.base_url = kc.get("base_url", "https://api-singapore.klingai.com").rstrip("/")
        self.poll_interval = int(kc.get("poll_interval_seconds", 10))
        self.poll_timeout = int(kc.get("poll_timeout_seconds", 600))

    def is_configured(self) -> bool:
        return self.enabled and bool(self.access_key) and bool(self.secret_key)

    def _auth_headers(self) -> dict[str, str]:
        token = _jwt_token(self.access_key, self.secret_key)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _submit(self, prompt: str) -> str:
        body: dict[str, Any] = {
            "model_name": self.model_name,
            "prompt": prompt,
            "mode": self.mode,
            "duration": self.duration,
            "aspect_ratio": self.aspect_ratio,
        }
        url = f"{self.base_url}/v1/videos/text2video"
        try:
            r = requests.post(url, headers=self._auth_headers(), json=body, timeout=30)
        except requests.RequestException as exc:
            raise KlingError(f"submit network error: {exc}") from exc
        if r.status_code != 200:
            raise KlingError(f"submit HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("code") != 0:
            raise KlingError(f"submit returned code={body.get('code')}: {body.get('message')}")
        task_id = (body.get("data") or {}).get("task_id")
        if not task_id:
            raise KlingError(f"no task_id in response: {body}")
        return task_id

    def _poll(self, task_id: str) -> str:
        url = f"{self.base_url}/v1/videos/text2video/{task_id}"
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            try:
                r = requests.get(url, headers=self._auth_headers(), timeout=30)
            except requests.RequestException as exc:
                log.warning("poll network hiccup: %s — retrying", exc)
                time.sleep(self.poll_interval)
                continue
            if r.status_code != 200:
                raise KlingError(f"poll HTTP {r.status_code}: {r.text[:300]}")
            body = r.json()
            data = body.get("data") or {}
            status = data.get("task_status")
            if status == "succeed":
                videos = (data.get("task_result") or {}).get("videos") or []
                if not videos or not videos[0].get("url"):
                    raise KlingError(f"succeed but no video URL: {body}")
                return videos[0]["url"]
            if status == "failed":
                raise KlingError(f"task failed: {data.get('task_status_msg')}")
            log.info("Kling task %s status=%s — waiting %ds", task_id, status, self.poll_interval)
            time.sleep(self.poll_interval)
        raise KlingError(f"poll timed out after {self.poll_timeout}s")

    def _download(self, url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with out_path.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        return out_path

    def generate(self, *, prompt: str, duration_seconds: float, out_path: Path) -> Path:
        if not self.is_configured():
            raise KlingError("Kling not configured (set visual.kling.access_key + secret_key)")
        log.info("Kling: submitting prompt (%.40s...)", prompt)
        task_id = self._submit(prompt)
        log.info("Kling: task_id=%s — polling", task_id)
        url = self._poll(task_id)
        log.info("Kling: task succeeded — downloading %s", url[:80])
        return self._download(url, out_path)
