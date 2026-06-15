"""REST endpoints for recurring screen schedules (auto-rotating analysis pool).

A screen schedule periodically re-runs a saved stock screen and reconciles a
pool of child analysis schedules: new hits are added, persistent misses are
evicted, held names are protected. Mirrors ``routers/schedule.py`` — thin async
wrappers around sync DB calls, with a "trigger now" that fires one reconcile.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from .. import database as db
from ..models import ScreenScheduleCreate, ScreenScheduleUpdate
from ..scheduler import (
    compute_first_run_at, compute_next_run_at, service as scheduler_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screen-schedules", tags=["screen-schedules"])


def _sub_config(req: ScreenScheduleCreate) -> dict:
    """Config for the child analysis schedules. llm_provider / think models are
    left None so the scheduler overlays the live effective config each run
    (same convention as the screener→schedule and holdings handoffs)."""
    return {
        "max_debate_rounds": req.max_debate_rounds,
        "max_risk_discuss_rounds": req.max_risk_discuss_rounds,
        "llm_provider": None,
        "deep_think_llm": None,
        "quick_think_llm": None,
        "output_language": req.output_language or "Chinese",
        "checkpoint_enabled": False,
    }


def _validate(req) -> None:
    if getattr(req, "schedule_type", None) not in ("daily", "weekly"):
        raise HTTPException(
            status_code=400,
            detail="schedule_type must be 'daily' | 'weekly' (screens rotate slowly)",
        )
    if not req.time_of_day:
        raise HTTPException(status_code=400, detail="time_of_day (HH:MM) is required")
    if req.schedule_type == "weekly" and (
        req.day_of_week is None or not (0 <= req.day_of_week <= 6)
    ):
        raise HTTPException(
            status_code=400,
            detail="day_of_week must be 0..6 (Mon..Sun) for weekly screens",
        )
    sub = getattr(req, "sub_schedule_type", "daily")
    if sub not in ("interval", "daily", "weekly"):
        raise HTTPException(
            status_code=400,
            detail="sub_schedule_type must be 'interval' | 'daily' | 'weekly'",
        )
    if sub == "interval" and (req.sub_interval_minutes or 0) < 5:
        raise HTTPException(
            status_code=400,
            detail="sub_interval_minutes must be >= 5 to avoid hammering LLM providers",
        )


@router.get("")
async def list_all():
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, db.list_screen_schedules, None)
    # Annotate each with its current managed-pool size for the UI.
    for it in items:
        pool = await loop.run_in_executor(
            None, db.list_schedules_by_source, it["id"],
        )
        it["managed_count"] = len(pool)
    return {"items": items, "total": len(items)}


@router.post("")
async def create(req: ScreenScheduleCreate):
    _validate(req)
    next_run = compute_first_run_at(
        req.schedule_type, None, req.time_of_day, req.day_of_week, req.asset_type,
    )
    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(
        None,
        lambda: db.create_screen_schedule(
            name=req.name,
            text=req.text,
            filters=req.filters,
            top_n=req.top_n,
            use_llm=req.use_llm,
            asset_type=req.asset_type,
            schedule_type=req.schedule_type,
            time_of_day=req.time_of_day,
            day_of_week=req.day_of_week,
            analysts=req.analysts,
            sub_schedule_type=req.sub_schedule_type,
            sub_interval_minutes=req.sub_interval_minutes,
            sub_time_of_day=req.sub_time_of_day,
            sub_day_of_week=req.sub_day_of_week,
            sub_config=_sub_config(req),
            evict_after_misses=req.evict_after_misses,
            max_pool_size=req.max_pool_size,
            auto_trade=req.auto_trade,
            auto_trade_cash_fraction=req.auto_trade_cash_fraction,
            next_run_at=next_run,
        ),
    )
    return row


@router.put("/{screen_schedule_id}")
async def update(screen_schedule_id: int, req: ScreenScheduleUpdate):
    loop = asyncio.get_running_loop()
    existing = await loop.run_in_executor(None, db.get_screen_schedule, screen_schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="screen schedule not found")
    fields = req.model_dump(exclude_unset=True)
    # Recompute next_run_at if the screen's own cadence changed.
    if any(k in fields for k in ("schedule_type", "time_of_day", "day_of_week")):
        merged = {**existing, **fields}
        fields["next_run_at"] = compute_next_run_at(
            merged.get("schedule_type") or existing["schedule_type"],
            None,
            merged.get("time_of_day"),
            merged.get("day_of_week"),
        )
        if "status" not in fields and existing["status"] == "disabled":
            fields["status"] = "active"
            fields["fail_count"] = 0
    updated = await loop.run_in_executor(
        None, lambda: db.update_screen_schedule(screen_schedule_id, **fields),
    )
    return updated


@router.delete("/{screen_schedule_id}")
async def delete(screen_schedule_id: int, cascade: bool = False):
    """Delete a screen schedule. ``?cascade=true`` also deletes the analysis
    schedules it created (its managed pool)."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: db.delete_screen_schedule(screen_schedule_id, cascade=cascade),
    )
    return {"ok": True}


@router.post("/{screen_schedule_id}/trigger")
async def trigger(screen_schedule_id: int):
    """Run one reconcile immediately, without touching next_run_at. Returns the
    pool size after reconciling."""
    loop = asyncio.get_running_loop()
    ss = await loop.run_in_executor(None, db.get_screen_schedule, screen_schedule_id)
    if not ss:
        raise HTTPException(status_code=404, detail="screen schedule not found")
    from .. import screen_schedule_runner
    ok = await screen_schedule_runner.reconcile(ss)
    pool = await loop.run_in_executor(
        None, db.list_schedules_by_source, screen_schedule_id,
    )
    return {"ok": ok, "managed_count": len(pool),
            "managed": [{"ticker": p["ticker"], "miss_count": p.get("miss_count", 0),
                         "schedule_id": p["id"]} for p in pool]}


@router.get("/{screen_schedule_id}/managed")
async def managed(screen_schedule_id: int):
    """List the analysis schedules this screen schedule currently manages."""
    loop = asyncio.get_running_loop()
    ss = await loop.run_in_executor(None, db.get_screen_schedule, screen_schedule_id)
    if not ss:
        raise HTTPException(status_code=404, detail="screen schedule not found")
    pool = await loop.run_in_executor(
        None, db.list_schedules_by_source, screen_schedule_id,
    )
    return {"items": pool, "total": len(pool)}
