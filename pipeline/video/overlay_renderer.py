"""Render a transparent PNG overlay with score bar + player stat card."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..config import load_settings

log = logging.getLogger(__name__)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rounded_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                  radius: int, fill: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render_overlay(
    run_id: str,
    scorecard: dict[str, Any],
    featured_player: str,
    featured_stat: str,
    out_dir: Path,
) -> Path:
    cfg = load_settings()["video"]
    w, h = cfg["output_resolution"]
    opacity = float(cfg.get("overlay_opacity", 0.85))
    alpha = int(255 * opacity)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    m = scorecard.get("match", {}) or {}
    t1 = m.get("team1", {}) or {}
    t2 = m.get("team2", {}) or {}
    result = m.get("result", "")

    # --- Top score bar
    bar_h = 180
    _rounded_rect(draw, (40, 40, w - 40, 40 + bar_h), radius=24, fill=(10, 10, 10, alpha))
    font_team = _load_font(52)
    font_result = _load_font(34)

    line1 = f"{t1.get('name','Team 1')[:22]}  {t1.get('score','')}"
    line2 = f"{t2.get('name','Team 2')[:22]}  {t2.get('score','')}"
    draw.text((70, 58), line1, fill=(255, 255, 255, 255), font=font_team)
    draw.text((70, 115), line2, fill=(255, 255, 255, 255), font=font_team)
    if result:
        draw.text((70, 175), result[:60], fill=(255, 220, 90, 255), font=font_result)

    # --- Bottom-left player card
    card_w, card_h = 720, 210
    card_x, card_y = 50, h - card_h - 260
    _rounded_rect(
        draw,
        (card_x, card_y, card_x + card_w, card_y + card_h),
        radius=24,
        fill=(20, 20, 30, alpha),
    )
    font_player = _load_font(64)
    font_stat = _load_font(40)
    font_brand = _load_font(28)

    draw.text((card_x + 24, card_y + 18), featured_player[:22],
              fill=(255, 255, 255, 255), font=font_player)
    draw.text((card_x + 24, card_y + 100), featured_stat[:36],
              fill=(120, 255, 150, 255), font=font_stat)
    draw.text((card_x + 24, card_y + 160), "IPL 2026",
              fill=(200, 200, 200, 255), font=font_brand)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}_overlay.png"
    img.save(out_path, format="PNG")
    log.info("overlay PNG written: %s", out_path)
    return out_path


def compose_player_stat(scorecard: dict[str, Any], featured_player: str) -> str:
    perf = scorecard.get("top_performers", {}) or {}
    scorer = perf.get("top_scorer") or {}
    bowler = perf.get("top_bowler") or {}
    if scorer.get("name") == featured_player:
        bits = [f"{scorer.get('runs','?')} off {scorer.get('balls','?')}"]
        if scorer.get("fours"): bits.append(f"{scorer['fours']}x4")
        if scorer.get("sixes"): bits.append(f"{scorer['sixes']}x6")
        return " | ".join(bits)
    if bowler.get("name") == featured_player:
        return f"{bowler.get('wickets','?')}/{bowler.get('runs','?')} in {bowler.get('overs','?')} ov"
    return "Must-see moment"
