"""Free-tier YouTube candidate search. No API key, no Google Cloud quota.

Two signals are merged:

  1. **Channel RSS** — YouTube exposes a public Atom feed for every channel at
     `https://www.youtube.com/feeds/videos.xml?channel_id=<ID>`. We poll each
     `youtube.preferred_channels` feed (ICC, IPL, Cricbuzz, …) and keep entries
     whose title matches the search keywords. Gives us fresh, trusted, official
     clips at zero quota cost.

  2. **yt-dlp search** — `ytsearch<N>:<query>` hits YouTube's public search
     surface via yt-dlp and returns full-text matches across the platform.
     Covers the long tail (fan highlights, commentary channels, etc.) that the
     official feeds miss. Flat-mode extraction returns duration + view count in
     a single call, so we don't need a second round-trip per candidate.

Results are merged, deduplicated by video id, enriched for any entries still
missing metadata, filtered by min-views and max-duration, and ranked:
preferred channel > creative-commons license > views descending.

Disk cache (`workspace/youtube_search_cache.json`) keeps repeated dev runs fast
and reduces load on YouTube's servers.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import feedparser

from ..config import load_settings, resolve_path

log = logging.getLogger(__name__)


class YouTubeSearchError(RuntimeError):
    """Raised when no search backend produces any usable results."""


# Aliases kept so callers written against the old API-key-based module keep working.
YouTubeAPIError = YouTubeSearchError
YouTubeQuotaExceeded = YouTubeSearchError


_CHANNEL_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_VID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/)([0-9A-Za-z_-]{11})")


def _extract_video_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _VID_RE.search(value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", value):
        return value
    return None


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _query_keywords(query: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2]


class YouTubeSearcher:
    def __init__(self) -> None:
        cfg = load_settings()
        self.yt_cfg = cfg["youtube"]
        self.cache_path = resolve_path(self.yt_cfg["cache_file"])
        self.cache_ttl = int(self.yt_cfg.get("cache_ttl_seconds", 7200))
        self._cache = _load_cache(self.cache_path)
        self._ydl_flat = None
        self._ydl_full = None

    # ---- yt-dlp handles ----

    def _flat_ydl(self):
        if self._ydl_flat is None:
            import yt_dlp
            self._ydl_flat = yt_dlp.YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
            })
        return self._ydl_flat

    def _full_ydl(self):
        if self._ydl_full is None:
            import yt_dlp
            self._ydl_full = yt_dlp.YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "skip_download": True,
                "ignoreerrors": True,
            })
        return self._ydl_full

    # ---- cache ----

    def _cache_get(self, key: str) -> Any:
        entry = self._cache.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] > self.cache_ttl:
            return None
        return entry["data"]

    def _cache_put(self, key: str, data: Any) -> None:
        self._cache[key] = {"ts": time.time(), "data": data}
        _save_cache(self.cache_path, self._cache)

    # ---- source 1: channel RSS ----

    def _from_channel_rss(self, channel_id: str, keywords: list[str]) -> list[dict[str, Any]]:
        key = f"rss:{channel_id}"
        all_entries = self._cache_get(key)
        if all_entries is None:
            try:
                parsed = feedparser.parse(_CHANNEL_FEED.format(channel_id=channel_id))
            except Exception as exc:  # noqa: BLE001
                log.warning("RSS fetch failed for %s: %s", channel_id, exc)
                return []
            all_entries = []
            for entry in parsed.entries or []:
                vid = _extract_video_id(entry.get("yt_videoid") or entry.get("link", ""))
                if not vid:
                    continue
                all_entries.append({
                    "video_id": vid,
                    "title": entry.get("title", ""),
                    "channel": entry.get("author", ""),
                    "channel_id": channel_id,
                    "published": entry.get("published", ""),
                    "source": "rss",
                })
            self._cache_put(key, all_entries)

        if not keywords:
            return all_entries
        return [e for e in all_entries if any(k in e["title"].lower() for k in keywords)]

    # ---- source 2: yt-dlp search ----

    def _from_ytdlp_search(self, query: str, n: int = 20) -> list[dict[str, Any]]:
        key = f"ytsearch:{query}|{n}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            info = self._flat_ydl().extract_info(f"ytsearch{n}:{query}", download=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("yt-dlp search failed for %r: %s", query, exc)
            return []
        items: list[dict[str, Any]] = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            vid = entry.get("id") or _extract_video_id(entry.get("url", ""))
            if not vid:
                continue
            items.append({
                "video_id": vid,
                "title": entry.get("title") or "",
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "channel_id": entry.get("channel_id") or "",
                "published": entry.get("upload_date") or "",
                "duration_seconds": int(entry.get("duration") or 0),
                "views": int(entry.get("view_count") or 0),
                "license": entry.get("license") or "youtube",
                "source": "ytsearch",
            })
        self._cache_put(key, items)
        return items

    # ---- enrichment for candidates missing metadata (mostly RSS hits) ----

    def _enrich(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for cand in candidates:
            if cand.get("duration_seconds") and cand.get("views") is not None:
                continue
            vid = cand["video_id"]
            cache_key = f"meta:{vid}"
            cached = self._cache_get(cache_key)
            if cached:
                cand.update(cached)
                continue
            try:
                info = self._full_ydl().extract_info(
                    f"https://www.youtube.com/watch?v={vid}", download=False
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("enrich failed for %s: %s", vid, exc)
                info = None
            if info:
                meta = {
                    "duration_seconds": int(info.get("duration") or 0),
                    "views": int(info.get("view_count") or 0),
                    "license": info.get("license") or "youtube",
                    "channel_id": cand.get("channel_id") or info.get("channel_id") or "",
                    "channel": cand.get("channel") or info.get("channel") or info.get("uploader") or "",
                }
                self._cache_put(cache_key, meta)
                cand.update(meta)
        return candidates

    # ---- public API ----

    def search_candidates(
        self, query: str, query_hints: list[str] | None = None
    ) -> list[dict[str, Any]]:
        keywords = _query_keywords(query)
        merged: dict[str, dict[str, Any]] = {}

        # Source 1: preferred-channel RSS feeds (cheap, trusted)
        for ch in self.yt_cfg.get("preferred_channels", []) or []:
            for item in self._from_channel_rss(ch, keywords):
                merged.setdefault(item["video_id"], item)

        # Source 2: yt-dlp full-platform search (primary + up to 2 hints)
        queries = [query] + [h for h in (query_hints or []) if h][:2]
        for q in queries:
            for item in self._from_ytdlp_search(q):
                existing = merged.get(item["video_id"])
                if existing is None:
                    merged[item["video_id"]] = item
                else:
                    # Merge: prefer RSS channel_id (authoritative) but adopt the rest
                    rss_channel = existing.get("channel_id")
                    merged[item["video_id"]] = {**existing, **item, "channel_id": rss_channel or item.get("channel_id")}

        if not merged:
            return []

        enriched = self._enrich(list(merged.values()))

        min_views = int(self.yt_cfg.get("min_views", 0))
        max_dur = int(self.yt_cfg.get("max_duration_seconds", 300))
        filtered = [
            v for v in enriched
            if (v.get("views") or 0) >= min_views
            and 0 < (v.get("duration_seconds") or 0) <= max_dur
        ]

        preferred = set(self.yt_cfg.get("preferred_channels", []) or [])
        filtered.sort(
            key=lambda v: (
                v.get("channel_id") in preferred,
                v.get("license") == "creativeCommon",
                v.get("views") or 0,
            ),
            reverse=True,
        )
        limit = int(self.yt_cfg.get("search_results_to_evaluate", 8))
        log.info(
            "youtube_search: %d unique candidates (%d after filters), returning top %d",
            len(merged), len(filtered), min(limit, len(filtered)),
        )
        return filtered[:limit]
