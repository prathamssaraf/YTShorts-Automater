"""Generate a multi-scene Shorts script from a topic + grounding."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import load_settings
from . import prompts
from .llm_client import OllamaClient, OllamaUnavailable

log = logging.getLogger(__name__)


@dataclass
class Scene:
    narration: str
    visual_prompt: str
    duration_seconds: float
    audio_path: str = ""        # filled by TTS stage
    video_path: str = ""        # filled by visual stage


@dataclass
class StoryScript:
    topic: str
    title: str
    description: str
    tags: list[str]
    music_mood: str
    thumbnail_text: str
    scenes: list[Scene] = field(default_factory=list)


def _coerce_scenes(raw_scenes: list[dict], default_seconds: int) -> list[Scene]:
    out: list[Scene] = []
    for s in raw_scenes:
        narration = (s.get("narration") or "").strip()
        visual = (s.get("visual_prompt") or "").strip()
        if not narration or not visual:
            continue
        try:
            dur = float(s.get("duration_seconds") or default_seconds)
        except (ValueError, TypeError):
            dur = float(default_seconds)
        out.append(Scene(narration=narration, visual_prompt=visual, duration_seconds=dur))
    return out


def _rule_based_script(topic: str, grounding: str) -> StoryScript:
    """LLM-free fallback. Produces a usable but bland 3-scene script."""
    summary = (grounding or topic)[:600]
    return StoryScript(
        topic=topic,
        title=f"{topic} | Story Time"[:60],
        description=summary[:280],
        tags=["history", "shorts", "story", "facts", "education", "viral"],
        music_mood="cinematic emotional",
        thumbnail_text=topic[:30],
        scenes=[
            Scene(
                narration=f"What if I told you everything you knew about {topic} was incomplete?",
                visual_prompt=f"Cinematic wide shot establishing {topic}, dramatic lighting, photorealistic, 35mm film",
                duration_seconds=5,
            ),
            Scene(
                narration=summary[:200] or f"Here is the story of {topic}.",
                visual_prompt=f"Mid shot of {topic} subject with shallow depth of field, dramatic lighting, photorealistic",
                duration_seconds=12,
            ),
            Scene(
                narration=f"And that is why {topic} still matters today.",
                visual_prompt=f"Slow zoom-out of {topic} scene, golden hour, cinematic",
                duration_seconds=5,
            ),
        ],
    )


def write_script(topic: str, grounding: str) -> StoryScript:
    cfg = load_settings()
    s_cfg = cfg["script"]
    total = int(s_cfg["total_duration_seconds"])
    scenes_min = int(s_cfg["scenes_min"])
    scenes_max = int(s_cfg["scenes_max"])
    seconds_per_scene = int(s_cfg["scene_duration_seconds"])
    visual_style = s_cfg["visual_style"]
    wpm = int(s_cfg.get("reading_speed_wpm", 155))

    client = OllamaClient()
    if not client.is_available():
        log.warning("Ollama unavailable — using rule-based script fallback.")
        return _rule_based_script(topic, grounding)

    prompt = prompts.render_script_prompt(
        topic=topic,
        grounding=grounding,
        total_seconds=total,
        scenes_min=scenes_min,
        scenes_max=scenes_max,
        seconds_per_scene=seconds_per_scene,
        visual_style=visual_style,
        wpm=wpm,
    )
    try:
        data = client.complete_json(prompt)
    except (OllamaUnavailable, ValueError) as exc:
        log.warning("LLM script gen failed (%s) — falling back to rules.", exc)
        return _rule_based_script(topic, grounding)

    scenes = _coerce_scenes(data.get("scenes") or [], default_seconds=seconds_per_scene)
    if not scenes:
        log.warning("LLM returned no usable scenes — falling back to rules.")
        return _rule_based_script(topic, grounding)

    script = StoryScript(
        topic=topic,
        title=str(data.get("title") or f"{topic} | Shorts")[:90],
        description=str(data.get("description") or "")[:300],
        tags=[str(t).lower() for t in (data.get("tags") or [])][:15],
        music_mood=str(data.get("music_mood") or "cinematic"),
        thumbnail_text=str(data.get("thumbnail_text") or topic)[:60],
        scenes=scenes,
    )
    log.info("script: %d scenes, title=%r", len(script.scenes), script.title)
    for i, sc in enumerate(script.scenes, 1):
        log.info("  scene %d (%.1fs): %s", i, sc.duration_seconds, sc.narration[:80])
    return script
