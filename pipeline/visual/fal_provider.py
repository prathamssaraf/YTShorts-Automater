"""fal.ai video generation provider.

Supports multiple models through one API key: Seedance 2.0 (default),
Kling 1.6, Wan, LTX-Video, etc. Async submit + poll under the hood via
the fal-client SDK.

Auth: FAL_KEY env var (set in .env or exported). Format: "<key_id>:<key_secret>".
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from ..config import load_settings
from .base import VisualProvider

log = logging.getLogger(__name__)


class FalError(RuntimeError):
    pass


class FalProvider(VisualProvider):
    name = "fal"

    def __init__(self) -> None:
        cfg = load_settings()["visual"]
        fc = cfg.get("fal") or {}
        self.enabled = bool(fc.get("enabled", True))
        self.api_key = fc.get("api_key") or os.environ.get("FAL_KEY") or ""
        self.model = fc.get("model", "bytedance/seedance-2.0/text-to-video")
        self.duration = str(fc.get("duration_seconds", 5))
        self.resolution = fc.get("resolution", "720p")
        self.aspect_ratio = cfg.get("aspect_ratio", "9:16")
        self.poll_interval = int(fc.get("poll_interval_seconds", 5))
        self.poll_timeout = int(fc.get("poll_timeout_seconds", 600))

    def is_configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }

    def _submit(self, prompt: str) -> str:
        """Submit a generation job and return the request_id for polling."""
        url = f"https://queue.fal.run/{self.model}"
        body: dict[str, Any] = {
            "prompt": prompt,
            "duration": self.duration,
            "aspect_ratio": self.aspect_ratio,
        }
        if self.resolution:
            body["resolution"] = self.resolution
        try:
            r = requests.post(url, headers=self._headers(), json=body, timeout=30)
        except requests.RequestException as exc:
            raise FalError(f"fal submit error: {exc}") from exc
        if r.status_code not in (200, 201):
            raise FalError(f"fal submit HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        req_id = data.get("request_id")
        if not req_id:
            raise FalError(f"no request_id in fal response: {data}")
        return req_id

    def _poll(self, request_id: str) -> str:
        """Poll until the job is done and return the video URL."""
        status_url = f"https://queue.fal.run/{self.model}/requests/{request_id}/status"
        result_url = f"https://queue.fal.run/{self.model}/requests/{request_id}"
        deadline = time.time() + self.poll_timeout

        while time.time() < deadline:
            try:
                r = requests.get(status_url, headers=self._headers(), timeout=30)
            except requests.RequestException as exc:
                log.warning("fal poll hiccup: %s", exc)
                time.sleep(self.poll_interval)
                continue
            if r.status_code != 200:
                raise FalError(f"fal status HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            status = data.get("status")

            if status == "COMPLETED":
                # Fetch the result
                try:
                    rr = requests.get(result_url, headers=self._headers(), timeout=30)
                    rr.raise_for_status()
                    result = rr.json()
                except Exception as exc:
                    raise FalError(f"fal result fetch failed: {exc}") from exc
                video = result.get("video") or {}
                video_url = video.get("url") if isinstance(video, dict) else None
                if not video_url:
                    # Some models return different shapes
                    video_url = result.get("video_url") or result.get("url")
                if not video_url:
                    raise FalError(f"fal completed but no video URL: {result}")
                return video_url

            if status in ("FAILED", "CANCELLED"):
                raise FalError(f"fal task {status}: {data}")

            log.info("fal %s status=%s — waiting %ds", request_id[:12], status, self.poll_interval)
            time.sleep(self.poll_interval)

        raise FalError(f"fal poll timed out after {self.poll_timeout}s")

    def _download(self, url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with out_path.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        if out_path.stat().st_size < 10_000:
            raise FalError(f"downloaded file too small ({out_path.stat().st_size}b): {url}")
        return out_path

    def generate(self, *, prompt: str, duration_seconds: float, out_path: Path) -> Path:
        if not self.is_configured():
            raise FalError("fal.ai not configured (set FAL_KEY in .env)")
        log.info("fal [%s]: submitting (%.50s...)", self.model.split("/")[-1], prompt)
        request_id = self._submit(prompt)
        log.info("fal: request_id=%s — polling", request_id[:16])
        video_url = self._poll(request_id)
        log.info("fal: done — downloading %s", video_url[:80])
        return self._download(video_url, out_path)
