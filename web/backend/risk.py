"""Paper-trading risk controls — auto stop-loss / take-profit + position limits.

Closes the "auto-buy but never auto-exit" gap: scheduled auto-trade opens
positions on a BUY decision, but without this a position only ever exits when a
*later* scheduled analysis happens to return SELL. Here a periodic price check
flattens a position the moment it breaches a stop-loss or take-profit band, and
buy-side guards cap concentration and halt buying after a daily loss limit.

Everything is OFF by default and enabled per-knob via environment variables, so
existing installs behave exactly as before until the operator opts in:

  TRADINGAGENTS_PAPER_STOP_PCT             e.g. 0.08  → flatten at -8%
  TRADINGAGENTS_PAPER_TAKE_PROFIT_PCT      e.g. 0.20  → flatten at +20%
  TRADINGAGENTS_PAPER_MAX_POSITIONS        e.g. 10    → no new buy past 10 names
  TRADINGAGENTS_PAPER_MAX_POSITION_PCT     e.g. 0.20  → cap one name at 20% equity
  TRADINGAGENTS_PAPER_DAILY_LOSS_LIMIT_PCT e.g. 0.05  → halt buys after -5% on the day

All knobs are read live (no restart needed). Functions are synchronous and
never raise into the caller — the scheduler runs them in an executor.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)


# --- knob readers (live) ----------------------------------------------------

def _f(name: str) -> float:
    try:
        return float(os.getenv(name, "0") or 0)
    except ValueError:
        return 0.0


def _i(name: str) -> int:
    try:
        return int(os.getenv(name, "0") or 0)
    except ValueError:
        return 0


def stop_pct() -> float:                return _f("TRADINGAGENTS_PAPER_STOP_PCT")
def take_profit_pct() -> float:         return _f("TRADINGAGENTS_PAPER_TAKE_PROFIT_PCT")
def max_positions() -> int:             return _i("TRADINGAGENTS_PAPER_MAX_POSITIONS")
def max_position_pct() -> float:        return _f("TRADINGAGENTS_PAPER_MAX_POSITION_PCT")
def daily_loss_limit_pct() -> float:    return _f("TRADINGAGENTS_PAPER_DAILY_LOSS_LIMIT_PCT")


def any_enabled() -> bool:
    return any(v > 0 for v in (stop_pct(), take_profit_pct(), daily_loss_limit_pct()))


# --- daily-loss circuit breaker state ---------------------------------------
# When the breaker trips, buys are halted for the rest of the (local) day. State
# is in-process; a restart clears it (the next risk check re-trips it if the
# loss persists).
_halt_date: Optional[str] = None


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def buys_halted() -> bool:
    return _halt_date == _today()


def _trip_breaker() -> None:
    global _halt_date
    _halt_date = _today()


# --- periodic risk check (stops / take-profit / daily-loss) ------------------

def run_risk_check(account_id: int) -> dict:
    """Flatten positions breaching stop/TP; trip the daily-loss breaker.

    Fetches each position's live price once, so it also computes current equity
    cheaply for the daily-loss test. Returns a summary for logging.
    """
    from .routers.paper import _fetch_last_price_sync, _check_market_state

    out = {"flattened": [], "halted": False}
    sp, tp, dll = stop_pct(), take_profit_pct(), daily_loss_limit_pct()
    if sp <= 0 and tp <= 0 and dll <= 0:
        return out
    try:
        acct = db.get_paper_account(account_id) or db.ensure_default_paper_account()
        positions = db.list_paper_positions(account_id)
        equity = float(acct["cash"])
        for p in positions:
            shares = float(p.get("shares") or 0)
            if shares <= 0:
                continue
            last = _fetch_last_price_sync(p["ticker"])
            if last is None or last <= 0:
                continue
            last = float(last)
            equity += shares * last
            avg = float(p.get("avg_cost") or 0)
            if avg <= 0:
                continue
            pnl = (last - avg) / avg
            kind: Optional[str] = None
            if sp > 0 and pnl <= -sp:
                kind = "stop"
            elif tp > 0 and pnl >= tp:
                kind = "tp"
            if not kind:
                continue
            # Don't try to sell what can't fill (suspended / locked limit-down).
            if _check_market_state(p["ticker"], "sell"):
                continue
            label = (f"自动止损 {pnl * 100:.1f}%" if kind == "stop"
                     else f"自动止盈 +{pnl * 100:.1f}%")
            order, err = db.place_paper_order(
                account_id=account_id, ticker=p["ticker"],
                asset_type=p.get("asset_type", "stock"), action="sell",
                shares=shares, price=last, source="auto",
                source_analysis_id=None, notes=label,
            )
            if err:
                logger.warning("risk: %s flatten failed for %s: %s", kind, p["ticker"], err)
                continue
            out["flattened"].append({"ticker": p["ticker"], "kind": kind, "pnl": round(pnl, 4)})
            logger.info("risk: auto-%s %s %.0f @ %.3f (pnl=%.1f%%)",
                        kind, p["ticker"], shares, last, pnl * 100)

        # Daily-loss breaker: equity vs the latest stored NAV (≈ prior close).
        if dll > 0:
            navs = db.list_paper_nav(account_id, limit=1)
            base = float(navs[0]["total_value"]) if navs else float(acct.get("initial_cash") or 0)
            if base > 0 and (equity - base) / base <= -dll:
                if not buys_halted():
                    logger.warning(
                        "risk: daily loss limit hit (equity=%.2f vs base=%.2f, %.1f%%) — buys halted today",
                        equity, base, (equity - base) / base * 100,
                    )
                _trip_breaker()
                out["halted"] = True
    except Exception:  # noqa: BLE001 — risk check must never crash the scheduler
        logger.exception("risk check failed")
    return out


# --- buy-side guards (called from execute_auto_trade) -----------------------

def buy_guard(account_id: int, positions: list, price: float,
              intended_shares: float, is_a_share: bool) -> tuple[float, Optional[str]]:
    """Vet/resize an intended auto-buy. Returns ``(allowed_shares, reason_or_None)``.

    ``allowed_shares == 0`` with a reason means "don't buy". Enforces, in order:
    daily-loss halt → max concurrent positions → max single-position % of equity
    (resizing the order down to fit, never up).
    """
    if buys_halted():
        return 0, "已触发单日亏损熔断，今日暂停买入"

    open_pos = [p for p in positions if (p.get("shares") or 0) > 0]

    mp = max_positions()
    if mp > 0 and len(open_pos) >= mp:
        return 0, f"持仓数已达上限 {mp}，跳过买入"

    mpp = max_position_pct()
    if mpp > 0 and price > 0:
        acct = db.get_paper_account(account_id)
        if acct:
            # Cheap equity proxy: cash + cost basis (no extra price fetch on the
            # buy path). Good enough to cap concentration.
            equity = float(acct["cash"]) + sum(
                float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)
            cap_shares = (equity * mpp) / price
            lot = 100 if is_a_share else 1
            if is_a_share:
                cap_shares = int(cap_shares // 100) * 100
            if cap_shares < intended_shares:
                intended_shares = cap_shares
            if intended_shares < lot:
                return 0, f"单票上限 {mpp * 100:.0f}% 不足一手，跳过买入"

    return intended_shares, None
