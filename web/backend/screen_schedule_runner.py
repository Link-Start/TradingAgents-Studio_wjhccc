"""Reconcile a recurring screen into a rotating pool of analysis schedules.

A ``screen_schedule`` fires on a slow cadence (daily/weekly). Each fire:
  1. re-runs the deterministic screener (reusing ``ScreenerRunner``),
  2. diffs the fresh hit-list against the analysis schedules this screen
     currently manages (``schedules.source_screen_schedule_id = ss.id``),
  3. adds a child schedule for each new hit, resets the miss-counter on
     surviving hits, increments it on misses, and evicts a name only after
     ``evict_after_misses`` consecutive misses — unless it's currently held
     (real or paper position), in which case it's kept and analysed so the
     user still gets sell signals.

Held-name protection and the "evict only after N misses" grace both come from
the user's chosen rotation policy. The orchestration mirrors ``scheduler`` —
sync DB work runs in an executor; the one async touch-point is the screen run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from . import database as db
from .scheduler import compute_first_run_at

logger = logging.getLogger(__name__)


def _held_tickers() -> set[str]:
    """Union of real-holdings and default paper-account positions (upper-cased).

    These are protected from eviction: a bought name keeps being analysed even
    after it drops off the screen, so the agents can still flag a sell."""
    held: set[str] = set()
    try:
        for h in db.list_holdings():
            held.add(str(h["ticker"]).upper())
    except Exception:  # noqa: BLE001
        logger.exception("reconcile: list_holdings failed")
    try:
        acct = db.ensure_default_paper_account()
        for p in db.list_paper_positions(acct["id"]):
            if (p.get("shares") or 0) > 0:
                held.add(str(p["ticker"]).upper())
    except Exception:  # noqa: BLE001
        logger.exception("reconcile: list_paper_positions failed")
    return held


def _sub_config(ss: dict) -> dict:
    """Child analysis schedule config — stored verbatim at screen-create time.

    Like the screener→schedule handoff, llm_provider/think models are left None
    so the scheduler overlays the live effective config at run time."""
    try:
        return json.loads(ss.get("sub_config_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _reconcile_pool(ss: dict, candidates: list, screen_run_id: str) -> dict:
    """Synchronous diff + DB mutations. Returns a summary dict for logging."""
    ss_id = ss["id"]
    asset_type = ss.get("asset_type", "stock")
    evict_after = int(ss.get("evict_after_misses") or 3)
    max_pool = ss.get("max_pool_size")
    auto_trade = bool(ss.get("auto_trade"))
    auto_frac = ss.get("auto_trade_cash_fraction")
    analysts = json.loads(ss.get("analysts_json") or "[]")
    sub_config = _sub_config(ss)

    # Ordered hits (best-ranked first) and a quick lookup set.
    hits = [str(c.get("ticker") or c.get("code")).upper()
            for c in candidates if (c.get("ticker") or c.get("code"))]
    hit_set = set(hits)
    score_by_ticker = {
        str(c.get("ticker") or c.get("code")).upper(): (c.get("score") or 0.0)
        for c in candidates if (c.get("ticker") or c.get("code"))
    }

    managed = {s["ticker"].upper(): s for s in db.list_schedules_by_source(ss_id)}
    # Tickers already owned by SOME schedule (user-built or another screen) →
    # don't create a duplicate; we only manage rows we created.
    all_active = {
        s["ticker"].upper() for s in db.list_schedules(None)
        if s["status"] != "disabled"
    }
    held = _held_tickers()

    added, kept, missed, evicted, skipped = [], [], [], [], []

    next_run = compute_first_run_at(
        ss.get("sub_schedule_type") or "daily",
        ss.get("sub_interval_minutes"),
        ss.get("sub_time_of_day"),
        ss.get("sub_day_of_week"),
        asset_type,
    )

    # 1. Hits ----------------------------------------------------------------
    for ticker in hits:
        if ticker in managed:
            # Survivor — clear its miss streak.
            if managed[ticker].get("miss_count"):
                db.update_schedule(managed[ticker]["id"], miss_count=0)
            kept.append(ticker)
        elif ticker in all_active:
            # A user-built / other-screen schedule already covers it.
            skipped.append(ticker)
        else:
            db.create_schedule(
                name=f"选股池: {ticker}",
                ticker=ticker,
                asset_type=asset_type,
                schedule_type=ss.get("sub_schedule_type") or "daily",
                interval_minutes=ss.get("sub_interval_minutes"),
                time_of_day=ss.get("sub_time_of_day"),
                day_of_week=ss.get("sub_day_of_week"),
                analysts=analysts,
                config=sub_config,
                next_run_at=next_run,
                from_holding=False,
                auto_trade=auto_trade,
                auto_trade_cash_fraction=auto_frac,
                source_screen_schedule_id=ss_id,
            )
            all_active.add(ticker)
            added.append(ticker)

    # 2. Misses (managed rows no longer selected) ----------------------------
    for ticker, row in managed.items():
        if ticker in hit_set:
            continue
        if ticker in held:
            # Protected: keep analysing a held name; reset the streak so it
            # isn't evicted the instant it's sold.
            if row.get("miss_count"):
                db.update_schedule(row["id"], miss_count=0)
            kept.append(ticker)
            continue
        new_miss = int(row.get("miss_count") or 0) + 1
        if new_miss >= evict_after:
            db.delete_schedule(row["id"])
            evicted.append(ticker)
        else:
            db.update_schedule(row["id"], miss_count=new_miss)
            missed.append(ticker)

    # 3. Optional hard cap — evict surplus non-held names, worst first -------
    if max_pool and max_pool > 0:
        pool = [s for s in db.list_schedules_by_source(ss_id)
                if s["ticker"].upper() not in held]
        overflow = len(pool) - int(max_pool)
        if overflow > 0:
            # Worst = highest miss streak, then lowest fresh score.
            pool.sort(key=lambda s: (
                -int(s.get("miss_count") or 0),
                score_by_ticker.get(s["ticker"].upper(), 0.0),
            ))
            for s in pool[:overflow]:
                db.delete_schedule(s["id"])
                evicted.append(s["ticker"].upper())

    return {
        "matched": len(hits), "added": added, "kept": kept,
        "missed": missed, "evicted": evicted, "skipped": skipped,
        "screen_run_id": screen_run_id,
    }


async def reconcile(ss: dict) -> bool:
    """Run the screen and reconcile the managed pool. Returns success."""
    from .screener_runner import ScreenerRunner

    loop = asyncio.get_running_loop()
    run_id = str(uuid.uuid4())
    try:
        filters = json.loads(ss.get("filters_json") or "{}")
    except (TypeError, ValueError):
        filters = {}
    try:
        await loop.run_in_executor(
            None, lambda: db.create_screen_run(run_id, ss.get("text") or ""),
        )
        # Nobody listens to a scheduled screen — a dummy queue keeps the
        # event-emitting runner happy (same pattern as scheduler._run_analysis).
        queue: asyncio.Queue = asyncio.Queue()
        runner = ScreenerRunner(
            run_id, ss.get("text") or "", filters,
            int(ss.get("top_n") or 20), bool(ss.get("use_llm")), queue,
        )
        await runner.run()
        run = await loop.run_in_executor(None, lambda: db.get_screen_run(run_id))
        if not run or run.get("status") != "complete":
            logger.warning("screen schedule %s: screen run not complete (%s)",
                           ss["id"], run and run.get("status"))
            return False
        candidates = run.get("candidates") or []
        summary = await loop.run_in_executor(
            None, lambda: _reconcile_pool(ss, candidates, run_id),
        )
        logger.info(
            "screen schedule %s reconciled: matched=%d +%d kept=%d miss=%d evict=%d skip=%d",
            ss["id"], summary["matched"], len(summary["added"]),
            len(summary["kept"]), len(summary["missed"]),
            len(summary["evicted"]), len(summary["skipped"]),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("screen schedule %s reconcile failed", ss.get("id"))
        return False
