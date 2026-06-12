"""Compatibility re-export — the handler moved to ``tradingagents.utils`` so
the web backend can share it with the CLI."""

from tradingagents.utils.stats_handler import StatsCallbackHandler

__all__ = ["StatsCallbackHandler"]
