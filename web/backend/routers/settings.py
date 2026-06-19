import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from tradingagents.default_config import DEFAULT_CONFIG, _ENV_OVERRIDES, _apply_env_overrides
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from .. import database as db
from ..models import SettingsUpdate, APIKeysUpdate, RiskSettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])

# Runtime settings override (in-memory, persists for server lifetime).
# Repopulated on startup from the DB via reload_overrides_from_env(), so this
# dict mirrors the persisted TRADINGAGENTS_* settings plus any per-request delta.
_overrides: dict = {}

# Project-root .env file. Still written best-effort so a *local* CLI run shares
# the same keys; in a container this file is ephemeral, which is exactly why the
# durable copy now lives in the DB (app_settings table).
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

# Reverse map: config-key → TRADINGAGENTS_* env var name. Built once from
# the single-source-of-truth dict in default_config so adding a new override
# in one place automatically makes it persist-able from the web.
_CONFIG_KEY_TO_ENV: dict[str, str] = {v: k for k, v in _ENV_OVERRIDES.items() if v}


def get_effective_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    config.update(_overrides)
    return config


def reload_overrides_from_env() -> None:
    """Rebuild ``_overrides`` from the current ``os.environ``.

    Called at startup AFTER ``database.load_app_settings_into_environ()`` has
    pushed the persisted DB settings into the environment, so the Settings page
    and every analysis run (which read ``get_effective_config()``) reflect the
    saved values. DEFAULT_CONFIG itself is a frozen import-time snapshot; this is
    how DB-persisted TRADINGAGENTS_* settings get re-applied on each boot.
    """
    base = DEFAULT_CONFIG.copy()
    _apply_env_overrides(base)  # reads os.environ (now seeded from the DB)
    for _env_var, key in _ENV_OVERRIDES.items():
        if key:
            _overrides[key] = base.get(key)


def _persist_env(env_var: str, value: Optional[str]) -> None:
    """Persist one env var to the DB (durable source of truth), mirror it into
    ``os.environ`` for the running process, and best-effort write-through to .env
    (for a co-located CLI; ignored if it fails, e.g. read-only/ephemeral fs).

    A falsy ``value`` clears the setting everywhere.
    """
    if value:
        db.set_app_setting(env_var, value)
        os.environ[env_var] = value
    else:
        db.delete_app_setting(env_var)
        os.environ.pop(env_var, None)
    try:
        from dotenv import set_key, unset_key  # type: ignore
        _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not _ENV_PATH.exists():
            _ENV_PATH.write_text("", encoding="utf-8")
        if value:
            set_key(str(_ENV_PATH), env_var, value, quote_mode="never")
        else:
            unset_key(str(_ENV_PATH), env_var)
    except Exception:  # noqa: BLE001 — .env write-through is best-effort only
        pass


@router.get("/settings")
async def get_settings():
    config = get_effective_config()
    return {
        "llm_provider": config.get("llm_provider"),
        "deep_think_llm": config.get("deep_think_llm"),
        "quick_think_llm": config.get("quick_think_llm"),
        "max_debate_rounds": config.get("max_debate_rounds"),
        "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds"),
        "output_language": config.get("output_language"),
        "checkpoint_enabled": config.get("checkpoint_enabled"),
        "benchmark_ticker": config.get("benchmark_ticker"),
        "data_cache_dir": config.get("data_cache_dir"),
        "results_dir": config.get("results_dir"),
        "memory_log_path": config.get("memory_log_path"),
        # Read-only: which DB backend is active (sqlite / mysql / …). Switching
        # is done via TRADINGAGENTS_DB_URL in .env + restart, not from the UI.
        "db_backend": db.current_backend(),
    }


@router.put("/settings")
async def update_settings(req: SettingsUpdate):
    """Update settings and persist any TRADINGAGENTS_*-mapped fields to the DB.

    Fields with a corresponding ``TRADINGAGENTS_*`` env var (per
    ``_ENV_OVERRIDES``) are persisted to the ``app_settings`` table (durable,
    survives container rebuilds) and mirrored into ``os.environ`` so:
      * the running server picks them up immediately (via ``_overrides``), and
      * the next server start re-applies them through
        ``database.load_app_settings_into_environ`` + ``reload_overrides_from_env``.
    Fields without an env-var mapping (e.g. ``data_cache_dir``) still get
    applied in-memory.
    """
    updates = req.model_dump(exclude_none=True)
    if not updates:
        return {"ok": True, "updated": []}

    # In-memory apply first, so /api/settings reflects the new state immediately.
    _overrides.update(updates)

    persisted: list[str] = []
    for key, value in updates.items():
        env_var = _CONFIG_KEY_TO_ENV.get(key)
        if not env_var or value is None:
            continue
        # Coerce booleans / ints into the string form env vars use.
        str_val = ("true" if value else "false") if isinstance(value, bool) else str(value)
        _persist_env(env_var, str_val)
        persisted.append(env_var)

    return {"ok": True, "updated": list(updates.keys()), "persisted": persisted}


# --- API key management ---------------------------------------------------
#
# Keys live in the project-root .env file (same one the CLI's
# load_dotenv() reads at import time). We mirror writes into os.environ
# so the running server picks up changes without a restart, and persist
# to disk so a restart doesn't lose them.

def _mask_key(value: str) -> str:
    """Return a UI-friendly mask: first 4 + last 4, ``***`` for short values."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def _provider_key_rows() -> list[dict]:
    """List of {provider, env_var, masked, set} for every supported provider."""
    rows = []
    for provider, env_var in PROVIDER_API_KEY_ENV.items():
        if not env_var:
            # Providers like ollama that don't authenticate; surface them so
            # the UI can show "No key required" rather than omitting silently.
            rows.append({
                "provider": provider,
                "env_var": None,
                "masked": "",
                "set": False,
                "required": False,
            })
            continue
        current = os.environ.get(env_var, "")
        rows.append({
            "provider": provider,
            "env_var": env_var,
            "masked": _mask_key(current),
            "set": bool(current),
            "required": True,
        })
    return rows


@router.get("/api-keys")
async def get_api_keys():
    """Return masked key state per provider; never echoes the raw key."""
    return {"providers": _provider_key_rows()}


@router.get("/model-catalog")
async def get_model_catalog():
    """Expose the per-provider model catalog the CLI uses.

    Returns ``{provider: {quick: [{label, value}, ...], deep: [...]}}``.
    Providers without a static catalog (e.g. ``openrouter``, ``azure``) are
    simply absent — the frontend should fall back to a free-text input for
    those, since their model lists are either dynamic or deployment-specific.
    """
    out: dict[str, dict[str, list[dict[str, str]]]] = {}
    for provider, modes in MODEL_OPTIONS.items():
        out[provider] = {
            mode: [{"label": label, "value": value} for label, value in options]
            for mode, options in modes.items()
        }
    return {"providers": out}


@router.put("/api-keys")
async def update_api_keys(req: APIKeysUpdate):
    """Update API keys for one or more providers.

    Each entry in ``keys`` maps a provider name (case-insensitive) to either
    a new key (any non-empty string) or an empty string to clear it. Changes
    are persisted to the durable ``app_settings`` table (so they survive a
    container rebuild) and mirrored into ``os.environ`` for the running process.
    """
    if not req.keys:
        return {"ok": True, "updated": []}

    updated: list[str] = []
    unknown: list[str] = []

    for provider_raw, value in req.keys.items():
        provider = provider_raw.strip().lower()
        env_var = PROVIDER_API_KEY_ENV.get(provider)
        if not env_var:
            unknown.append(provider_raw)
            continue
        _persist_env(env_var, value or "")
        updated.append(env_var)

    if unknown and not updated:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown providers: {', '.join(unknown)}",
        )

    return {"ok": True, "updated": updated, "unknown": unknown}


# --- Risk-control / budget settings --------------------------------------
#
# These business knobs (token budget, stop-loss, position caps, …) used to be
# env-only. They now live in the Settings page: persisted to the DB and mirrored
# into os.environ, which scheduler.py / risk.py / dashboard.py already read live.
# Field name → env var. ``int`` fields are stored without a decimal point.
_RISK_FIELD_ENV: dict[str, tuple[str, type]] = {
    "daily_token_budget":        ("TRADINGAGENTS_DAILY_TOKEN_BUDGET", int),
    "paper_stop_pct":            ("TRADINGAGENTS_PAPER_STOP_PCT", float),
    "paper_take_profit_pct":     ("TRADINGAGENTS_PAPER_TAKE_PROFIT_PCT", float),
    "paper_max_positions":       ("TRADINGAGENTS_PAPER_MAX_POSITIONS", int),
    "paper_max_position_pct":    ("TRADINGAGENTS_PAPER_MAX_POSITION_PCT", float),
    "paper_daily_loss_limit_pct": ("TRADINGAGENTS_PAPER_DAILY_LOSS_LIMIT_PCT", float),
    "risk_check_sec":            ("TRADINGAGENTS_RISK_CHECK_SEC", int),
}


def _read_risk_field(env_var: str, typ: type):
    raw = os.environ.get(env_var)
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw)) if typ is int else float(raw)
    except ValueError:
        return None


@router.get("/risk-settings")
async def get_risk_settings():
    """Current risk/budget knobs (None = unset → that guard is off)."""
    out = {field: _read_risk_field(env_var, typ)
           for field, (env_var, typ) in _RISK_FIELD_ENV.items()}
    # Surface the live daily-loss circuit-breaker state for the UI.
    try:
        from .. import risk
        out["buys_halted_today"] = risk.buys_halted()
    except Exception:  # noqa: BLE001
        out["buys_halted_today"] = False
    return out


@router.put("/risk-settings")
async def update_risk_settings(req: RiskSettingsUpdate):
    """Persist risk/budget knobs to the DB + os.environ (live, restart-safe)."""
    updates = req.model_dump(exclude_none=True)
    persisted: list[str] = []
    for field, value in updates.items():
        env_var, typ = _RISK_FIELD_ENV[field]
        # Coerce to the env string form: ints without a decimal, floats as-is.
        str_val = str(int(value)) if typ is int else str(float(value))
        _persist_env(env_var, str_val)
        persisted.append(env_var)
    return {"ok": True, "persisted": persisted}
