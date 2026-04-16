"""LLM prompt templates for story/history Shorts. All prompts expect JSON output."""
from __future__ import annotations

ROLE = (
    "You are an expert short-form documentary writer. You craft concise, factually "
    "accurate, emotionally engaging 30-60 second narration scripts that hook viewers "
    "in the first 3 seconds and pay them off with a memorable closing line."
)


SCRIPT_PROMPT = """{role}

TOPIC: {topic}

GROUNDING NOTES (treat as the only source of truth — do not invent facts beyond these):
{grounding}

CONSTRAINTS:
- Total spoken duration: ~{total_seconds} seconds (~{word_budget} words at {wpm} wpm)
- Number of scenes: {scenes_min}-{scenes_max}
- Each scene is ONE complete sentence of narration + ONE descriptive visual prompt
- Visual prompts should be vivid, cinematic, and consistent in style: "{visual_style}"
- Open with a hook (question, surprising fact, or in-medias-res action) — never start with "Today we'll talk about..."
- Close with a memorable line, lesson, or thought-provoking question
- Title under 60 characters, must mention the topic
- Description under 300 characters
- 8-15 lowercase hashtags without '#'
- Music mood: short keyword phrase (e.g. "epic orchestral", "melancholic piano", "tense thriller")

Return ONLY a JSON object:

{{
  "title": "<string>",
  "description": "<string>",
  "tags": ["<tag1>", "..."],
  "music_mood": "<short keyword phrase>",
  "thumbnail_text": "<2-4 word punchy headline for an optional title card>",
  "scenes": [
    {{
      "narration": "<one sentence; must be speakable in ~{seconds_per_scene}s>",
      "visual_prompt": "<one detailed cinematic description of the shot>",
      "duration_seconds": {seconds_per_scene}
    }}
  ]
}}
"""


def render_script_prompt(
    *,
    topic: str,
    grounding: str,
    total_seconds: int,
    scenes_min: int,
    scenes_max: int,
    seconds_per_scene: int,
    visual_style: str,
    wpm: int,
) -> str:
    word_budget = int(total_seconds * wpm / 60)
    return SCRIPT_PROMPT.format(
        role=ROLE,
        topic=topic,
        grounding=grounding or "(no external grounding available — rely on widely-known historical facts and be conservative)",
        total_seconds=total_seconds,
        word_budget=word_budget,
        scenes_min=scenes_min,
        scenes_max=scenes_max,
        seconds_per_scene=seconds_per_scene,
        visual_style=visual_style,
        wpm=wpm,
    )
