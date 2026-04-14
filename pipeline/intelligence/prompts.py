"""LLM prompt templates. All prompts expect JSON output."""
from __future__ import annotations

import json
from typing import Any

ROLE = "You are an expert cricket content creator who produces viral YouTube Shorts for IPL fans."


MOMENT_AND_METADATA_PROMPT = """{role}

Analyse the following match context and decide what to feature in a 60-second YouTube Short.

MATCH CONTEXT (JSON):
{context_json}

CRITICAL GROUNDING RULES (read before writing anything):
- You may ONLY feature a player whose name appears in `top_performers`, `top_moments`, or `news_headlines`. Never use a name from general cricket knowledge that is not in this JSON.
- If the context has almost no data (empty top_moments AND empty top_performers), feature a player named in news_headlines. If none are named there either, set "featured_player" to "" and explain in "reasoning".
- Every numeric stat you quote in titles/description (runs, balls, wickets, overs) must match a number present in the context. Do not round up or embellish.
- If you are unsure, be conservative rather than clever.

SELECTION:
- Pick the single most Short-worthy moment from `top_moments`.
- If `top_moments` is empty, feature the player_of_match or top_scorer from `top_performers`.

FORMAT RULES:
- The Short centres on ONE player and ONE highlight.
- Titles under 60 characters; must contain the player name; must include "IPL 2026".
- Description under 300 characters.
- 10–15 lowercase hashtags without the '#' prefix.
- Music prompt: a short mood keyword phrase for MusicGen (e.g. "epic orchestral", "high energy edm", "tense dramatic").
- `search_query_hints`: 2 phrases you'd type into YouTube to find THIS EXACT moment — include team abbreviation and a distinctive stat (e.g. "Rutherford 71 off 31 RCB IPL 2026", "Rutherford 5 sixes final over").

Return ONLY a JSON object with this exact schema:

{{
  "featured_player": "<string — name taken verbatim from context>",
  "featured_moment_index": <int, 0-based index into top_moments, or -1 if none selected>,
  "featured_moment_summary": "<one-sentence description of the chosen moment, using only facts from context>",
  "tone": "<energetic | emotional | analytical | funny>",
  "reasoning": "<1-3 sentences explaining why this moment will perform>",
  "titles": ["<title1>", "<title2>", "<title3>"],
  "chosen_title": "<the best of the three>",
  "description": "<youtube description under 300 chars>",
  "tags": ["<tag1>", "<tag2>", "..."],
  "music_prompt": "<music mood keyword phrase>",
  "search_query_hints": ["<phrase 1>", "<phrase 2>"]
}}
"""


VIDEO_SELECTION_PROMPT = """{role}

We've already decided the Short will feature {player} — specifically: {moment_summary}

Here are candidate YouTube videos (JSON list). Pick the single best clip to source footage from, and rank the rest as fallbacks. Prefer: official channels, creative-commons licensed clips, exact-moment highlights, higher views, shorter length (ideally 30-180s).

CANDIDATES:
{candidates_json}

Return ONLY a JSON object with this schema:

{{
  "chosen_video_id": "<video id of best match>",
  "fallback_video_ids": ["<next best>", "<...>"],
  "reasoning": "<1-2 sentences>"
}}
"""


def render_moment_and_metadata(context: dict[str, Any]) -> str:
    return MOMENT_AND_METADATA_PROMPT.format(
        role=ROLE,
        context_json=json.dumps(context, indent=2, default=str),
    )


def render_video_selection(player: str, moment_summary: str, candidates: list[dict[str, Any]]) -> str:
    return VIDEO_SELECTION_PROMPT.format(
        role=ROLE,
        player=player,
        moment_summary=moment_summary,
        candidates_json=json.dumps(candidates, indent=2, default=str),
    )
