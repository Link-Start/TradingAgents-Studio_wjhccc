"""Tests for the debate prompt budget helpers (clip_report / recent_history)."""

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.agents.utils.debate_utils import clip_report, recent_history


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    # Restore the defaults so other tests are unaffected.
    set_config({"debate_report_max_chars": 4000, "debate_history_max_turns": 6})


def test_clip_report_under_limit_unchanged():
    set_config({"debate_report_max_chars": 100})
    assert clip_report("short report") == "short report"


def test_clip_report_over_limit_keeps_head_and_tail():
    set_config({"debate_report_max_chars": 50})
    text = "H" * 100 + "T" * 100  # head marker / tail marker
    out = clip_report(text)
    assert out.startswith("H" * 30)   # 60% of the budget from the head
    assert out.endswith("T" * 20)     # 40% of the budget from the tail
    assert "clipped" in out
    assert len(out) < len(text)


def test_clip_report_preserves_trailing_summary_table():
    # Analyst prompts put the summary table at the END of the report — the
    # clip must never drop it.
    set_config({"debate_report_max_chars": 100})
    body = "analysis text " * 50
    table = "| signal | direction |\n| BUY | up |"
    out = clip_report(body + table)
    assert out.endswith(table[-40:])


def test_clip_report_zero_disables():
    set_config({"debate_report_max_chars": 0})
    text = "y" * 100_000
    assert clip_report(text) == text


def test_clip_report_empty():
    assert clip_report("") == ""


def _turn(speaker: str, body: str) -> str:
    return f"{speaker}: {body}"


def test_recent_history_under_limit_unchanged():
    set_config({"debate_history_max_turns": 6})
    history = "\n" + "\n".join([
        _turn("Bull Analyst", "point 1"),
        _turn("Bear Analyst", "point 2"),
    ])
    assert recent_history(history) == history


def test_recent_history_windows_old_turns():
    set_config({"debate_history_max_turns": 2})
    history = "\n" + "\n".join([
        _turn("Bull Analyst", "round 1 bull"),
        _turn("Bear Analyst", "round 1 bear"),
        _turn("Bull Analyst", "round 2 bull"),
        _turn("Bear Analyst", "round 2 bear"),
    ])
    out = recent_history(history)
    assert "round 1 bull" not in out
    assert "round 1 bear" not in out
    assert "round 2 bull" in out
    assert "round 2 bear" in out
    assert "omitted" in out


def test_recent_history_preserves_multiline_turns():
    set_config({"debate_history_max_turns": 2})
    history = "\n".join([
        "",
        "Bull Analyst: line a",
        "continuation of bull",
        "Bear Analyst: line b",
        "continuation of bear",
        "Aggressive Analyst: line c",
    ])
    out = recent_history(history)
    assert "line a" not in out
    assert "Bear Analyst: line b" in out
    assert "continuation of bear" in out
    assert "Aggressive Analyst: line c" in out


def test_recent_history_zero_disables():
    set_config({"debate_history_max_turns": 0})
    history = "\n".join(_turn("Bull Analyst", f"t{i}") for i in range(50))
    assert recent_history(history) == history


def test_recent_history_empty():
    assert recent_history("") == ""
