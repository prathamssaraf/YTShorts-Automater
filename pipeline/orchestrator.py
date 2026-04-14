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
from .data.context_builder import MatchContext, build_context, keywords_for_news
from .data.news_collector import fetch_news
from .data.scorecard_collector import fetch_scorecard
from .intelligence.decision_maker import ShortPlan, pick_video, plan_shorts
from .logging.run_logger import RunLogger
from .upload.youtube_uploader import UploadError, upload
from .video.downloader import download
from .video.editor import assemble
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
    parser.add_argument("--count", type=int, default=1,
                        help="Produce N distinct Shorts from this match (each featuring a different player).")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="Do not clean workspace when done.")
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


def _produce_one_short(
    *,
    match_id: str,
    scorecard: dict[str, Any],
    plan: ShortPlan,
    dry_run: bool,
) -> dict[str, Any]:
    """Run everything *after* data-collection/planning for one ShortPlan."""
    runlog = RunLogger()
    try:
        runlog.set("match", {
            "id": match_id,
            "description": scorecard.get("match", {}).get("description", ""),
            "result": scorecard.get("match", {}).get("result", ""),
        })
        runlog.update("decision", {
            "featured_player": plan.featured_player,
            "featured_moment": plan.featured_moment,
            "tone": plan.tone,
            "llm_reasoning": plan.llm_reasoning,
            "chosen_title": plan.chosen_title,
        })

        # YouTube search
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
            raise RuntimeError("no YouTube candidates found")

        plan = pick_video(plan, candidates)
        chosen = next(
            (c for c in candidates if c["video_id"] == plan.chosen_video_id),
            candidates[0],
        )
        runlog.set("video_source", {
            "video_id": chosen["video_id"],
            "title": chosen.get("title"),
            "channel": chosen.get("channel"),
            "license": chosen.get("license"),
            "views": chosen.get("views"),
        })

        # Download
        runlog.start_stage("download")
        source_path = download(plan.chosen_video_id, plan.fallback_video_ids)
        runlog.end_stage("download")

        # Scene select
        runlog.start_stage("scene_detect")
        try:
            start, end = select_window(source_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("scene detection failed: %s — using first 55s", exc)
            start, end = 0.0, 55.0
        runlog.end_stage("scene_detect")

        # Transcription
        runlog.start_stage("transcription")
        srt_text, _ = transcribe(source_path, start, end - start)
        srt_path = _write_srt_file(runlog.run_id, srt_text)
        runlog.end_stage("transcription")

        # Music
        runlog.start_stage("music_generation")
        music_path = generate_music(plan.music_prompt, runlog.run_id)
        runlog.end_stage("music_generation")

        # Overlay
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

        # Edit
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

        # Upload
        runlog.start_stage("upload")
        if dry_run:
            upload(
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
                runlog.set("output", {
                    "upload_error": str(exc),
                    "local_path": str(final_path),
                    "title_used": plan.chosen_title,
                })
                raise
        runlog.end_stage("upload")

        runlog.succeed()
        return {
            "status": "success",
            "player": plan.featured_player,
            "local_path": str(final_path),
            "run_id": runlog.run_id,
        }

    except Exception as exc:  # noqa: BLE001
        runlog.fail(exc)
        return {
            "status": "failed",
            "player": plan.featured_player,
            "error": str(exc),
            "run_id": runlog.run_id,
        }
    finally:
        runlog.finish()


def process_match(
    match: dict[str, Any],
    *,
    dry_run: bool = False,
    count: int = 1,
    keep_workspace: bool = False,
) -> list[dict[str, Any]]:
    """Run the full pipeline for one match, producing up to `count` distinct Shorts."""
    match_id = str(match.get("match_id") or match.get("id") or "")

    # Dup-skip only fires when producing a single Short — multi-short invocations
    # intentionally let the user produce additional Shorts for the same match.
    if count <= 1 and RunLogger().has_processed_match(match_id):
        log.info("match %s already has a successful Short — skipping (use --count N to override).",
                 match_id)
        return [{"status": "skipped_duplicate", "match_id": match_id}]

    # --- Data collection (once for the whole match)
    log.info("collecting match data for %s", match_id)
    scorecard = fetch_scorecard(match_id)
    keywords = keywords_for_news(scorecard)
    news = fetch_news(keywords=keywords)
    ctx: MatchContext = build_context(scorecard, news)

    # --- Plan N distinct Shorts
    log.info("planning up to %d Short(s)", count)
    plans = plan_shorts(ctx, n=count)
    if not plans:
        log.warning("no plans produced — nothing to do.")
        return []

    # --- Produce each Short (search + download + edit + upload per plan)
    results: list[dict[str, Any]] = []
    any_success = False
    for i, plan in enumerate(plans, 1):
        log.info("=" * 60)
        log.info("Short %d/%d — player: %s", i, len(plans), plan.featured_player)
        log.info("=" * 60)
        result = _produce_one_short(
            match_id=match_id,
            scorecard=scorecard,
            plan=plan,
            dry_run=dry_run,
        )
        results.append(result)
        if result.get("status") == "success":
            any_success = True

    _cleanup_workspace(any_success, force_keep=keep_workspace)
    return results


def _safe_process(match: dict[str, Any], *, dry_run: bool, count: int) -> None:
    try:
        process_match(match, dry_run=dry_run, count=count)
    except Exception as exc:  # noqa: BLE001
        log.error("match %s failed: %s", match.get("match_id"), exc)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    log.info("repo root: %s", REPO_ROOT)

    if args.watch:
        from .trigger import watch
        watch(lambda m: _safe_process(m, dry_run=args.dry_run, count=args.count))
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
        results = process_match(
            match,
            dry_run=args.dry_run,
            count=args.count,
            keep_workspace=args.keep_workspace,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline run failed: %s", exc)
        return 1

    successes = [r for r in results if r.get("status") == "success"]
    failures = [r for r in results if r.get("status") == "failed"]
    log.info("=" * 60)
    log.info("DONE: %d succeeded, %d failed (%d total)",
             len(successes), len(failures), len(results))
    for r in successes:
        log.info("  ✓ %s → %s", r.get("player"), r.get("local_path"))
    for r in failures:
        log.info("  ✗ %s — %s", r.get("player"), r.get("error"))
    return 0 if successes else 1


if __name__ == "__main__":
    sys.exit(main())
