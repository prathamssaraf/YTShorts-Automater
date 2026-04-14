"""Scorecard + ball-by-ball collector.

Source of truth: ESPNcricinfo's legacy engine JSON —
    https://www.espncricinfo.com/ci/engine/match/<MATCH_ID>.json

The modern hs-consumer-api and the Next.js page HTML are both Akamai-WAF-
protected and return 403 to plain `requests` calls. The legacy /ci/engine/
endpoint is also WAF-protected BUT it accepts requests that present a real
browser TLS fingerprint, which we achieve via `curl_cffi`.

What this collector produces:
  - Team names, abbreviations, and innings scores (drives overlay + LLM context).
  - Match description, venue, date, natural-language result.
  - Top scorer / top bowler / player-of-the-match (extracted from live scorecard +
    fall-of-wickets partnerships + commentary post-text).
  - Notable moments (sixes, fours, wickets) extracted from the trailing commentary
    window the endpoint exposes — usually the final 4–6 overs, which is exactly
    the climax footage we want to feature.

On any failure, returns a minimal stub so downstream code doesn't crash; the LLM
will gracefully degrade to news-only context in that case.
"""
from __future__ import annotations

import logging
import re
from typing import Any

try:
    from curl_cffi import requests as _cc_requests  # type: ignore
    _HAS_CURL_CFFI = True
except ImportError:
    _cc_requests = None  # type: ignore
    _HAS_CURL_CFFI = False

log = logging.getLogger(__name__)


_URL = "https://www.espncricinfo.com/ci/engine/match/{mid}.json"
_IMPERSONATE = "chrome120"
_HEADERS = {"Accept": "application/json, text/plain, */*"}

_POM_RE = re.compile(r"<b>\s*([^,<]+?)\s*,\s*Player of the Match\b", re.IGNORECASE)
_RESULT_RE = re.compile(
    r"([A-Z]{2,5})\s+(?:have\s+)?won\s+by\s+\d+\s+(?:runs?|wickets?)",
    re.IGNORECASE,
)
_NAME_SPLIT_RE = re.compile(r"^(.+?)\s+to\s+(.+)$", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", raw)).strip()


def _minimal_fallback(match_id: str, reason: str) -> dict[str, Any]:
    log.warning("scorecard fallback for match %s: %s", match_id, reason)
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
        "_fallback_reason": reason,
    }


def fetch_scorecard(match_id: str) -> dict[str, Any]:
    """Return a normalised scorecard dict. Never raises — returns a stub on failure."""
    if not _HAS_CURL_CFFI:
        return _minimal_fallback(
            match_id,
            "curl_cffi not installed — pip install curl_cffi (auto-installed by run.sh)",
        )
    try:
        r = _cc_requests.get(
            _URL.format(mid=match_id),
            impersonate=_IMPERSONATE,
            timeout=15,
            headers=_HEADERS,
        )
    except Exception as exc:  # noqa: BLE001
        return _minimal_fallback(match_id, f"network error: {exc}")
    if r.status_code != 200:
        return _minimal_fallback(match_id, f"HTTP {r.status_code}")
    try:
        raw = r.json()
    except Exception as exc:  # noqa: BLE001
        return _minimal_fallback(match_id, f"bad JSON: {exc}")
    try:
        return _normalise(match_id, raw)
    except Exception as exc:  # noqa: BLE001
        log.exception("scorecard normalisation failed for %s: %s", match_id, exc)
        return _minimal_fallback(match_id, f"parse error: {exc}")


# ---------------------------------------------------------------------------
# Normalisation helpers

def _team_info(team_map: dict[str, dict], tid: Any) -> dict[str, Any]:
    t = team_map.get(str(tid), {})
    return {
        "id": t.get("team_id"),
        "name": t.get("team_name", ""),
        "abbr": t.get("team_abbreviation") or t.get("team_short_name", ""),
    }


def _innings_summary(inning: dict, team_map: dict) -> dict[str, Any]:
    info = _team_info(team_map, inning.get("batting_team_id"))
    runs = inning.get("runs")
    wickets = inning.get("wickets")
    score = f"{runs}/{wickets}" if runs is not None and wickets is not None else ""
    return {
        **info,
        "score": score,
        "overs": str(inning.get("overs", "")),
        "run_rate": inning.get("run_rate"),
    }


def _extract_result(comms: list, innings: list, team_map: dict) -> str:
    """Try commentary first; fall back to computing from innings totals."""
    for c in comms:
        for b in c.get("ball") or []:
            for field in ("text", "post_text"):
                m = _RESULT_RE.search(b.get(field) or "")
                if m:
                    return m.group(0).strip()
    if len(innings) >= 2:
        try:
            r1 = int(innings[0].get("runs", 0))
            r2 = int(innings[1].get("runs", 0))
            w2 = int(innings[1].get("wickets", 10))
            t1 = _team_info(team_map, innings[0].get("batting_team_id"))
            t2 = _team_info(team_map, innings[1].get("batting_team_id"))
            if r2 > r1:
                return f"{t2['abbr'] or t2['name']} won by {10 - w2} wickets"
            if r1 > r2:
                return f"{t1['abbr'] or t1['name']} won by {r1 - r2} runs"
            return "Match tied"
        except (ValueError, TypeError):
            pass
    return ""


