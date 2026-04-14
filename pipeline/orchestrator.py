"""End-to-end orchestrator. The one entry point `run.sh` invokes."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import REPO_ROOT
from .config import load_settings, resolve_path
from .data.context_builder import build_context, keywords_for_news
from .data.news_collector import fetch_news
from .data.scorecard_collector import fetch_scorecard
from .intelligence.decision_maker import pick_video, plan_short
from .logging.run_logger import RunLogger
from .upload.youtube_uploader import UploadError, upload
from .video.downloader import VideoUnavailableError, download
from .video.editor import EditorError, assemble
from .video.music_generator import generate_music
from .video.overlay_renderer import compose_player_stat, render_overlay
from .video.scene_detector import select_window
from .video.transcriber import transcribe
from .video.youtube_search import YouTubeAPIError, YouTubeQuotaExceeded, YouTubeSearcher

log = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipeline.orchestrator")
    parser.add_argument("--match-id", help="Run for a specific match id.")
    parser.add_argument("--watch", action="store_true", help="Run the polling trigger loop.")
    parser.add_argument("--dry-run", action="store_true", help="Skip YouTube upload.")
    parser.add_argument("--keep-workspace", action="store_true", help="Do not clean workspace on success.")
    return parser.parse_args(argv)


def _cleanup_workspace(success: bool) -> None:
    cfg = load_settings()["logging"]
    keep_on_fail = cfg.get("keep_workspace_on_failure", True)
    keep_on_ok = cfg.get("keep_workspace_on_success", False)
    keep = keep_on_ok if success else keep_on_fail
    if keep:
        return
    for sub in ("downloads", "clips", "audio"):
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


def _write_srt_file(run_id: str, srt_text: str) -> Path | None:
    if not srt_text:
        return None
    path = resolve_path(f"workspace/clips/{run_id}.srt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(srt_text, encoding="utf-8")
    return path


def process_match(match: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Run the full pipeline for a single match. Returns a summary dict."""
    runlog = RunLogger()
    match_id = str(match.get("match_id") or match.get("id") or "")
    try:
        if runlog.has_processed_match(match_id):
            log.info("match %s already processed successfully — skipping.", match_id)
            runlog.set("status", "skipped_duplicate")
            runlog.finish()
            return {"status": "skipped_duplicate", "match_id": match_id}

        # --- Stage 2: data
        runlog.start_stage("data_collection")
        scorecard = fetch_scorecard(match_id)
        keywords = keywords_for_news(scorecard)
        news = fetch_news(keywords=keywords)
        ctx = build_context(scorecard, news)
        runlog.set("match", {
            "id": match_id,
            "description": scorecard.get("match", {}).get("description", ""),
            "result": scorecard.get("match", {}).get("result", ""),
        })
        runlog.end_stage("data_collection")

        # --- Stage 3: decision (part 1)
        runlog.start_stage("llm_decision")
        plan = plan_short(ctx)
        runlog.update("decision", {
            "featured_player": plan.featured_player,
            "featured_moment": plan.featured_moment,
            "tone": plan.tone,
            "llm_reasoning": plan.llm_reasoning,
            "chosen_title": plan.chosen_title,
        })
        runlog.end_stage("llm_decision")

        # --- Stage 4a: youtube search
        runlog.start_stage("youtube_search")
        searcher = YouTubeSearcher()
        query = f"{plan.featured_player} {plan.featured_moment.get('type','highlight')} IPL 2026"
        try:
            candidates = searcher.search_candidates(query, plan.search_query_hints)
        except (YouTubeAPIError, YouTubeQuotaExceeded) as exc:
            log.warning("YouTube search failed: %s", exc)
            candidates = []
        runlog.end_stage("youtube_search")

        if not candidates:
            raise RuntimeError("no YouTube candidates found (check api key / network)")

        # --- Stage 3: decision (part 2) — pick video
        plan = pick_video(plan, candidates)
        chosen = next((c for c in candidates if c["video_id"] == plan.chosen_video_id), candidates[0])
        runlog.set("video_source", {
            "video_id": chosen["video_id"],
            "title": chosen.get("title"),
            "channel": chosen.get("channel"),
            "license": chosen.get("license"),
            "views": chosen.get("views"),
        })

        # --- Stage 4b: download
        runlog.start_stage("download")
        source_path = download(plan.chosen_video_id, plan.fallback_video_ids)
        runlog.end_stage("download")

        # --- Stage 5: scene select
        runlog.start_stage("scene_detect")
        try:
            start, end = select_window(source_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("scene detection failed: %s — using first 55s", exc)
            start, end = 0.0, 55.0
        runlog.end_stage("scene_detect")

        # --- Stage 6: transcription
        runlog.start_stage("transcription")
        srt_text, _ = transcribe(source_path, start, end - start)
        srt_path = _write_srt_file(runlog.run_id, srt_text)
        runlog.end_stage("transcription")

        # --- Stage 7: music
        runlog.start_stage("music_generation")
        music_path = generate_music(plan.music_prompt, runlog.run_id)
        runlog.end_stage("music_generation")

        # --- Stage 8: overlay
        runlog.start_stage("overlay_render")
        stat_str = compose_player_stat(scorecard, plan.featured_player)
        overlay_path = render_overlay(
            run_id=runlog.run_id,
            scorecard=scorecard,
            featured_player=plan.featured_player,
            featured_stat=stat_str,
            out_dir=resolve_path("workspace/clips"),
        )
        runlog.end_stage("overlay_render")

        # --- Stage 9: edit
        runlog.start_stage("editing")
        final_path = assemble(
            run_id=runlog.run_id,
            source_video=source_path,
            start=start, end=end,
            srt_path=srt_path,
            overlay_png=overlay_path,
            music_wav=music_path,
        )
        runlog.end_stage("editing")

        # --- Stage 10: upload
        runlog.start_stage("upload")
        if dry_run:
            result = upload(
                video_path=final_path,
                title=plan.chosen_title,
                description=plan.description,
                tags=plan.tags,
                dry_run=True,
            )
            runlog.set("output", {
                "dry_run": True,
                "local_path": str(final_path),
                "title_used": plan.chosen_title,
            })
        else:
            try:
                result = upload(
                    video_path=final_path,
                    title=plan.chosen_title,
                    description=plan.description,
                    tags=plan.tags,
                    dry_run=False,
                )
                runlog.set("output", {
                    "youtube_video_id": result.get("video_id"),
                    "youtube_url": result.get("url"),
                    "title_used": plan.chosen_title,
                    "local_path": str(final_path),
                })
            except UploadError as exc:
                # Partial success — keep the final file, mark run failed at upload stage only.
                preserved = resolve_path("workspace/output") / final_path.name
                runlog.set("output", {
                    "upload_error": str(exc),
                    "local_path": str(preserved),
                    "title_used": plan.chosen_title,
                })
                raise
        runlog.end_stage("upload")

        runlog.succeed()
        return {"status": "success", "plan": asdict(plan), "match_id": match_id}

    except Exception as exc:  # noqa: BLE001
        runlog.fail(exc)
        raise
    finally:
        success = runlog._record.get("status") == "success"
        runlog.finish()
        _cleanup_workspace(success)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    log.info("repo root: %s", REPO_ROOT)

    if args.watch:
        from .trigger import watch
        watch(lambda m: _safe_process(m, dry_run=args.dry_run))
        return 0

    if args.match_id:
        match = {"match_id": args.match_id}
    else:
        from .trigger import find_newly_completed
        newly = find_newly_completed()
        if not newly:
            log.info("No newly completed matches and no --match-id given. Nothing to do.")
            return 0
        match = newly[0]

    try:
        process_match(match, dry_run=args.dry_run)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline run failed: %s", exc)
        return 1


def _safe_process(match: dict[str, Any], *, dry_run: bool) -> None:
    try:
        process_match(match, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log.error("match %s failed: %s", match.get("match_id"), exc)


if __name__ == "__main__":
    sys.exit(main())
