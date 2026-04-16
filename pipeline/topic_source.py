"""Topic acquisition: explicit CLI input or Wikipedia 'on this day'."""
from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Any

import requests

from .config import load_settings

log = logging.getLogger(__name__)

_USER_AGENT = "YTShorts-Automater/1.0 (https://github.com/prathamssaraf/YTShorts-Automater)"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}


def fetch_wikipedia_summary(topic: str, lang: str = "en") -> str:
    """Return the lead paragraph of the Wikipedia article for `topic`, or empty string."""
    if not topic:
        return ""
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(topic)}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=10)
    except requests.RequestException as exc:
        log.warning("Wikipedia summary fetch failed for %r: %s", topic, exc)
        return ""
    if r.status_code != 200:
        log.info("Wikipedia returned %d for %r", r.status_code, topic)
        return ""
    try:
        data = r.json()
    except ValueError:
        return ""
    return data.get("extract") or ""


def today_in_history(lang: str = "en") -> dict[str, Any] | None:
    """Pick a random notable event from Wikipedia's 'On this day'."""
    today = datetime.utcnow()
    url = (
        f"https://api.wikimedia.org/feed/v1/wikipedia/{lang}/onthisday/events/"
        f"{today.month:02d}/{today.day:02d}"
    )
    try:
        r = requests.get(url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        events = r.json().get("events") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("Today-in-history fetch failed: %s", exc)
        return None
    if not events:
        return None
    # Prefer events with a clear lead text + a linked page
    candidates = [e for e in events if e.get("text") and e.get("pages")]
    if not candidates:
        candidates = events
    pick = random.choice(candidates[:25])
    pages = pick.get("pages") or []
    title = pages[0].get("titles", {}).get("normalized") if pages else ""
    return {
        "topic": title or pick.get("text", "")[:80],
        "year": pick.get("year"),
        "summary": pick.get("text", ""),
        "wikipedia_url": (pages[0].get("content_urls", {}).get("desktop", {}).get("page")
                          if pages else None),
    }


def resolve_topic(cli_topic: str | None) -> dict[str, Any]:
    """Return {'topic', 'grounding'} — from CLI, settings, or today-in-history fallback."""
    cfg = load_settings()["topic"]
    chosen = (cli_topic or cfg.get("default_topic") or "").strip()
    grounding = ""

    if not chosen:
        log.info("No topic given — picking from today-in-history.")
        ev = today_in_history(cfg.get("language", "en"))
        if not ev:
            raise RuntimeError(
                "No topic provided and today-in-history lookup failed. "
                "Pass --topic '<your topic>' explicitly."
            )
        chosen = ev["topic"]
        grounding = ev.get("summary", "")

    if cfg.get("use_wikipedia_grounding") and chosen:
        wiki = fetch_wikipedia_summary(chosen, cfg.get("language", "en"))
        if wiki:
            grounding = (grounding + "\n\n" + wiki).strip() if grounding else wiki

    log.info("topic resolved: %r (grounding: %d chars)", chosen, len(grounding))
    return {"topic": chosen, "grounding": grounding}
