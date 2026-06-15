"""Make py_mini_racer (V8) safe to use from our thread pools.

Several AKShare endpoints (the sina/tencent daily-history fetchers, sina
financial reports, fund NAV, …) decrypt their payloads by running JavaScript
through ``py_mini_racer`` — an embedded V8. V8 initialises a process-global
allocator (PartitionAlloc) the first time *any* ``MiniRacer()`` is created. If
two worker threads create their first MiniRacer at the same instant — which is
exactly what happens when the Paper-page price refresh, the screener snapshot
and a scheduled analysis all hit AKShare's sina fallback together — both race
to initialise that global pool and V8 aborts the whole process:

    [FATAL:partition_address_space.cc] Check failed: !IsConfigurablePoolInitialized().

The crash kills the Python process, so every in-flight HTTP request hangs in
``pending`` and the UI looks frozen.

Two defences here:

1. ``prewarm_v8()`` — create one MiniRacer on the **main thread** at startup so
   V8's global init happens once, single-threaded, before any worker can race
   it. This alone removes the abort.
2. ``V8_LOCK`` — a process-wide re-entrant lock the V8-using fetchers hold while
   they run JS, so two threads never drive V8 concurrently. Belt-and-suspenders
   in case init wasn't pre-warmed (e.g. a code path that imports AKShare lazily).
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Held by any code path that runs V8 (via an AKShare JS endpoint). Re-entrant so
# a fetcher that nests another guarded call doesn't deadlock itself.
V8_LOCK = threading.RLock()

_PREWARMED = False
_PREWARM_LOCK = threading.Lock()


def prewarm_v8() -> bool:
    """Force py_mini_racer's V8 global init on the calling thread, once.

    Call from the main thread at startup, before any worker pool runs AKShare.
    Idempotent and best-effort: if py_mini_racer isn't installed (or init
    fails) we log and move on — the lock still serialises real usage."""
    global _PREWARMED
    with _PREWARM_LOCK:
        if _PREWARMED:
            return True
        try:
            import py_mini_racer  # noqa: PLC0415 — lazy: optional dependency

            ctx = py_mini_racer.MiniRacer()
            ctx.eval("1+1")
            _PREWARMED = True
            logger.info("py_mini_racer / V8 pre-warmed on the main thread")
            return True
        except Exception as e:  # noqa: BLE001 — never let pre-warm crash startup
            logger.warning("V8 pre-warm skipped (%s); relying on V8_LOCK", e)
            return False
