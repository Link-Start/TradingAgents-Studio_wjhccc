import os
from datetime import timedelta, datetime

from fastapi import APIRouter, Query
from typing import Optional

from .. import database as db

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def dashboard():
    stats = db.get_dashboard_stats()
    return stats


@router.get("/usage")
async def usage():
    """LLM token usage + daily budget for the cost dashboard.

    Token-based (provider-agnostic, no price table to maintain). The daily
    budget is ``TRADINGAGENTS_DAILY_TOKEN_BUDGET`` (total in+out tokens/day,
    0 = unlimited); when today's usage reaches it, the scheduler pauses new
    scheduled analyses until the next local day.
    """
    today_cut = db.local_day_cutoff_utc_iso()
    month_cut = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
    series_cut = (datetime.utcnow() - timedelta(days=14)).isoformat() + "Z"
    budget = int(os.getenv("TRADINGAGENTS_DAILY_TOKEN_BUDGET", "0") or 0)
    today = db.usage_aggregate(today_cut)
    return {
        "today": today,
        "month": db.usage_aggregate(month_cut),
        "all": db.usage_aggregate(None),
        "daily": db.usage_daily_series(series_cut),
        "daily_budget": budget,
        "over_budget": bool(budget > 0 and today["tokens_total"] >= budget),
    }


@router.get("/compare")
async def compare(
    tickers: str = Query(..., description="Comma-separated ticker list"),
    days: int = 30,
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    return db.get_compare(ticker_list, days)