def _extract_pom(comms: list) -> str | None:
    for c in comms:
        for b in c.get("ball") or []:
            for field in ("post_text", "text"):
                m = _POM_RE.search(b.get(field) or "")
                if m:
                    name = _strip_html(m.group(1))
                    if name:
                        return name
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _extract_top_performers(centre: dict, comms: list) -> tuple[dict, dict]:
    """Infer top scorer/bowler from live scorecard + FoW partnerships + comms."""
    batsmen: dict[str, dict[str, Any]] = {}

    # End-of-match / at-crease batters (includes detailed stats).
    for b in centre.get("batting") or []:
        pid = str(b.get("player_id"))
        runs = _int_or_none(b.get("runs"))
        if runs is None:
            continue
        batsmen[pid] = {
            "name": (b.get("known_as") or b.get("popular_name") or "").strip(),
            "runs": runs,
            "balls": _int_or_none(b.get("balls_faced")) or 0,
            "fours": _int_or_none(b.get("fours")) or 0,
            "sixes": _int_or_none(b.get("sixes")) or 0,
            "strike_rate": b.get("strike_rate"),
            "dismissal": b.get("dismissal_name"),
        }

    # Fall-of-wicket partnerships expose every batter that came to the crease.
    for fow in centre.get("fow") or []:
        for p in fow.get("player") or []:
            pid = str(p.get("player_id"))
            runs = _int_or_none(p.get("runs"))
            if runs is None:
                continue
            existing = batsmen.get(pid)
            if existing is None or runs > existing.get("runs", 0):
                batsmen[pid] = {
                    **(existing or {}),
                    "name": (p.get("known_as") or p.get("popular_name") or "").strip(),
                    "runs": runs,
                }

    top_scorer: dict[str, Any] = {}
    if batsmen:
        top_scorer = max(batsmen.values(), key=lambda b: b.get("runs", 0))

    # Bowlers: centre.bowling shows recent bowlers only, but it's better than nothing.
    bowlers_raw: list[dict[str, Any]] = []
    for b in centre.get("bowling") or []:
        bowlers_raw.append({
            "name": (b.get("known_as") or b.get("popular_name") or "").strip(),
            "wickets": _int_or_none(b.get("wickets")) or 0,
            "runs": _int_or_none(b.get("conceded")) or _int_or_none(b.get("runs")) or 0,
            "overs": b.get("overs"),
            "economy": b.get("economy_rate"),
        })
    top_bowler: dict[str, Any] = {}
    if bowlers_raw:
        top_bowler = max(
            bowlers_raw,
            key=lambda b: (b.get("wickets", 0), -b.get("runs", 0)),
        )

    return top_scorer, top_bowler


def _extract_notable_moments(comms: list) -> list[dict[str, Any]]:
    moments: list[dict[str, Any]] = []
    for c in comms:
        for b in c.get("ball") or []:
            event = (b.get("event") or "").upper()
            dismissal = b.get("dismissal") or ""
            kind: str | None = None
            if dismissal:
                kind = "wicket"
            elif "SIX" in event:
                kind = "six"
            elif "FOUR" in event:
                kind = "four"
            if not kind:
                continue
            players = b.get("players") or ""
            name_match = _NAME_SPLIT_RE.match(players)
            bowler, batsman = (name_match.group(1), name_match.group(2)) if name_match else (None, None)
            moments.append({
                "type": kind,
                "batsman": batsman,
                "bowler": bowler,
                "over": str(b.get("overs_actual") or b.get("over_number") or ""),
                "description": _strip_html(b.get("text")),
            })
    # The endpoint returns latest-first; reverse so downstream ranking sees chronology.
    moments.reverse()
    return moments


def _normalise(match_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    teams = raw.get("team") or []
    innings = raw.get("innings") or []
    match = raw.get("match") or {}
    centre = raw.get("centre") or {}
    comms = raw.get("comms") or []

    team_map = {str(t.get("team_id")): t for t in teams}
    team1 = _innings_summary(innings[0], team_map) if len(innings) > 0 else {}
    team2 = _innings_summary(innings[1], team_map) if len(innings) > 1 else {}
    description = raw.get("description") or match.get("cms_match_title") or f"Match {match_id}"
    result = _extract_result(comms, innings, team_map)
    pom = _extract_pom(comms)
    top_scorer, top_bowler = _extract_top_performers(centre, comms)
    moments = _extract_notable_moments(comms)

    if top_scorer and not pom and top_scorer.get("runs", 0) >= 50:
        # reasonable guess if commentary didn't expose POM explicitly
        pom = top_scorer.get("name")

    log.info(
        "scorecard parsed: %s vs %s — %s — %d notable moments, top scorer %s (%s), POM %s",
        team1.get("abbr") or team1.get("name") or "?",
        team2.get("abbr") or team2.get("name") or "?",
        result or "(no result parsed)",
        len(moments),
        top_scorer.get("name") or "(unknown)",
        top_scorer.get("runs") if top_scorer else "?",
        pom or "(unknown)",
    )

    return {
        "match": {
            "id": match_id,
            "description": description,
            "venue": match.get("ground_name") or "",
            "date": match.get("date_string") or match.get("date") or "",
            "result": result,
            "team1": team1,
            "team2": team2,
        },
        "top_performers": {
            "top_scorer": top_scorer,
            "top_bowler": top_bowler,
            "player_of_match": pom,
        },
        "notable_moments": moments,
    }
