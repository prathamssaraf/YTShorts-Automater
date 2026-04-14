"""RSS-based news collector for ESPNcricinfo + Cricbuzz."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import feedparser

from ..config import load_settings

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    return html.unescape(_TAG_RE.sub("", raw)).strip()


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tup = getattr(entry, key, None) or entry.get(key)
        if tup:
            try:
                return datetime(*tup[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _mentions_any(text: str, needles: Iterable[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles if n)


def _title_similarity(a: str, b: str) -> float:
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / max(len(set_a), len(set_b))


def fetch_news(keywords: list[str] | None = None) -> list[dict[str, Any]]:
    """Fetch recent cricket headlines filtered by keywords (team/player names)."""
    cfg = load_settings()["data"]
    recency = timedelta(hours=cfg.get("news_recency_hours", 6))
    cutoff = datetime.now(timezone.utc) - recency
    max_items = cfg["news_max_articles"]

    feeds = [
        ("ESPNcricinfo", cfg["espncricinfo_rss"]),
        ("Cricbuzz", cfg["cricbuzz_rss"]),
    ]

    collected: list[dict[str, Any]] = []
    for source, url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("feedparser failed for %s: %s", url, exc)
            continue
        for entry in parsed.entries or []:
            published = _entry_time(entry)
            if published and published < cutoff:
                continue
            headline = _strip_html(entry.get("title"))
            summary = _strip_html(entry.get("summary"))
            if keywords and not _mentions_any(f"{headline} {summary}", keywords):
                continue
            collected.append(
                {
                    "headline": headline,
                    "source": source,
                    "published": published.isoformat() if published else None,
                    "summary": summary[:600],
                    "url": entry.get("link"),
                }
            )

    # Deduplicate by title similarity.
    deduped: list[dict[str, Any]] = []
    for item in sorted(collected, key=lambda x: x.get("published") or "", reverse=True):
        if any(_title_similarity(item["headline"], kept["headline"]) > 0.8 for kept in deduped):
            continue
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped
