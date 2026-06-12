"""Helpers that keep debate prompts bounded as rounds accumulate.

Every bull/bear/risk debate turn re-sends the analyst reports in full plus the
entire running transcript, so the per-turn input grows quadratically with the
number of rounds. These helpers clip each report and window the transcript to
the most recent turns, governed by ``debate_report_max_chars`` and
``debate_history_max_turns`` in the config (0 disables either cap).
"""

from tradingagents.dataflows.config import get_config

# Turn boundaries in the debate transcript — every turn is appended as
# "<Speaker>: <content>" (see researchers/ and risk_mgmt/ nodes).
SPEAKER_PREFIXES = (
    "Bull Analyst:",
    "Bear Analyst:",
    "Aggressive Analyst:",
    "Conservative Analyst:",
    "Neutral Analyst:",
)

_CLIP_NOTE = (
    "\n…(middle of the report clipped for this debate turn; the full report "
    "is kept in the final analysis output)…\n"
)


def clip_report(text: str) -> str:
    """Clip one analyst report to the configured per-report character budget.

    Keeps the head AND the tail, dropping the middle: every analyst prompt
    instructs the model to put a summary table at the END of the report, so a
    head-only cut would discard exactly the densest part.
    """
    limit = int(get_config().get("debate_report_max_chars") or 0)
    if not text or limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return text[:head] + _CLIP_NOTE + text[-tail:]


def recent_history(history: str) -> str:
    """Window the debate transcript to the configured number of recent turns."""
    limit = int(get_config().get("debate_history_max_turns") or 0)
    if not history or limit <= 0:
        return history
    turns: list[str] = []
    current: list[str] = []
    for line in history.split("\n"):
        if line.startswith(SPEAKER_PREFIXES):
            if current and any(s.strip() for s in current):
                turns.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current and any(s.strip() for s in current):
        turns.append("\n".join(current))
    if len(turns) <= limit:
        return history
    omitted = len(turns) - limit
    kept = "\n".join(turns[-limit:])
    return f"(earlier {omitted} turn(s) omitted to keep the prompt short)\n{kept}"
