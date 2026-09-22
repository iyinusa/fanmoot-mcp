"""Moments the recording could not contain.

``fixtures/`` is what fanmoot.com actually answered, and that is what makes it
worth asserting against (HAD-14). It was captured on a Tuesday morning out of
season, though, so the live digest is empty and every match thread is quiet —
and the tools that matter most on this surface are the ones that answer during
a match.

So a scenario test says "given Arsenal two-one up in the sixty-seventh minute,
with a thread arguing about the second goal" in as many words, and these
builders turn that sentence into the payload Fanmoot would have sent. They are
shaped by the same mirrors in ``app.fanmoot.models`` that validate the real
recordings, so a drift between this file and Fanmoot shows up as a validation
error rather than as a passing test about a shape nobody serves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

#: A fixed instant the voice tests are written against, so "in about two hours"
#: is a deterministic assertion rather than a clock race. Those tests pass a
#: ``now`` into ``app.voice`` and build their scenario around the same one.
#:
#: A test that drives a *tool* cannot do that — the tool reads the wall clock —
#: so it passes ``now=voice.now_utc()`` here instead, and everything below is
#: positioned relative to whichever instant it is handed.
NOW = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)

ARSENAL = {
    "id": "987f11c8-e5cd-45a8-ad6f-4cd81e95bf19",
    "slug": "arsenal",
    "name": "Arsenal",
    "short_name": "Arsenal",
    "badge_path": "/static/badges/premier-league/arsenal.webp",
    "primary_color": "#EF0107",
    "secondary_color": "#FFFFFF",
}

SPURS = {
    "id": "4a0f4f0e-2f1b-4a36-9a2e-0f3d1e6a77aa",
    "slug": "tottenham-hotspur",
    "name": "Tottenham Hotspur",
    "short_name": "Spurs",
    "badge_path": "/static/badges/premier-league/tottenham-hotspur.webp",
    "primary_color": "#132257",
    "secondary_color": "#FFFFFF",
}

PREMIER_LEAGUE = {
    "id": "52eddc50-e916-4fc7-ae6d-9a64a919597e",
    "slug": "premier-league",
    "name": "Premier League",
    "short_name": "Premier League",
    "logo_path": "/static/badges/leagues/premier-league.webp",
}


def clock(
    period: str = "2h",
    *,
    base_minute: int = 45,
    minutes_in: int = 22,
    now: datetime = NOW,
) -> dict[str, Any]:
    """A clock origin, the way Fanmoot sends one: where it started, not what it
    reads. Twenty-two minutes into the second half is the sixty-seventh."""
    return {
        "period": period,
        "base_minute": base_minute,
        "started_at": (now - timedelta(minutes=minutes_in)).isoformat(),
        "running": period not in {"ht", "pen"},
        "source": "server",
    }


def match(
    *,
    now: datetime = NOW,
    match_id: str = "11111111-2222-3333-4444-555555555555",
    status: str = "live",
    home: dict[str, Any] | None = None,
    away: dict[str, Any] | None = None,
    home_score: int | None = 2,
    away_score: int | None = 1,
    kickoff_at: datetime | None = None,
    score_state: str = "confirmed",
    venue: str | None = "the Emirates",
    moot_count: int = 0,
    with_clock: bool = True,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One match, as ``/v1/matches/{id}`` sends it."""
    return {
        "id": match_id,
        "url": "/matches/premier-league/2026-27/arsenal-vs-tottenham-hotspur-2026-09-22",
        "league": PREMIER_LEAGUE,
        "season": "2026-27",
        "home": home or ARSENAL,
        "away": away or SPURS,
        "kickoff_at": (kickoff_at or now - timedelta(minutes=67)).isoformat(),
        "status": status,
        "home_score": home_score,
        "away_score": away_score,
        "minute": None,
        "clock": clock(now=now) if with_clock and status == "live" else None,
        "score_state": score_state,
        "venue": venue,
        "round": "Regular Season - 6",
        "moot_count": moot_count,
        "events": events or [],
    }


def live_digest(matches: list[dict[str, Any]], *, at: datetime | None = None) -> dict[str, Any]:
    """``/v1/matches/live`` — ids, scores and clocks, and deliberately no names.

    Mirrors the real endpoint's thinness, which is the whole reason
    ``live_scores`` has to join it to the match records (see
    ``app/mcp/tools/matches.py``).
    """
    return {
        "at": (at or NOW).isoformat(),
        "matches": [
            {
                "id": m["id"],
                "status": m["status"],
                "home_score": m["home_score"],
                "away_score": m["away_score"],
                "clock": m["clock"],
                "score_state": m["score_state"],
            }
            for m in matches
        ],
    }


def moot(
    body: str,
    *,
    handle: str = "gooner_jo",
    name: str = "Jo",
    moot_id: str | None = None,
    reactions: int = 0,
    replies: int = 0,
    locked: bool = False,
    audience: str = "public",
    posted_at: datetime | None = None,
    now: datetime = NOW,
) -> dict[str, Any]:
    """One take."""
    identifier = moot_id or str(uuid.uuid4())
    return {
        "id": identifier,
        "short_id": identifier[:10].replace("-", ""),
        "url": f"/m/{identifier[:10].replace('-', '')}",
        "author": {
            "id": "6683dec2-f763-47db-b5d1-988cdb23faf8",
            "handle": handle,
            "name": name,
            "avatar_url": "/static/media/avatars/6e/2d/6e2d.svg",
            "is_verified": False,
            "is_agent": False,
            "club": ARSENAL,
        },
        "body": body,
        "kind": "moot",
        "status": "published",
        "created_at": (posted_at or now).isoformat(),
        "reply_count": replies,
        "reaction_count": reactions,
        "reaction_counts": {"heat": reactions},
        "locked": locked,
        "audience": audience,
    }


def moot_page(items: list[dict[str, Any]], *, next_cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor}


def thread(
    root: dict[str, Any],
    *,
    replies: list[dict[str, Any]] | None = None,
    ancestors: list[dict[str, Any]] | None = None,
    reply_total: int | None = None,
) -> dict[str, Any]:
    kept = replies or []
    return {
        "moot": root,
        "ancestors": ancestors or [],
        "replies": kept,
        "reply_total": reply_total if reply_total is not None else len(kept),
        "page": 1,
        "pages": 1,
    }


def match_page(matches: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    return {
        "items": matches,
        "total": total if total is not None else len(matches),
        "page": 1,
        "pages": 1,
    }


def match_groups(days: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"day": day, "items": items} for day, items in days.items()]
