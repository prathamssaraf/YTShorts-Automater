"""Merges scorecard + news into a single MatchContext dataclass."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import load_settings

log = logging.getLogger(__name__)


@dataclass
class MatchContext:
    match_summary: str
    top_moments: list[dict[str, Any]] = field(default_factory=list)
    news_headlines: list[dict[str, Any]] = field(default_factory=list)
    top_performers: dict[str, Any] = field(default_factory=dict)
    raw_scorecard: dict[str, Any] = field(default_factory=dict)
    match_meta: dict[str, Any] = field(default_factory=dict)

    def to_llm_dict(self) -> dict[str, Any]:
        """Trim to what the LLM should see (keep prompt compact)."""
        return {
            "match_summary": self.match_summary,
            "top_moments": self.top_moments[:5],
            "news_headlines": [
                {"headline": n.get("headline"), "source": n.get("source")}
                for n in self.news_headlines[:6]
            ],
            "top_performers": self.top_performers,
        }


_TYPE_WEIGHT = {"six": 3, "wicket": 4, "four": 1, "dot_cluster": 1}


def _parse_over(over: Any) -> float:
    try:
        return float(over)
    except (TypeError, ValueError):
        return 0.0


def _moment_score(moment: dict[str, Any], marquee: set[str]) -> float:
    base = _TYPE_WEIGHT.get(moment.get("type", ""), 0)
    over = _parse_over(moment.get("over"))
    last_over_bonus = 3 if over >= 18.0 else (1 if over >= 15.0 else 0)
    player = (moment.get("batsman") or moment.get("bowler") or "").strip()
    star_bonus = 2 if player in marquee else 0
    return base + last_over_bonus + star_bonus


def _build_summary(scorecard: dict[str, Any]) -> str:
    m = scorecard.get("match", {})
    t1 = m.get("team1", {}) or {}
    t2 = m.get("team2", {}) or {}
    desc = m.get("description") or ""
    result = m.get("result") or ""
    venue = m.get("venue") or ""
    parts = [p for p in [desc, venue] if p]
    head = ". ".join(parts)
    if t1.get("name") and t2.get("name"):
        head += f". {t1['name']} {t1.get('score','')} vs {t2['name']} {t2.get('score','')}."
    if result:
        head += f" {result}."
    return head.strip()


def _attach_news(moments: list[dict[str, Any]], news: list[dict[str, Any]]) -> None:
    for moment in moments:
        player = (moment.get("batsman") or moment.get("bowler") or "").lower()
        if not player:
            continue
        related = [n for n in news if player in (n.get("headline", "") + " " + n.get("summary", "")).lower()]
        moment["related_news"] = [n["headline"] for n in related[:3]]


def build_context(scorecard: dict[str, Any], news: list[dict[str, Any]]) -> MatchContext:
    cfg = load_settings()
    marquee = set(cfg["data"].get("marquee_players") or [])
    moments_raw = scorecard.get("notable_moments") or []
    ranked = sorted(moments_raw, key=lambda m: _moment_score(m, marquee), reverse=True)[:5]
    _attach_news(ranked, news)

    ctx = MatchContext(
        match_summary=_build_summary(scorecard),
        top_moments=ranked,
        news_headlines=news,
        top_performers=scorecard.get("top_performers", {}) or {},
        raw_scorecard=scorecard,
        match_meta=scorecard.get("match", {}) or {},
    )
    log.info(
        "built context: %d moments, %d news; summary=%r",
        len(ranked),
        len(news),
        ctx.match_summary[:80],
    )
    return ctx


def keywords_for_news(scorecard: dict[str, Any]) -> list[str]:
    m = scorecard.get("match", {}) or {}
    names: list[str] = []
    for team in (m.get("team1") or {}, m.get("team2") or {}):
        if team.get("name"):
            names.append(team["name"])
    perf = scorecard.get("top_performers", {}) or {}
    for key in ("top_scorer", "top_bowler"):
        p = perf.get(key) or {}
        if p.get("name"):
            names.append(p["name"])
    if perf.get("player_of_match"):
        names.append(perf["player_of_match"])
    return [n for n in names if n]
