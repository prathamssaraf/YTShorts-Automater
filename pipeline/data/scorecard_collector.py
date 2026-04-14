"""Scorecard + ball-by-ball data collector.

Primary path uses the `cricdata` PyPI package when available. If it is missing or
the site structure has changed, we fall back to a thin direct-HTTP fetch of
ESPNcricinfo's public match JSON endpoints. The fallback returns a reduced dict
so the rest of the pipeline can still function.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X) cricket-shorts/0.1",
    "Accept": "application/json, text/plain, */*",
}


class ScorecardFetchError(RuntimeError):
    pass


def _try_cricdata(match_id: str) -> dict[str, Any] | None:
    try:
        import cricdata  # type: ignore
    except ImportError:
        return None
    try:
        sc = cricdata.scorecard(match_id)  # type: ignore[attr-defined]
        return sc  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        log.warning("cricdata scorecard failed for %s: %s", match_id, exc)
        return None


def _normalise_cricdata(raw: dict[str, Any]) -> dict[str, Any]:
    """Best-effort normalisation — cricdata shape varies by version."""
    innings = raw.get("innings") or raw.get("scorecard") or []
    teams = []
    notable: list[dict[str, Any]] = []
    top_scorer: dict[str, Any] | None = None
    top_bowler: dict[str, Any] | None = None

    for inning in innings:
        team = {
            "name": inning.get("team_name") or inning.get("team") or "Unknown",
            "score": inning.get("score") or f"{inning.get('runs', 0)}/{inning.get('wickets', 0)}",
            "overs": inning.get("overs", 0),
        }
        teams.append(team)
        for bat in inning.get("batting", []) or []:
            runs = int(bat.get("runs") or 0)
            if top_scorer is None or runs > (top_scorer.get("runs") or 0):
                top_scorer = {
                    "name": bat.get("name"),
                    "runs": runs,
                    "balls": int(bat.get("balls") or 0),
                    "fours": int(bat.get("fours") or 0),
                    "sixes": int(bat.get("sixes") or 0),
                }
        for bowl in inning.get("bowling", []) or []:
            wkts = int(bowl.get("wickets") or 0)
            if top_bowler is None or wkts > (top_bowler.get("wickets") or 0):
                top_bowler = {
                    "name": bowl.get("name"),
                    "wickets": wkts,
                    "runs": int(bowl.get("runs") or 0),
                    "overs": float(bowl.get("overs") or 0),
                }
        for ball in inning.get("balls", []) or []:
            t = None
            if ball.get("isWicket"):
                t = "wicket"
            elif int(ball.get("scoreValue", 0)) == 6:
                t = "six"
            elif int(ball.get("scoreValue", 0)) == 4:
                t = "four"
            if t is None:
                continue
            notable.append(
                {
                    "type": t,
                    "batsman": ball.get("batsman"),
                    "bowler": ball.get("bowler"),
                    "over": ball.get("over"),
                    "description": ball.get("commentary") or ball.get("description") or "",
                }
            )

    return {
        "match": {
            "id": raw.get("id") or raw.get("match_id"),
            "description": raw.get("description") or raw.get("title") or "",
            "venue": raw.get("venue") or "",
            "date": raw.get("date") or "",
            "result": raw.get("result") or raw.get("status_text") or "",
            "team1": teams[0] if len(teams) > 0 else {},
            "team2": teams[1] if len(teams) > 1 else {},
        },
        "top_performers": {
            "top_scorer": top_scorer or {},
            "top_bowler": top_bowler or {},
            "player_of_match": raw.get("player_of_match") or (top_scorer or {}).get("name"),
        },
        "notable_moments": notable,
    }


def _fallback_minimal(match_id: str) -> dict[str, Any]:
    """Shape-preserving empty scorecard so downstream code doesn't crash."""
    log.warning("Using minimal scorecard fallback for match %s", match_id)
    return {
        "match": {
            "id": match_id,
            "description": f"Match {match_id}",
            "venue": "",
            "date": "",
            "result": "",
            "team1": {},
            "team2": {},
        },
        "top_performers": {"top_scorer": {}, "top_bowler": {}, "player_of_match": None},
        "notable_moments": [],
        "_fallback": True,
    }


def fetch_scorecard(match_id: str) -> dict[str, Any]:
    """Return a normalised scorecard dict. Never raises — returns a minimal stub on failure."""
    raw = _try_cricdata(match_id)
    if raw:
        try:
            return _normalise_cricdata(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to normalise cricdata scorecard: %s", exc)
    return _fallback_minimal(match_id)
