"""Uses the LLM (or a rule-based fallback) to decide what Short to make and which clip to use."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..data.context_builder import MatchContext
from . import prompts
from .llm_client import OllamaClient, OllamaUnavailable

log = logging.getLogger(__name__)


@dataclass
class ShortPlan:
    featured_player: str
    featured_moment: dict[str, Any]
    tone: str
    titles: list[str]
    chosen_title: str
    description: str
    tags: list[str]
    music_prompt: str
    llm_reasoning: str
    search_query_hints: list[str] = field(default_factory=list)
    chosen_video_id: str | None = None
    fallback_video_ids: list[str] = field(default_factory=list)


def _clamp_title(title: str, player: str) -> str:
    if player and player.lower() not in title.lower():
        title = f"{player} - {title}"
    if "IPL 2026" not in title:
        title = f"{title} | IPL 2026"
    return title[:95]


def _rule_based_plan(ctx: MatchContext) -> ShortPlan:
    """LLM-free fallback — still produces a usable plan."""
    player = (ctx.top_performers.get("player_of_match")
              or (ctx.top_performers.get("top_scorer") or {}).get("name")
              or "Top Performer")
    moment = ctx.top_moments[0] if ctx.top_moments else {
        "type": "highlight",
        "batsman": player,
        "description": "Top performance",
    }
    stat_bits = []
    scorer = ctx.top_performers.get("top_scorer") or {}
    if scorer.get("name") == player and scorer.get("runs"):
        stat_bits.append(f"{scorer['runs']} off {scorer.get('balls','?')}")
    stat_str = " | ".join(stat_bits) if stat_bits else "Must-see moment"
    base_title = f"{player} {stat_str}!"
    titles = [
        _clamp_title(base_title, player),
        _clamp_title(f"{player} Goes OFF 🔥", player),
        _clamp_title(f"{player} Unstoppable!", player),
    ]
    return ShortPlan(
        featured_player=player,
        featured_moment=moment,
        tone="energetic",
        titles=titles,
        chosen_title=titles[0],
        description=f"{player} lighting up IPL 2026. {ctx.match_summary[:200]}",
        tags=["IPL2026", "Cricket", "Shorts", (player or "").replace(" ", ""), "IPL", "Highlights"],
        music_prompt="high energy edm",
        llm_reasoning="LLM unavailable — fell back to top-performer rule.",
        search_query_hints=[f"{player} highlights IPL 2026"],
    )


def _coerce_plan_from_llm(ctx: MatchContext, data: dict[str, Any]) -> ShortPlan:
    idx = int(data.get("featured_moment_index", -1))
    featured_moment = (
        ctx.top_moments[idx] if 0 <= idx < len(ctx.top_moments)
        else {"type": "highlight", "description": data.get("featured_moment_summary", "")}
    )
    titles = [str(t) for t in (data.get("titles") or [])][:3] or ["IPL 2026 Highlight"]
    player = str(data.get("featured_player") or "Top Performer")
    chosen = _clamp_title(str(data.get("chosen_title") or titles[0]), player)
    tags = [str(t) for t in (data.get("tags") or [])][:15]
    return ShortPlan(
        featured_player=player,
        featured_moment=featured_moment,
        tone=str(data.get("tone") or "energetic"),
        titles=[_clamp_title(t, player) for t in titles],
        chosen_title=chosen,
        description=str(data.get("description") or "")[:300],
        tags=tags,
        music_prompt=str(data.get("music_prompt") or "high energy edm"),
        llm_reasoning=str(data.get("reasoning") or ""),
        search_query_hints=[str(h) for h in (data.get("search_query_hints") or [])],
    )


def plan_short(ctx: MatchContext) -> ShortPlan:
    """Decision 1: pick the moment + produce title/description/tags/music prompt."""
    client = OllamaClient()
    if not client.is_available():
        log.warning("Ollama unavailable — using rule-based ShortPlan fallback.")
        return _rule_based_plan(ctx)
    prompt = prompts.render_moment_and_metadata(ctx.to_llm_dict())
    try:
        data = client.complete_json(prompt)
    except (OllamaUnavailable, ValueError) as exc:
        log.warning("LLM decision failed (%s). Falling back to rules.", exc)
        return _rule_based_plan(ctx)
    return _coerce_plan_from_llm(ctx, data)


def pick_video(plan: ShortPlan, candidates: list[dict[str, Any]]) -> ShortPlan:
    """Decision 2: choose which YouTube candidate to download. Mutates + returns plan."""
    if not candidates:
        return plan
    client = OllamaClient()
    if not client.is_available():
        # Rule-based: sort by views desc and prefer CC.
        ranked = sorted(
            candidates,
            key=lambda c: (c.get("license") == "creativeCommon", c.get("views") or 0),
            reverse=True,
        )
        plan.chosen_video_id = ranked[0]["video_id"]
        plan.fallback_video_ids = [c["video_id"] for c in ranked[1:4]]
        return plan

    compact = [
        {
            "video_id": c.get("video_id"),
            "title": c.get("title"),
            "channel": c.get("channel"),
            "views": c.get("views"),
            "duration": c.get("duration_seconds"),
            "license": c.get("license"),
        }
        for c in candidates
    ]
    prompt = prompts.render_video_selection(
        plan.featured_player,
        plan.featured_moment.get("description") or plan.featured_moment.get("type", ""),
        compact,
    )
    try:
        data = client.complete_json(prompt)
    except (OllamaUnavailable, ValueError) as exc:
        log.warning("LLM video selection failed (%s). Using top candidate.", exc)
        plan.chosen_video_id = candidates[0]["video_id"]
        plan.fallback_video_ids = [c["video_id"] for c in candidates[1:4]]
        return plan

    chosen = str(data.get("chosen_video_id") or "")
    known_ids = {c["video_id"] for c in candidates}
    if chosen not in known_ids:
        chosen = candidates[0]["video_id"]
    fallbacks = [v for v in (data.get("fallback_video_ids") or []) if v in known_ids and v != chosen]
    plan.chosen_video_id = chosen
    plan.fallback_video_ids = fallbacks[:3]
    plan.llm_reasoning = (plan.llm_reasoning + "\n\n[video pick] " + str(data.get("reasoning") or "")).strip()
    return plan
