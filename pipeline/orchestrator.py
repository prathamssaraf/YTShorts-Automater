"""Story/History Shorts orchestrator. The single entry point `run.sh` invokes."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from . import REPO_ROOT
from .audio.music import fetch_music
from .audio.tts import concat_wavs, probe_duration, synthesize
from .config import load_settings, resolve_path
from .intelligence.script_writer import StoryScript, write_script
from .logging.run_logger import RunLogger
from .topic_source import resolve_topic
from .upload.youtube_uploader import UploadError, upload
from .video.composer import compose
from .video.transcriber import transcribe
from .visual.manager import NoVisualProvider, VisualManager

log = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipeline.orchestrator")
    parser.add_argument("--topic", help="Topic to make a Short about. Empty = today-in-history.")
    parser.add_argument("--dry-run", action="store_true", help="Skip YouTube upload.")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="Don't clean intermediate files when done.")
    return parser.parse_args(argv)


def _cleanup_workspace(success: bool, force_keep: bool = False) -> None:
    if force_keep:
        return
    cfg = load_settings()["logging"]
    keep_on_fail = cfg.get("keep_workspace_on_failure", True)
    keep_on_ok = cfg.get("keep_workspace_on_success", False)
    keep = keep_on_ok if success else keep_on_fail
    if keep:
        return
    for sub in ("scenes", "clips", "audio"):
        path = resolve_path(f"workspace/{sub}")
        if path.exists():
            for child in path.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except OSError:
                    pass


def _generate_scenes(script: StoryScript, run_id: str) -> list[Path]:
    """Generate one video clip per scene via the visual provider cascade."""
    cfg = load_settings()["visual"]
    scenes_dir = resolve_path(cfg.get("scene_dir", "workspace/scenes")) / run_id
    scenes_dir.mkdir(parents=True, exist_ok=True)
    style = load_settings()["script"].get("visual_style", "")
    manager = VisualManager()
    log.info("visual providers active: %s", manager.configured_names() or "NONE")

    paths: list[Path] = []
    for i, scene in enumerate(script.scenes, 1):
        out = scenes_dir / f"scene_{i:02d}.mp4"
        prompt = scene.visual_prompt
        if style and style.lower() not in prompt.lower():
            prompt = f"{prompt}. Style: {style}."
        log.info("=== visual %d/%d ===", i, len(script.scenes))
        path = manager.generate(prompt=prompt, duration_seconds=scene.duration_seconds, out_path=out)
        paths.append(path)
    return paths


def _generate_narration(script: StoryScript, run_id: str) -> tuple[list[Path], Path]:
    cfg = load_settings()["tts"]
    audio_dir = resolve_path(cfg.get("output_dir", "workspace/audio")) / run_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    per_scene: list[Path] = []
    for i, scene in enumerate(script.scenes, 1):
        out = audio_dir / f"scene_{i:02d}.wav"
        log.info("=== TTS %d/%d ===", i, len(script.scenes))
        synthesize(scene.narration, out)
        per_scene.append(out)
    full = audio_dir / "narration.wav"
    concat_wavs(per_scene, full)
    log.info("narration assembled: %s (%.1fs)", full, probe_duration(full))
    return per_scene, full


def process_topic(topic: str | None, *, dry_run: bool = False, keep_workspace: bool = False) -> dict[str, Any]:
    runlog = RunLogger()
    try:
        # Stage 1 — topic + grounding
        runlog.start_stage("topic_resolution")
        resolved = resolve_topic(topic)
        runlog.set("match", {"topic": resolved["topic"]})  # reuse 'match' field for legacy log shape
        runlog.end_stage("topic_resolution")

        # Stage 2 — script
        runlog.start_stage("script_writing")
        script = write_script(resolved["topic"], resolved["grounding"])
        runlog.update("decision", {
            "title": script.title,
            "scene_count": len(script.scenes),
            "music_mood": script.music_mood,
        })
        runlog.end_stage("script_writing")

        # Stage 3 — narration TTS
        runlog.start_stage("tts")
        scene_audios, narration_full = _generate_narration(script, runlog.run_id)
        runlog.end_stage("tts")

        # Stage 4 — visual generation (per scene)
        runlog.start_stage("visual_generation")
        scene_videos = _generate_scenes(script, runlog.run_id)
        runlog.set("video_source", {"scene_count": len(scene_videos)})
        runlog.end_stage("visual_generation")

        # Stage 5 — subtitles via whisper.cpp on the full narration
        runlog.start_stage("transcription")
        srt_text, _ = transcribe(narration_full, 0.0, probe_duration(narration_full))
        srt_path = None
        if srt_text:
            srt_path = resolve_path(f"workspace/clips/{runlog.run_id}.srt")
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            srt_path.write_text(srt_text, encoding="utf-8")
        runlog.end_stage("transcription")

        # Stage 6 — music
        runlog.start_stage("music")
        music_path = fetch_music(script.music_mood, probe_duration(narration_full), runlog.run_id)
        runlog.end_stage("music")

        # Stage 7 — compose
        runlog.start_stage("compose")
        final_path = compose(
            run_id=runlog.run_id,
            script=script,
            scene_videos=scene_videos,
            scene_audios=scene_audios,
            narration_audio=narration_full,
            music_audio=music_path,
            srt_path=srt_path,
        )
        runlog.end_stage("compose")

        # Stage 8 — upload
        runlog.start_stage("upload")
        if dry_run:
            upload(video_path=final_path, title=script.title,
                   description=script.description, tags=script.tags, dry_run=True)
            runlog.set("output", {"dry_run": True, "local_path": str(final_path),
                                  "title_used": script.title})
        else:
            try:
                result = upload(video_path=final_path, title=script.title,
                                description=script.description, tags=script.tags, dry_run=False)
                runlog.set("output", {
                    "youtube_video_id": result.get("video_id"),
                    "youtube_url": result.get("url"),
                    "title_used": script.title,
                    "local_path": str(final_path),
                })
            except UploadError as exc:
                runlog.set("output", {"upload_error": str(exc),
                                      "local_path": str(final_path),
                                      "title_used": script.title})
                raise
        runlog.end_stage("upload")

        runlog.succeed()
        return {"status": "success", "topic": resolved["topic"],
                "local_path": str(final_path), "run_id": runlog.run_id}

    except Exception as exc:  # noqa: BLE001
        runlog.fail(exc)
        raise
    finally:
        success = runlog._record.get("status") == "success"
        runlog.finish()
        _cleanup_workspace(success, force_keep=keep_workspace)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    log.info("repo root: %s", REPO_ROOT)
    try:
        result = process_topic(args.topic, dry_run=args.dry_run,
                               keep_workspace=args.keep_workspace)
    except NoVisualProvider as exc:
        log.error("%s", exc)
        log.error(
            "👉 Set Kling credentials: edit config/settings.yaml under visual.kling, "
            "or set env vars KLING_ACCESS_KEY + KLING_SECRET_KEY (run.sh forwards them)."
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline failed: %s", exc)
        return 1
    log.info("=" * 60)
    log.info("DONE: %s — %s", result["status"], result.get("local_path"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
