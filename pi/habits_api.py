"""Routes for the HABITS slide (lane habits-feed-1001). Included by
calendar_api.py with one include_router line plus one startup hook.

GET /api/habits/derived?days=14   camera-derived habits (habits_derived)
GET /api/habits/autolog/rules     the camera -> tap-list rules and the flag
POST /api/habits/autolog/run      run the auto-log now (same as the 15-min loop)
GET /api/habits/commits?days=14   git commits per day (habits_sources)
GET /api/habits/tokens?days=14    Claude Code tokens per day (habits_sources)
GET /api/habits/wall              one document for the wall feed
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from fastapi import APIRouter, Query

import database as db
import habits_autolog
import habits_derived
import habits_sources

log = logging.getLogger("habits")


@contextlib.asynccontextmanager
async def _lifespan(app):
    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()


router = APIRouter(lifespan=_lifespan)
AUTOLOG_EVERY_S = 900
_last_autolog: dict = {}


def _safe(fn, *a, **kw) -> dict:
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 - a source failing never takes the wall down
        return {"stale": True, "error": repr(e)[:200], "days": []}


@router.get("/api/habits/derived")
async def habits_derived_route(days: int = Query(14, ge=1, le=31)) -> dict:
    return await asyncio.to_thread(_safe, habits_derived.derived, days)


@router.get("/api/habits/autolog/rules")
async def habits_autolog_rules() -> dict:
    return habits_autolog.rules() | {"last_run": _last_autolog or None}


@router.post("/api/habits/autolog/run")
async def habits_autolog_run(dry_run: bool = False) -> dict:
    res = await asyncio.to_thread(_safe, habits_autolog.run, dry_run=dry_run)
    if not dry_run:
        _last_autolog.clear()
        _last_autolog.update(res)
    return res


@router.get("/api/habits/commits")
async def habits_commits(days: int = Query(14, ge=1, le=60)) -> dict:
    return await asyncio.to_thread(_safe, habits_sources.commits, days)


@router.get("/api/habits/tokens")
async def habits_tokens(days: int = Query(14, ge=1, le=60)) -> dict:
    return await asyncio.to_thread(_safe, habits_sources.tokens, days)


def _taps() -> list[dict]:
    out = []
    for h in db.get_habits():
        out.append({"id": h["id"], "name": h["name"], "emoji": h["emoji"], "color": h["color"],
                    "streak": h["streak"], "done_today": bool(h["done_today"]),
                    "today_source": h.get("today_source"), "last_logged": h["last_logged"],
                    "auto": any(r["habit"] == h["name"] and r["auto"] for r in habits_autolog.RULES)})
    return out


def wall_doc(days: int = 14) -> dict:
    return {
        "generated": datetime.now(habits_derived.TZ).isoformat(timespec="seconds"),
        "derived": _safe(habits_derived.derived, days),
        "taps": _taps_safe(),
        "autolog": habits_autolog.rules() | {"last_run": _last_autolog or None},
        "commits": _safe(habits_sources.commits, days),
        "tokens": _safe(habits_sources.tokens, days),
    }


def _taps_safe() -> list | dict:
    try:
        return _taps()
    except Exception as e:  # noqa: BLE001
        return {"stale": True, "error": repr(e)[:200]}


@router.get("/api/habits/wall")
async def habits_wall(days: int = Query(14, ge=1, le=31)) -> dict:
    return await asyncio.to_thread(wall_doc, days)


async def _loop():
    await asyncio.sleep(20)          # let the server come up first
    while True:
        try:
            res = await asyncio.to_thread(habits_autolog.run)
            _last_autolog.clear()
            _last_autolog.update(res)
            if res.get("logged"):
                log.warning("habits autolog: %s", res["logged"])
            # warm the other caches so the wall never waits on a cold fetch
            await asyncio.to_thread(_safe, habits_derived.derived, 14)
            await asyncio.to_thread(_safe, habits_sources.commits, 14)
            await asyncio.to_thread(_safe, habits_sources.tokens, 14)
        except Exception as e:  # noqa: BLE001
            log.warning("habits loop: %r", e)
        await asyncio.sleep(AUTOLOG_EVERY_S)


