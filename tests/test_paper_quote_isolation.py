"""Tests for the paper-router quote isolation fixes.

Covers the three regressions that used to hang the whole API when the quote
upstream degraded:
  1. ``_name_map`` is single-flight with a bounded wait (no thread pile-up).
  2. ``_name_map`` returns the stale cache instead of blocking when the
     upstream hangs.
  3. A-share price fetches never fall back to the timeout-less kline path.
"""

import threading
import time

import pytest


@pytest.fixture()
def paper(monkeypatch, tmp_path):
    """Import the router with a throwaway DB and reset its module caches."""
    monkeypatch.setenv("TRADINGAGENTS_WEB_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TRADINGAGENTS_DB_URL", raising=False)
    from web.backend.routers import paper as paper_mod
    # Reset shared module state between tests.
    with paper_mod._NAME_MAP_LOCK:
        paper_mod._NAME_MAP.clear()
    paper_mod._NAME_MAP_EXPIRES = 0.0
    paper_mod._NAME_MAP_FETCH = None
    with paper_mod._PRICE_CACHE_LOCK:
        paper_mod._PRICE_CACHE.clear()
    yield paper_mod
    with paper_mod._NAME_MAP_LOCK:
        paper_mod._NAME_MAP.clear()
    paper_mod._NAME_MAP_EXPIRES = 0.0
    paper_mod._NAME_MAP_FETCH = None


def test_name_map_single_flight(paper, monkeypatch):
    """Concurrent cold-cache callers share ONE upstream fetch."""
    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_upstream():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        with paper._NAME_MAP_LOCK:
            paper._NAME_MAP.update({"600519": "贵州茅台"})
            paper._NAME_MAP_EXPIRES = time.monotonic() + 60
            return dict(paper._NAME_MAP)

    monkeypatch.setattr(paper, "_fetch_name_map_upstream", fake_upstream)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(paper._name_map(wait_sec=10)))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    started.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1, "expected exactly one upstream fetch (single-flight)"
    assert all(r.get("600519") == "贵州茅台" for r in results)


def test_name_map_bounded_wait_returns_stale(paper, monkeypatch):
    """A hung upstream must not block the caller past wait_sec."""
    hang = threading.Event()

    def hung_upstream():
        hang.wait(timeout=30)  # simulates AKShare with no timeout
        return {}

    monkeypatch.setattr(paper, "_fetch_name_map_upstream", hung_upstream)
    # Seed a stale cache entry.
    with paper._NAME_MAP_LOCK:
        paper._NAME_MAP.update({"000001": "平安银行"})
    paper._NAME_MAP_EXPIRES = 0.0  # expired

    t0 = time.monotonic()
    out = paper._name_map(wait_sec=0.3)
    elapsed = time.monotonic() - t0
    hang.set()

    assert elapsed < 2.0, f"caller blocked {elapsed:.1f}s — bounded wait broken"
    assert out.get("000001") == "平安银行", "should fall back to stale cache"


def test_a_share_price_skips_kline_fallback(paper, monkeypatch):
    """When all spot endpoints fail for an A-share, do NOT hit route_to_vendor."""
    monkeypatch.setattr(paper, "_fetch_spot_price", lambda t: None)

    def boom(*a, **k):
        raise AssertionError("kline fallback must not run for A-share tickers")

    import tradingagents.dataflows.interface as iface
    monkeypatch.setattr(iface, "route_to_vendor", boom)

    assert paper._fetch_last_price_uncached("600519.SH") is None


def test_us_ticker_still_uses_kline_path(paper, monkeypatch):
    """Non-A-share tickers keep the kline path (yfinance has its own timeouts)."""
    called = []

    def fake_vendor(method, ticker, start, end):
        called.append(ticker)
        return "Header\n\nDate,Close\n2026-06-10,100.0\n2026-06-11,101.5\n"

    import tradingagents.dataflows.interface as iface
    monkeypatch.setattr(iface, "route_to_vendor", fake_vendor)

    price = paper._fetch_last_price_uncached("AAPL")
    assert called == ["AAPL"]
    assert price == 101.5


def test_resolve_name_never_fetches(paper, monkeypatch):
    """_resolve_name must be a pure cache lookup (event-loop safe)."""
    def boom():
        raise AssertionError("_resolve_name must not trigger an upstream fetch")

    monkeypatch.setattr(paper, "_fetch_name_map_upstream", boom)
    assert paper._resolve_name("600519.SH") is None  # cold cache → None, no fetch
    with paper._NAME_MAP_LOCK:
        paper._NAME_MAP.update({"600519": "贵州茅台"})
    assert paper._resolve_name("600519.SH") == "贵州茅台"
