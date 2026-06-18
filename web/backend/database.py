import os
import re
import json
import logging
from datetime import datetime
from typing import Optional
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, event, text, inspect as sa_inspect,
    MetaData, Table, Column, Integer, Float, String, Text,
    ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

# Legacy SQLite file location. Still honoured for backward compatibility: when
# TRADINGAGENTS_DB_URL is not set, the engine targets this file as
# ``sqlite:///<_DB_PATH>``. Tests monkeypatch this attribute, so keep the name.
_DB_PATH = os.getenv(
    "TRADINGAGENTS_WEB_DB",
    os.path.join(os.path.expanduser("~"), ".tradingagents", "web_state.db"),
)

# ---------------------------------------------------------------------------
# Schema — declared once with SQLAlchemy MetaData so the SAME definition builds
# on SQLite *and* MySQL (create_all emits the right per-dialect DDL: AUTOINCREMENT
# vs AUTO_INCREMENT, VARCHAR vs TEXT, etc.). create_all is checkfirst=True and
# NEVER drops — an existing SQLite DB keeps its rows and original column types.
#
# Cross-DB type notes:
#   * id PKs are String(64) (not unbounded TEXT) because MySQL requires a bounded
#     length on a primary key / indexed column. UUID hex ids are <=36 chars.
#   * Timestamps stay ISO-8601 strings (app-generated) → String(40): portable,
#     no dialect datetime/timezone surprises.
#   * Large free text / JSON blobs use _BigText → LONGTEXT on MySQL (debate
#     histories and config blobs can exceed MySQL's 64 KB TEXT limit).
#   * from_holding / auto_trade stay INTEGER (0/1) to preserve existing read
#     semantics the frontend already depends on.
# ---------------------------------------------------------------------------

# TEXT on SQLite, LONGTEXT on MySQL (for columns that can exceed 64 KB).
_BigText = Text().with_variant(LONGTEXT(), "mysql")

_METADATA = MetaData()

Table(
    "analyses", _METADATA,
    Column("id", String(64), primary_key=True),
    Column("ticker", String(32), nullable=False),
    Column("trade_date", String(32), nullable=False),
    Column("asset_type", String(16), server_default=text("'stock'")),
    Column("analysts", Text, nullable=False),
    Column("config_json", _BigText, nullable=False),
    Column("status", String(24), server_default=text("'pending'")),
    Column("signal", String(16)),
    Column("confidence", Float),
    Column("final_decision", _BigText),
    Column("created_at", String(40), nullable=False),
    Column("completed_at", String(40)),
    Column("error_msg", Text),
    # LLM usage for the whole run, filled in when the analysis completes.
    Column("tokens_in", Integer),
    Column("tokens_out", Integer),
    Column("llm_calls", Integer),
    # list_analyses orders by created_at; latest_signal(s)_for_ticker(s) filter
    # on (ticker, status) then take the newest — both need these to avoid full
    # table scans as history grows.
    Index("idx_analyses_created", "created_at"),
    Index("idx_analyses_ticker_status_created", "ticker", "status", "created_at"),
)

Table(
    "agent_events", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("analysis_id", String(64), ForeignKey("analyses.id"), nullable=False),
    Column("agent_name", String(64), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("content", _BigText),
    Column("tokens_used", Integer),
    Column("timestamp", String(40), nullable=False),
    # SQLite does not auto-index FK columns; WS replay + delete filter on this.
    Index("idx_agent_events_analysis", "analysis_id"),
)

Table(
    "agent_reports", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("analysis_id", String(64), ForeignKey("analyses.id"), nullable=False),
    Column("agent_name", String(64), nullable=False),
    Column("report_type", String(64), nullable=False),
    Column("content", _BigText, nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("idx_agent_reports_analysis", "analysis_id"),
)

Table(
    "holdings", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", String(32), nullable=False),
    Column("asset_type", String(16), nullable=False, server_default=text("'stock'")),
    Column("shares", Float, nullable=False),
    Column("cost_price", Float, nullable=False),
    Column("open_date", String(32)),
    Column("notes", Text),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Index("idx_holdings_ticker", "ticker"),
)

Table(
    "schedules", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255)),
    Column("ticker", String(32), nullable=False),
    Column("asset_type", String(16), nullable=False, server_default=text("'stock'")),
    Column("schedule_type", String(24), nullable=False),
    Column("interval_minutes", Integer),
    Column("time_of_day", String(16)),
    Column("day_of_week", Integer),
    Column("analysts", Text, nullable=False),
    Column("config_json", _BigText, nullable=False),
    Column("status", String(16), nullable=False, server_default=text("'active'")),
    Column("fail_count", Integer, nullable=False, server_default=text("0")),
    Column("last_run_at", String(40)),
    Column("last_analysis_id", String(64)),
    Column("next_run_at", String(40), nullable=False),
    Column("from_holding", Integer, nullable=False, server_default=text("0")),
    Column("auto_trade", Integer, nullable=False, server_default=text("0")),
    Column("auto_trade_cash_fraction", Float),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Index("idx_schedules_next", "next_run_at", "status"),
    Index("idx_schedules_ticker", "ticker"),
    # Auto-rotation bookkeeping (added via ALTER for pre-existing DBs in init_db):
    #   source_screen_schedule_id — which screen_schedule auto-created this row
    #     (NULL = user-/holdings-created, never auto-managed).
    #   miss_count — consecutive screens that DIDN'T re-select this ticker; the
    #     screen scheduler evicts the row once it reaches evict_after_misses.
    Column("source_screen_schedule_id", Integer),
    Column("miss_count", Integer, nullable=False, server_default=text("0")),
)

# A recurring stock-screen that auto-maintains a rotating pool of analysis
# ``schedules``. Each fire re-runs the screener and reconciles the pool: new
# hits get a child schedule (source_screen_schedule_id = this id), persistent
# misses are evicted, held tickers are protected. See screen_schedule_runner.
Table(
    "screen_schedules", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255)),
    # --- screen definition (mirrors ScreenRequest) ---
    Column("text", Text),
    Column("filters_json", _BigText),
    Column("top_n", Integer, nullable=False, server_default=text("20")),
    Column("use_llm", Integer, nullable=False, server_default=text("0")),
    Column("asset_type", String(16), nullable=False, server_default=text("'stock'")),
    # --- when the screen itself runs (daily/weekly only — rotation is slow) ---
    Column("schedule_type", String(24), nullable=False),
    Column("time_of_day", String(16)),
    Column("day_of_week", Integer),
    # --- config for the child analysis schedules this screen creates ---
    Column("analysts_json", Text, nullable=False),
    Column("sub_schedule_type", String(24), nullable=False, server_default=text("'daily'")),
    Column("sub_interval_minutes", Integer),
    Column("sub_time_of_day", String(16)),
    Column("sub_day_of_week", Integer),
    Column("sub_config_json", _BigText, nullable=False),
    # --- rotation params ---
    Column("evict_after_misses", Integer, nullable=False, server_default=text("3")),
    Column("max_pool_size", Integer),
    Column("auto_trade", Integer, nullable=False, server_default=text("0")),
    Column("auto_trade_cash_fraction", Float),
    # --- recurrence bookkeeping (mirrors schedules) ---
    Column("status", String(16), nullable=False, server_default=text("'active'")),
    Column("fail_count", Integer, nullable=False, server_default=text("0")),
    Column("last_run_at", String(40)),
    Column("last_screen_run_id", String(64)),
    Column("next_run_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Index("idx_screen_schedules_next", "next_run_at", "status"),
)

Table(
    "paper_accounts", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False),
    Column("initial_cash", Float, nullable=False),
    Column("cash", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

Table(
    "paper_positions", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False),
    Column("ticker", String(32), nullable=False),
    Column("asset_type", String(16), nullable=False, server_default=text("'stock'")),
    Column("shares", Float, nullable=False),
    Column("avg_cost", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("account_id", "ticker", name="uq_paper_positions_acct_ticker"),
    Index("idx_paper_positions_acct", "account_id"),
)

Table(
    "paper_orders", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False),
    Column("ticker", String(32), nullable=False),
    Column("asset_type", String(16), nullable=False, server_default=text("'stock'")),
    Column("action", String(8), nullable=False),
    Column("shares", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("source", String(16), nullable=False, server_default=text("'manual'")),
    Column("source_analysis_id", String(64)),
    Column("notes", Text),
    Column("filled_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("idx_paper_orders_acct", "account_id", "filled_at"),
)

Table(
    "paper_nav", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False),
    Column("snapshot_date", String(32), nullable=False),
    Column("cash", Float, nullable=False),
    Column("positions_value", Float, nullable=False),
    Column("total_value", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("account_id", "snapshot_date", name="uq_paper_nav_acct_date"),
    Index("idx_paper_nav_acct", "account_id", "snapshot_date"),
)

Table(
    "backtest_runs", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False),
    Column("signal_source", String(64), nullable=False),
    Column("source_config", _BigText, nullable=False),
    Column("tickers", Text),
    Column("benchmark", String(32)),
    Column("start_date", String(32), nullable=False),
    Column("end_date", String(32), nullable=False),
    Column("initial_cash", Float, nullable=False),
    Column("sizing_mode", String(32), nullable=False),
    Column("sizing_config", _BigText, nullable=False),
    Column("confidence_floor", Float),
    Column("status", String(16), nullable=False, server_default=text("'pending'")),
    Column("metrics_json", _BigText),
    Column("warnings", Text),
    Column("final_cash", Float),
    Column("final_total", Float),
    Column("error_msg", Text),
    Column("created_at", String(40), nullable=False),
    Column("completed_at", String(40)),
    Index("idx_backtest_runs_created", "created_at"),
)

Table(
    "backtest_trades", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
    Column("timestamp", String(40), nullable=False),
    Column("ticker", String(32), nullable=False),
    Column("action", String(8), nullable=False),
    Column("shares", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("fee", Float, nullable=False),
    Column("realised_pnl", Float, nullable=False),
    Column("source_analysis_id", String(64)),
    Column("metadata_json", _BigText),
    Index("idx_backtest_trades_run", "run_id", "timestamp"),
)

Table(
    "backtest_nav", _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
    Column("snapshot_date", String(40), nullable=False),
    Column("total_value", Float, nullable=False),
    Column("benchmark_value", Float),
    Index("idx_backtest_nav_run", "run_id", "snapshot_date"),
)

Table(
    "screen_runs", _METADATA,
    Column("id", String(64), primary_key=True),
    Column("text", Text),
    Column("strategy_json", _BigText),
    Column("candidates_json", _BigText),
    Column("status", String(16), nullable=False, server_default=text("'pending'")),
    Column("error_msg", Text),
    Column("created_at", String(40), nullable=False),
    Column("completed_at", String(40)),
    Index("idx_screen_runs_created", "created_at"),
)

# Persisted app configuration managed from the Settings page — LLM/data API keys
# and the TRADINGAGENTS_* runtime settings. Stored by their ENVIRONMENT-VARIABLE
# name (e.g. ``OPENAI_API_KEY``, ``TRADINGAGENTS_MAX_DEBATE_ROUNDS``) so startup
# can copy them straight back into ``os.environ``. This replaces writing to the
# project-root .env, which is ephemeral inside a rebuilt container — the DB
# (MySQL in production) is durable, so UI-set keys/settings survive redeploys.
# Column names are ``skey``/``svalue`` because ``key``/``value`` are reserved
# words on MySQL. Values are stored in plaintext (same trust level as .env).
Table(
    "app_settings", _METADATA,
    Column("skey", String(128), primary_key=True),
    Column("svalue", _BigText),
    Column("updated_at", String(40), nullable=False),
)


# ---------------------------------------------------------------------------
# Engine — one SQLAlchemy Engine per process, rebuilt only when the resolved URL
# changes (tests monkeypatch _DB_PATH between cases). The connection pool replaces
# the old per-thread connection cache.
# ---------------------------------------------------------------------------

_engine = None
_engine_url: Optional[str] = None


def _resolve_url() -> str:
    """SQLAlchemy URL for the configured backend.

    ``TRADINGAGENTS_DB_URL`` wins when set (e.g.
    ``mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4``). Otherwise we
    fall back to the legacy SQLite file so existing installs need zero config.
    """
    url = os.getenv("TRADINGAGENTS_DB_URL")
    if url and url.strip():
        return url.strip()
    from pathlib import Path
    return "sqlite:///" + Path(_DB_PATH).as_posix()


def _get_engine():
    global _engine, _engine_url
    url = _resolve_url()
    if _engine is not None and _engine_url == url:
        return _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    if is_sqlite:
        db_file = parsed.database
        if db_file and db_file != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
        engine = create_engine(
            url, future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            # Non-persistent pragmas (busy_timeout/synchronous/foreign_keys) plus
            # WAL (persistent but idempotent) — same tuning the old code applied.
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    else:
        # MySQL / others: pool_pre_ping survives idle-connection drops;
        # pool_recycle stays under MySQL's default wait_timeout.
        engine = create_engine(
            url, future=True, pool_pre_ping=True, pool_recycle=1800,
        )
    _engine = engine
    _engine_url = url
    logger.info("DB engine ready: %s", parsed.render_as_string(hide_password=True))
    return engine


def current_backend() -> dict:
    """Backend descriptor for the Settings page (dialect + password-masked URL)."""
    parsed = make_url(_resolve_url())
    return {
        "dialect": parsed.get_backend_name(),
        "url": parsed.render_as_string(hide_password=True),
    }


# ---------------------------------------------------------------------------
# Compatibility shim — lets the existing query functions keep their SQLite-style
# API (``conn.execute(sql_with_?, (params,))`` → rows that support ``dict(row)``,
# ``row["col"]``, ``row[0]``, plus ``.lastrowid`` / ``.rowcount``) while running
# on any SQLAlchemy backend. ``?`` placeholders are rewritten to named binds so
# the same SQL works under both qmark (SQLite) and format (pymysql) paramstyles.
# ---------------------------------------------------------------------------

class _Row(dict):
    """dict with positional access too, mirroring ``sqlite3.Row`` semantics."""

    def __init__(self, mapping):
        super().__init__(mapping)
        self._vals = list(mapping.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)


def _named_sql(sql: str) -> str:
    """Rewrite positional ``?`` placeholders to ``:p0, :p1, …`` named binds."""
    counter = {"n": 0}

    def repl(_m):
        k = counter["n"]
        counter["n"] += 1
        return f":p{k}"

    return re.sub(r"\?", repl, sql)


def _binds(params) -> dict:
    return {f"p{i}": v for i, v in enumerate(params)}


class _ExecResult:
    """Wraps a SQLAlchemy CursorResult with the sqlite3-cursor surface used here."""

    def __init__(self, sa_result):
        self._sa = sa_result
        self.lastrowid = None
        self.rowcount = -1
        if sa_result is not None:
            try:
                self.rowcount = sa_result.rowcount
            except Exception:  # pragma: no cover - dialect quirk
                self.rowcount = -1
            try:
                self.lastrowid = sa_result.lastrowid
            except Exception:
                self.lastrowid = None
        self._rows_cache = None

    def _rows(self):
        if self._rows_cache is None:
            if self._sa is not None and self._sa.returns_rows:
                self._rows_cache = [_Row(m) for m in self._sa.mappings()]
            else:
                self._rows_cache = []
        return self._rows_cache

    def fetchone(self):
        rows = self._rows()
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._rows())


class _Conn:
    """Thin wrapper exposing ``execute`` / ``executemany`` over a SA connection."""

    def __init__(self, sa_conn):
        self._c = sa_conn

    def execute(self, sql: str, params=None) -> _ExecResult:
        binds = _binds(list(params)) if params else {}
        return _ExecResult(self._c.execute(text(_named_sql(sql)), binds))

    def executemany(self, sql: str, seq_params) -> _ExecResult:
        seq = [list(p) for p in seq_params]
        if not seq:
            return _ExecResult(None)
        rows = [_binds(p) for p in seq]
        return _ExecResult(self._c.execute(text(_named_sql(sql)), rows))


@contextmanager
def get_db():
    """Transactional connection: commits on clean exit, rolls back on error."""
    engine = _get_engine()
    with engine.begin() as sa_conn:
        yield _Conn(sa_conn)


def init_db():
    """Create any missing tables (never drops) and apply column migrations.

    ``create_all`` is checkfirst=True, so an existing SQLite DB is left fully
    intact — only absent tables are created. On a fresh MySQL database it builds
    the whole schema from the MetaData above.
    """
    engine = _get_engine()
    _METADATA.create_all(engine)
    # create_all skips tables that already exist — including any indexes added
    # to the schema after the table was first created. Create those explicitly
    # so pre-existing DBs pick up new indexes (checkfirst makes this idempotent).
    with engine.begin() as conn:
        existing = {
            ix["name"]
            for table in _METADATA.tables.values()
            for ix in sa_inspect(conn).get_indexes(table.name)
        }
        for table in _METADATA.tables.values():
            for index in table.indexes:
                if index.name not in existing:
                    index.create(conn)
    # Column migrations for DBs created before a column was added. Only matters
    # for a pre-existing SQLite file; a fresh DB already has the column.
    with engine.begin() as conn:
        cols = {c["name"] for c in sa_inspect(conn).get_columns("schedules")}
        if "auto_trade" not in cols:
            conn.execute(text(
                "ALTER TABLE schedules ADD COLUMN auto_trade INTEGER NOT NULL DEFAULT 0"))
        if "auto_trade_cash_fraction" not in cols:
            conn.execute(text(
                "ALTER TABLE schedules ADD COLUMN auto_trade_cash_fraction REAL"))
        if "source_screen_schedule_id" not in cols:
            conn.execute(text(
                "ALTER TABLE schedules ADD COLUMN source_screen_schedule_id INTEGER"))
        if "miss_count" not in cols:
            conn.execute(text(
                "ALTER TABLE schedules ADD COLUMN miss_count INTEGER NOT NULL DEFAULT 0"))
        a_cols = {c["name"] for c in sa_inspect(conn).get_columns("analyses")}
        for col in ("tokens_in", "tokens_out", "llm_calls"):
            if col not in a_cols:
                conn.execute(text(f"ALTER TABLE analyses ADD COLUMN {col} INTEGER"))


# --- Analyses CRUD ---

def create_analysis(id: str, ticker: str, trade_date: str, asset_type: str,
                    analysts: list, config: dict) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO analyses (id, ticker, trade_date, asset_type, analysts, config_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (id, ticker, trade_date, asset_type, json.dumps(analysts), json.dumps(config), now),
        )
    return {"id": id, "status": "pending", "created_at": now}


def update_analysis_status(id: str, status: str, signal: Optional[str] = None,
                           confidence: Optional[float] = None,
                           final_decision: Optional[str] = None,
                           error_msg: Optional[str] = None,
                           tokens_in: Optional[int] = None,
                           tokens_out: Optional[int] = None,
                           llm_calls: Optional[int] = None):
    with get_db() as conn:
        fields = ["status = ?"]
        params = [status]
        if signal is not None:
            fields.append("signal = ?")
            params.append(signal)
        if confidence is not None:
            fields.append("confidence = ?")
            params.append(confidence)
        if final_decision is not None:
            fields.append("final_decision = ?")
            params.append(final_decision)
        if error_msg is not None:
            fields.append("error_msg = ?")
            params.append(error_msg)
        if tokens_in is not None:
            fields.append("tokens_in = ?")
            params.append(tokens_in)
        if tokens_out is not None:
            fields.append("tokens_out = ?")
            params.append(tokens_out)
        if llm_calls is not None:
            fields.append("llm_calls = ?")
            params.append(llm_calls)
        if status in ("complete", "failed"):
            fields.append("completed_at = ?")
            params.append(datetime.utcnow().isoformat() + "Z")
        params.append(id)
        conn.execute(f"UPDATE analyses SET {', '.join(fields)} WHERE id = ?", params)


def get_analysis(id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None


def list_analyses(ticker: Optional[str] = None, signal: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None,
                  page: int = 1, size: int = 20) -> dict:
    conditions = []
    params = []
    if ticker:
        conditions.append("ticker LIKE ?")
        params.append(f"%{ticker}%")
    if signal:
        conditions.append("signal = ?")
        params.append(signal)
    if date_from:
        conditions.append("trade_date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("trade_date <= ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    # List view only needs metadata — final_decision / config_json can be tens
    # of KB per row and would bloat every page of results.
    list_cols = ("id, ticker, trade_date, asset_type, analysts, status, signal, "
                 "confidence, created_at, completed_at, error_msg, "
                 "tokens_in, tokens_out, llm_calls")
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM analyses {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {list_cols} FROM analyses {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


def delete_analysis(id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM agent_events WHERE analysis_id = ?", (id,))
        conn.execute("DELETE FROM agent_reports WHERE analysis_id = ?", (id,))
        conn.execute("DELETE FROM analyses WHERE id = ?", (id,))


def fail_stale_runs() -> int:
    """Mark interrupted analyses / screen runs as failed on startup.

    A process kill (crash, reload, Ctrl-C) leaves rows stuck in
    'pending'/'running' forever — their in-memory runner is gone but the DB
    still says they're live. We reconcile that at boot so the history/screener
    views don't show ghost runs the user then can't get rid of. Returns the
    number of rows reconciled.
    """
    now = datetime.utcnow().isoformat() + "Z"
    msg = "中断（服务重启）"
    with get_db() as conn:
        a = conn.execute(
            "UPDATE analyses SET status = 'failed', error_msg = ?, completed_at = ? "
            "WHERE status IN ('pending', 'running')",
            (msg, now),
        )
        s = conn.execute(
            "UPDATE screen_runs SET status = 'error', error_msg = ?, completed_at = ? "
            "WHERE status IN ('pending', 'running')",
            (msg, now),
        )
    return (a.rowcount or 0) + (s.rowcount or 0)


# --- Screen runs (选股) CRUD ---

def create_screen_run(id: str, text: str) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO screen_runs (id, text, status, created_at) VALUES (?, ?, 'pending', ?)",
            (id, text, now),
        )
    return {"id": id, "text": text, "status": "pending", "created_at": now}


def update_screen_run(id: str, *, status: Optional[str] = None,
                      strategy: Optional[dict] = None,
                      candidates: Optional[list] = None,
                      error_msg: Optional[str] = None) -> Optional[dict]:
    sets, params = [], []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
        if status in ("complete", "error"):
            sets.append("completed_at = ?")
            params.append(datetime.utcnow().isoformat() + "Z")
    if strategy is not None:
        sets.append("strategy_json = ?")
        params.append(json.dumps(strategy, ensure_ascii=False))
    if candidates is not None:
        sets.append("candidates_json = ?")
        params.append(json.dumps(candidates, ensure_ascii=False))
    if error_msg is not None:
        sets.append("error_msg = ?")
        params.append(error_msg)
    if not sets:
        return get_screen_run(id)
    params.append(id)
    with get_db() as conn:
        conn.execute(f"UPDATE screen_runs SET {', '.join(sets)} WHERE id = ?", params)
    return get_screen_run(id)


def get_screen_run(id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM screen_runs WHERE id = ?", (id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["strategy"] = json.loads(d.pop("strategy_json") or "null")
    d["candidates"] = json.loads(d.pop("candidates_json") or "[]")
    return d


def list_screen_runs(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, text, status, created_at, completed_at FROM screen_runs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_screen_run(id: str) -> bool:
    """Delete one screen run. Returns True if a row was removed."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM screen_runs WHERE id = ?", (id,))
    return cur.rowcount > 0


def get_dashboard_stats() -> dict:
    with get_db() as conn:
        recent = conn.execute(
            "SELECT * FROM analyses ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        signal_dist = conn.execute(
            "SELECT signal, COUNT(*) as count FROM analyses WHERE signal IS NOT NULL GROUP BY signal"
        ).fetchall()
    return {
        "recent": [dict(r) for r in recent],
        "signal_distribution": {r["signal"]: r["count"] for r in signal_dist},
    }


def get_compare(tickers: list, days: int = 30) -> list:
    placeholders = ",".join("?" * len(tickers))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT ticker, trade_date, signal, confidence, created_at FROM analyses "
            f"WHERE ticker IN ({placeholders}) AND signal IS NOT NULL "
            f"ORDER BY created_at DESC LIMIT ?",
            tickers + [days],
        ).fetchall()
    return [dict(r) for r in rows]


# --- Agent Events ---

def add_agent_event(analysis_id: str, agent_name: str, event_type: str,
                    content: Optional[str] = None, tokens_used: Optional[int] = None):
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_events (analysis_id, agent_name, event_type, content, tokens_used, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (analysis_id, agent_name, event_type, content, tokens_used, now),
        )


def get_agent_events(analysis_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE analysis_id = ? ORDER BY timestamp",
            (analysis_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Agent Reports ---

def add_agent_report(analysis_id: str, agent_name: str, report_type: str, content: str):
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_reports (analysis_id, agent_name, report_type, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (analysis_id, agent_name, report_type, content, now),
        )


def get_agent_reports(analysis_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_reports WHERE analysis_id = ? ORDER BY created_at",
            (analysis_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Holdings ---

def create_holding(ticker: str, asset_type: str, shares: float, cost_price: float,
                   open_date: Optional[str] = None, notes: Optional[str] = None) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO holdings (ticker, asset_type, shares, cost_price, open_date, notes, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, asset_type, shares, cost_price, open_date, notes, now, now),
        )
        return {"id": cur.lastrowid, "created_at": now}


def list_holdings() -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM holdings ORDER BY ticker, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_holding(holding_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM holdings WHERE id = ?", (holding_id,)
        ).fetchone()
    return dict(row) if row else None


def update_holding(holding_id: int, **fields) -> Optional[dict]:
    if not fields:
        return get_holding(holding_id)
    cols, vals = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k not in ("shares", "cost_price", "open_date", "notes"):
            continue
        cols.append(f"{k} = ?")
        vals.append(v)
    if not cols:
        return get_holding(holding_id)
    cols.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat() + "Z")
    vals.append(holding_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE holdings SET {', '.join(cols)} WHERE id = ?", vals,
        )
    return get_holding(holding_id)


def delete_holding(holding_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))


def latest_signal_for_ticker(ticker: str) -> Optional[dict]:
    """Most recent complete analysis for ``ticker``, used to annotate holdings."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, signal, confidence, trade_date, created_at "
            "FROM analyses WHERE ticker = ? AND status = 'complete' "
            "ORDER BY created_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    return dict(row) if row else None


def latest_signals_for_tickers(tickers: list[str]) -> dict[str, dict]:
    """Most recent complete analysis per ticker, in ONE query.

    Batch variant of ``latest_signal_for_ticker`` for list views (holdings page)
    — avoids N+1 round trips. Greatest-n-per-group via a MAX(created_at) join
    rather than window functions so it runs on SQLite and older MySQL alike.
    Returns ``{ticker: row_dict}``; tickers with no complete analysis are absent.
    """
    tickers = list(dict.fromkeys(tickers))  # dedupe, keep order
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT a.id, a.ticker, a.signal, a.confidence, a.trade_date, a.created_at "
            f"FROM analyses a "
            f"JOIN (SELECT ticker, MAX(created_at) AS max_created "
            f"      FROM analyses WHERE status = 'complete' AND ticker IN ({placeholders}) "
            f"      GROUP BY ticker) m "
            f"  ON m.ticker = a.ticker AND m.max_created = a.created_at "
            f"WHERE a.status = 'complete'",
            tickers,
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        ticker = d.pop("ticker")
        out[ticker] = d
    return out


# --- Schedules ---

def create_schedule(
    *,
    name: Optional[str],
    ticker: str,
    asset_type: str,
    schedule_type: str,
    interval_minutes: Optional[int],
    time_of_day: Optional[str],
    day_of_week: Optional[int],
    analysts: list,
    config: dict,
    next_run_at: str,
    from_holding: bool = False,
    auto_trade: bool = False,
    auto_trade_cash_fraction: Optional[float] = None,
    source_screen_schedule_id: Optional[int] = None,
) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (name, ticker, asset_type, schedule_type, "
            "interval_minutes, time_of_day, day_of_week, analysts, config_json, "
            "next_run_at, from_holding, auto_trade, auto_trade_cash_fraction, "
            "source_screen_schedule_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, ticker, asset_type, schedule_type, interval_minutes, time_of_day,
             day_of_week, json.dumps(analysts), json.dumps(config), next_run_at,
             1 if from_holding else 0, 1 if auto_trade else 0,
             auto_trade_cash_fraction, source_screen_schedule_id, now, now),
        )
        sid = cur.lastrowid
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
    return dict(row)


def list_schedules(status: Optional[str] = None) -> list:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE status = ? ORDER BY next_run_at",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY status, next_run_at"
            ).fetchall()
    return [dict(r) for r in rows]


def get_schedule(schedule_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
    return dict(row) if row else None


def update_schedule(schedule_id: int, **fields) -> Optional[dict]:
    """Update mutable fields on a schedule. Unknown/None keys are ignored."""
    allowed = {
        "name", "schedule_type", "interval_minutes", "time_of_day", "day_of_week",
        "analysts", "config_json", "status", "next_run_at", "last_run_at",
        "last_analysis_id", "fail_count", "auto_trade", "auto_trade_cash_fraction",
        "miss_count",
    }
    cols, vals = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "auto_trade":
            v = 1 if v else 0
        cols.append(f"{k} = ?")
        vals.append(json.dumps(v) if k in ("analysts",) and isinstance(v, list) else v)
    if not cols:
        return get_schedule(schedule_id)
    cols.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat() + "Z")
    vals.append(schedule_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE schedules SET {', '.join(cols)} WHERE id = ?", vals,
        )
    return get_schedule(schedule_id)


def delete_schedule(schedule_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


def due_schedules(now_iso: str) -> list:
    """Return active schedules whose next_run_at is at or before ``now_iso``."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE status = 'active' "
            "AND next_run_at <= ? ORDER BY next_run_at",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_schedule_fire(
    schedule_id: int,
    *,
    success: bool,
    analysis_id: Optional[str],
    next_run_at: str,
    auto_disable_after: int = 3,
):
    """Update a schedule after a fire. On failure, increment fail_count and
    auto-disable when it reaches ``auto_disable_after``."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        sched = conn.execute(
            "SELECT fail_count FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        if not sched:
            return
        if success:
            new_fail = 0
            new_status = "active"
        else:
            new_fail = sched["fail_count"] + 1
            new_status = "disabled" if new_fail >= auto_disable_after else "active"
        conn.execute(
            "UPDATE schedules SET fail_count = ?, status = ?, last_run_at = ?, "
            "last_analysis_id = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (new_fail, new_status, now, analysis_id, next_run_at, now, schedule_id),
        )


def list_schedules_by_source(screen_schedule_id: int) -> list:
    """Analysis schedules auto-created by a given screen_schedule (its pool)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE source_screen_schedule_id = ? "
            "ORDER BY ticker",
            (screen_schedule_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_analysis_exists(ticker: str, within_minutes: int = 30) -> bool:
    """True if a non-failed analysis for ``ticker`` was created within the window.

    Runtime de-dup so two schedules covering the same ticker (e.g. one manual +
    one from a screen pool) don't both burn a deep analysis when they fire close
    together — the second sees the first's fresh result and skips. The window is
    short so a single interval schedule's own cadence (typically hourly) is never
    blocked. Holdings tracking never triggers analysis, so it can't conflict here.
    """
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(minutes=within_minutes)).isoformat() + "Z"
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM analyses WHERE ticker = ? "
            "AND status IN ('pending', 'running', 'complete') AND created_at >= ? LIMIT 1",
            (ticker, cutoff),
        ).fetchone()
    return row is not None


def has_buy_filled_today(ticker: str, today: Optional[str] = None) -> bool:
    """True if ``ticker`` has a paper buy order filled today (server-local).

    Used to skip a same-day re-analysis of a freshly bought name — it only
    needs re-evaluating from the next trading day onward (might need a sell).
    ``filled_at`` is stored as a UTC ISO string; we match on the date prefix
    passed in (caller supplies a server-local YYYY-MM-DD)."""
    day = today or datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM paper_orders WHERE ticker = ? AND action = 'buy' "
            "AND filled_at LIKE ? LIMIT 1",
            (ticker, day + "%"),
        ).fetchone()
    return row is not None


# --- Screen schedules (auto-rotating analysis pool) ---

def create_screen_schedule(
    *,
    name: Optional[str],
    text: Optional[str],
    filters: Optional[dict],
    top_n: int,
    use_llm: bool,
    asset_type: str,
    schedule_type: str,
    time_of_day: Optional[str],
    day_of_week: Optional[int],
    analysts: list,
    sub_schedule_type: str,
    sub_interval_minutes: Optional[int],
    sub_time_of_day: Optional[str],
    sub_day_of_week: Optional[int],
    sub_config: dict,
    evict_after_misses: int,
    max_pool_size: Optional[int],
    auto_trade: bool,
    auto_trade_cash_fraction: Optional[float],
    next_run_at: str,
) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO screen_schedules (name, text, filters_json, top_n, "
            "use_llm, asset_type, schedule_type, time_of_day, day_of_week, "
            "analysts_json, sub_schedule_type, sub_interval_minutes, "
            "sub_time_of_day, sub_day_of_week, sub_config_json, "
            "evict_after_misses, max_pool_size, auto_trade, "
            "auto_trade_cash_fraction, next_run_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, text, json.dumps(filters or {}), top_n, 1 if use_llm else 0,
             asset_type, schedule_type, time_of_day, day_of_week,
             json.dumps(analysts), sub_schedule_type, sub_interval_minutes,
             sub_time_of_day, sub_day_of_week, json.dumps(sub_config),
             evict_after_misses, max_pool_size, 1 if auto_trade else 0,
             auto_trade_cash_fraction, next_run_at, now, now),
        )
        sid = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM screen_schedules WHERE id = ?", (sid,)
        ).fetchone()
    return dict(row)


def list_screen_schedules(status: Optional[str] = None) -> list:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM screen_schedules WHERE status = ? ORDER BY next_run_at",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM screen_schedules ORDER BY status, next_run_at"
            ).fetchall()
    return [dict(r) for r in rows]


def get_screen_schedule(screen_schedule_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM screen_schedules WHERE id = ?", (screen_schedule_id,)
        ).fetchone()
    return dict(row) if row else None


def update_screen_schedule(screen_schedule_id: int, **fields) -> Optional[dict]:
    """Update mutable fields on a screen schedule. Unknown/None keys ignored.

    JSON-typed keys (filters, analysts, sub_config) accept python objects and
    are serialised here; pass them under their python names (``filters`` etc.)."""
    json_map = {"filters": "filters_json", "analysts": "analysts_json",
                "sub_config": "sub_config_json"}
    allowed = {
        "name", "text", "top_n", "use_llm", "asset_type", "schedule_type",
        "time_of_day", "day_of_week", "sub_schedule_type", "sub_interval_minutes",
        "sub_time_of_day", "sub_day_of_week", "evict_after_misses", "max_pool_size",
        "auto_trade", "auto_trade_cash_fraction", "status", "fail_count",
        "last_run_at", "last_screen_run_id", "next_run_at",
        "filters_json", "analysts_json", "sub_config_json",
    }
    cols, vals = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k in json_map:
            cols.append(f"{json_map[k]} = ?")
            vals.append(json.dumps(v))
            continue
        if k not in allowed:
            continue
        if k in ("use_llm", "auto_trade"):
            v = 1 if v else 0
        cols.append(f"{k} = ?")
        vals.append(v)
    if not cols:
        return get_screen_schedule(screen_schedule_id)
    cols.append("updated_at = ?")
    vals.append(datetime.utcnow().isoformat() + "Z")
    vals.append(screen_schedule_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE screen_schedules SET {', '.join(cols)} WHERE id = ?", vals,
        )
    return get_screen_schedule(screen_schedule_id)


def delete_screen_schedule(screen_schedule_id: int, *, cascade: bool = False):
    """Delete a screen schedule. With ``cascade``, also delete the analysis
    schedules it auto-created (its managed pool)."""
    with get_db() as conn:
        if cascade:
            conn.execute(
                "DELETE FROM schedules WHERE source_screen_schedule_id = ?",
                (screen_schedule_id,),
            )
        conn.execute(
            "DELETE FROM screen_schedules WHERE id = ?", (screen_schedule_id,)
        )


def due_screen_schedules(now_iso: str) -> list:
    """Active screen schedules whose next_run_at is at or before ``now_iso``."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM screen_schedules WHERE status = 'active' "
            "AND next_run_at <= ? ORDER BY next_run_at",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_screen_schedule_fire(
    screen_schedule_id: int,
    *,
    success: bool,
    next_run_at: str,
    screen_run_id: Optional[str] = None,
    auto_disable_after: int = 3,
):
    """Update a screen schedule after a reconcile fire. On failure, increment
    fail_count and auto-disable when it reaches ``auto_disable_after``."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        ss = conn.execute(
            "SELECT fail_count FROM screen_schedules WHERE id = ?",
            (screen_schedule_id,),
        ).fetchone()
        if not ss:
            return
        if success:
            new_fail = 0
            new_status = "active"
        else:
            new_fail = ss["fail_count"] + 1
            new_status = "disabled" if new_fail >= auto_disable_after else "active"
        conn.execute(
            "UPDATE screen_schedules SET fail_count = ?, status = ?, "
            "last_run_at = ?, last_screen_run_id = ?, next_run_at = ?, "
            "updated_at = ? WHERE id = ?",
            (new_fail, new_status, now, screen_run_id, next_run_at, now,
             screen_schedule_id),
        )


# --- Paper trading ---

def ensure_default_paper_account(initial_cash: float = 10_000.0) -> dict:
    """Create the default paper-trading account if none exists. Returns the
    sole account (creates one if the table is empty, otherwise returns the
    first one). Most users only need a single virtual account."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM paper_accounts ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
        cur = conn.execute(
            "INSERT INTO paper_accounts (name, initial_cash, cash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("默认账户", initial_cash, initial_cash, now, now),
        )
        row = conn.execute(
            "SELECT * FROM paper_accounts WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_paper_account(account_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM paper_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row else None


def list_paper_accounts() -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM paper_accounts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def reset_paper_account(account_id: int,
                        initial_cash: Optional[float] = None) -> Optional[dict]:
    """Wipe positions + orders + nav snapshots for an account and reset cash.

    With ``initial_cash`` (>0) the account's starting capital (本金) is changed
    to that value and cash reset to it — lets the user run a small book (e.g.
    ¥10,000). Without it, the existing ``initial_cash`` is reused. Used when the
    user wants to start a fresh simulation."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        row = conn.execute(
            "SELECT initial_cash FROM paper_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not row:
            return None
        new_cash = (float(initial_cash) if initial_cash is not None and initial_cash > 0
                    else row["initial_cash"])
        conn.execute("DELETE FROM paper_positions WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM paper_orders WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM paper_nav WHERE account_id = ?", (account_id,))
        conn.execute(
            "UPDATE paper_accounts SET initial_cash = ?, cash = ?, updated_at = ? WHERE id = ?",
            (new_cash, new_cash, now, account_id),
        )
    return get_paper_account(account_id)


def adjust_paper_capital(account_id: int, delta: float) -> tuple[Optional[dict], Optional[str]]:
    """Inject or withdraw capital WITHOUT touching positions / orders.

    Adds ``delta`` to BOTH cash and initial_cash so the P&L baseline stays
    consistent (adding money is not a gain). This is the safe alternative to
    ``reset_paper_account`` for "加钱": holdings and order history are preserved.
    A negative delta withdraws; rejected if it would drive cash below zero.
    Returns ``(account_dict, None)`` or ``(None, error_msg)``.
    """
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        row = conn.execute(
            "SELECT initial_cash, cash FROM paper_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not row:
            return None, "account not found"
        new_cash = row["cash"] + delta
        if new_cash < -1e-9:
            return None, f"现金不足:当前可用 {row['cash']:.2f},无法减少 {abs(delta):.2f}"
        new_initial = max(0.0, row["initial_cash"] + delta)
        conn.execute(
            "UPDATE paper_accounts SET initial_cash = ?, cash = ?, updated_at = ? WHERE id = ?",
            (new_initial, new_cash, now, account_id),
        )
    return get_paper_account(account_id), None


def list_paper_positions(account_id: int) -> list:
    # Decorate each open position with its most recent BUY order's provenance
    # (source + analysis id + fill time) so the Holdings table can show "where
    # did this position come from" and link back to the triggering analysis —
    # the position row itself doesn't carry a source, only the orders do.
    with get_db() as conn:
        rows = conn.execute(
            "SELECT p.*, "
            "  o.source        AS last_buy_source, "
            "  o.source_analysis_id AS source_analysis_id, "
            "  o.filled_at     AS last_buy_at "
            "FROM paper_positions p "
            "LEFT JOIN paper_orders o ON o.id = ("
            "  SELECT id FROM paper_orders b "
            "  WHERE b.account_id = p.account_id AND b.ticker = p.ticker "
            "        AND b.action = 'buy' "
            "  ORDER BY b.filled_at DESC LIMIT 1) "
            "WHERE p.account_id = ? AND p.shares > 0 "
            "ORDER BY p.ticker",
            (account_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_paper_orders(account_id: int, limit: int = 200) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE account_id = ? "
            "ORDER BY filled_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_paper_nav(account_id: int, limit: int = 365) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_nav WHERE account_id = ? "
            "ORDER BY snapshot_date DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _is_a_share_ticker(ticker: str) -> bool:
    """Match A-share ticker shapes: 6-digit / .SH / .SS / .SZ / sh/sz prefix."""
    t = (ticker or "").upper().strip()
    if not t:
        return False
    digits = "".join(ch for ch in t if ch.isdigit())
    if len(digits) != 6:
        return False
    # Plain 6 digits (000001) or with SH/SS/SZ marker anywhere.
    if t == digits:
        return True
    return any(marker in t for marker in (".SH", ".SS", ".SZ", "SH.", "SZ."))


def _cst_date_of(iso_timestamp: str):
    """Parse an ISO timestamp (UTC or naive UTC) and return its CST calendar date.

    T+1 is defined on the **trading-day calendar**, which for A-share is
    Asia/Shanghai. We store timestamps as naive UTC ISO with a 'Z' marker;
    this helper converts back to CST so a buy at 22:00 CST followed by a
    sell at 09:00 CST next day correctly counts as 2 different days.
    """
    from datetime import timezone, timedelta as _td
    cst = timezone(_td(hours=8))
    dt = datetime.fromisoformat(iso_timestamp.rstrip("Z"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(cst).date()


def place_paper_order(
    *,
    account_id: int,
    ticker: str,
    asset_type: str,
    action: str,
    shares: float,
    price: float,
    source: str = "manual",
    source_analysis_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Atomically place a paper order, updating cash and positions.

    Returns ``(order_dict, None)`` on success or ``(None, error_msg)`` on a
    business-rule failure (insufficient cash for buy, insufficient shares
    for sell, A-share T+1 violation). Caller maps the error to a 400 response.

    A-share T+1: a sell on an A-share ticker is rejected if the most recent
    buy of that ticker happened on the same CST trading day. Non-A-share
    (US / HK) tickers skip this check.
    """
    now = datetime.utcnow().isoformat() + "Z"
    ticker = ticker.strip().upper()
    action = action.lower()
    if action not in ("buy", "sell"):
        return None, "action must be 'buy' or 'sell'"
    if shares <= 0 or price <= 0:
        return None, "shares and price must be > 0"

    # A-share T+1 check, runs before we touch any rows.
    if action == "sell" and _is_a_share_ticker(ticker):
        with get_db() as conn:
            row = conn.execute(
                "SELECT MAX(filled_at) FROM paper_orders "
                "WHERE account_id = ? AND ticker = ? AND action = 'buy'",
                (account_id, ticker),
            ).fetchone()
        last_buy = row[0] if row else None
        if last_buy:
            try:
                last_buy_date = _cst_date_of(last_buy)
                today_cst = _cst_date_of(now)
                if last_buy_date >= today_cst:
                    return None, (
                        f"A 股 T+1 限制:{ticker} 今日({last_buy_date})刚买入,"
                        f"按 A 股交易规则需到下一个交易日才能卖出。"
                    )
            except Exception:
                # Best-effort: if timestamp parsing fails, let it through
                # rather than blocking a legitimate sell.
                pass

    with get_db() as conn:
        acct = conn.execute(
            "SELECT * FROM paper_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not acct:
            return None, "account not found"
        pos = conn.execute(
            "SELECT * FROM paper_positions WHERE account_id = ? AND ticker = ?",
            (account_id, ticker),
        ).fetchone()

        if action == "buy":
            cost = shares * price
            if cost > acct["cash"] + 1e-9:
                return None, f"现金不足: 需 {cost:.2f}, 可用 {acct['cash']:.2f}"
            # Update cash and position (upsert with weighted-avg cost basis).
            new_cash = acct["cash"] - cost
            conn.execute(
                "UPDATE paper_accounts SET cash = ?, updated_at = ? WHERE id = ?",
                (new_cash, now, account_id),
            )
            if pos:
                total_shares = pos["shares"] + shares
                new_cost = (pos["shares"] * pos["avg_cost"] + cost) / total_shares
                conn.execute(
                    "UPDATE paper_positions SET shares = ?, avg_cost = ?, updated_at = ? "
                    "WHERE id = ?",
                    (total_shares, new_cost, now, pos["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO paper_positions (account_id, ticker, asset_type, "
                    "shares, avg_cost, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, ticker, asset_type, shares, price, now, now),
                )
        else:  # sell
            if not pos or pos["shares"] < shares - 1e-9:
                have = pos["shares"] if pos else 0
                return None, f"持仓不足: 需 {shares}, 持有 {have}"
            proceeds = shares * price
            new_cash = acct["cash"] + proceeds
            conn.execute(
                "UPDATE paper_accounts SET cash = ?, updated_at = ? WHERE id = ?",
                (new_cash, now, account_id),
            )
            new_shares = pos["shares"] - shares
            if new_shares <= 1e-9:
                conn.execute("DELETE FROM paper_positions WHERE id = ?", (pos["id"],))
            else:
                conn.execute(
                    "UPDATE paper_positions SET shares = ?, updated_at = ? WHERE id = ?",
                    (new_shares, now, pos["id"]),
                )

        cur = conn.execute(
            "INSERT INTO paper_orders (account_id, ticker, asset_type, action, shares, "
            "price, source, source_analysis_id, notes, filled_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, ticker, asset_type, action, shares, price, source,
             source_analysis_id, notes, now, now),
        )
        order = conn.execute(
            "SELECT * FROM paper_orders WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(order), None


def upsert_paper_nav(
    account_id: int,
    snapshot_date: str,
    cash: float,
    positions_value: float,
):
    """Insert or replace the NAV snapshot for ``snapshot_date``.

    Portable upsert (SELECT-then-UPDATE/INSERT) instead of SQLite's
    ``ON CONFLICT`` so it works identically on MySQL. The (account_id,
    snapshot_date) UNIQUE constraint guarantees at most one row per day.
    """
    now = datetime.utcnow().isoformat() + "Z"
    total = cash + positions_value
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM paper_nav WHERE account_id = ? AND snapshot_date = ?",
            (account_id, snapshot_date),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE paper_nav SET cash = ?, positions_value = ?, total_value = ? "
                "WHERE account_id = ? AND snapshot_date = ?",
                (cash, positions_value, total, account_id, snapshot_date),
            )
        else:
            conn.execute(
                "INSERT INTO paper_nav (account_id, snapshot_date, cash, positions_value, "
                "total_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, snapshot_date, cash, positions_value, total, now),
            )


# --- Backtesting ---

def create_backtest_run(
    *,
    name: str,
    signal_source: str,
    source_config: dict,
    tickers: Optional[list[str]],
    benchmark: Optional[str],
    start_date: str,
    end_date: str,
    initial_cash: float,
    sizing_mode: str,
    sizing_config: dict,
    confidence_floor: Optional[float],
) -> dict:
    """Insert a pending backtest run; returns the new row dict."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO backtest_runs (name, signal_source, source_config, "
            "tickers, benchmark, start_date, end_date, initial_cash, sizing_mode, "
            "sizing_config, confidence_floor, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (name, signal_source, json.dumps(source_config),
             json.dumps(tickers) if tickers else None, benchmark,
             start_date, end_date, initial_cash, sizing_mode,
             json.dumps(sizing_config), confidence_floor, now),
        )
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (rid,)).fetchone()
    return dict(row)


def list_backtest_runs(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, signal_source, tickers, benchmark, start_date, "
            "end_date, initial_cash, status, metrics_json, final_total, "
            "created_at, completed_at FROM backtest_runs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_backtest_run(run_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def update_backtest_status(
    run_id: int,
    *,
    status: str,
    metrics_json: Optional[str] = None,
    warnings_text: Optional[str] = None,
    final_cash: Optional[float] = None,
    final_total: Optional[float] = None,
    error_msg: Optional[str] = None,
):
    now = datetime.utcnow().isoformat() + "Z"
    fields = ["status = ?"]
    params: list = [status]
    if metrics_json is not None:
        fields.append("metrics_json = ?")
        params.append(metrics_json)
    if warnings_text is not None:
        fields.append("warnings = ?")
        params.append(warnings_text)
    if final_cash is not None:
        fields.append("final_cash = ?")
        params.append(final_cash)
    if final_total is not None:
        fields.append("final_total = ?")
        params.append(final_total)
    if error_msg is not None:
        fields.append("error_msg = ?")
        params.append(error_msg)
    if status in ("complete", "failed"):
        fields.append("completed_at = ?")
        params.append(now)
    params.append(run_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE backtest_runs SET {', '.join(fields)} WHERE id = ?", params,
        )


def insert_backtest_trades(run_id: int, trades: list[dict]):
    if not trades:
        return
    rows = [
        (
            run_id,
            t["timestamp"],
            t["ticker"],
            t["action"],
            t["shares"],
            t["price"],
            t["fee"],
            t["realised_pnl"],
            (t.get("metadata") or {}).get("analysis_id"),
            json.dumps(t.get("metadata") or {}),
        )
        for t in trades
    ]
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO backtest_trades (run_id, timestamp, ticker, action, "
            "shares, price, fee, realised_pnl, source_analysis_id, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def insert_backtest_nav(run_id: int, nav_curve: list[tuple]):
    """``nav_curve`` is ``[(datetime, total_value, benchmark_value), ...]``."""
    if not nav_curve:
        return
    rows = [
        (run_id, d.isoformat(), total, bench)
        for d, total, bench in nav_curve
    ]
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO backtest_nav (run_id, snapshot_date, total_value, "
            "benchmark_value) VALUES (?, ?, ?, ?)",
            rows,
        )


def get_backtest_nav(run_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT snapshot_date, total_value, benchmark_value "
            "FROM backtest_nav WHERE run_id = ? ORDER BY snapshot_date",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_backtest_trades(run_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY timestamp",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_backtest_run(run_id: int):
    with get_db() as conn:
        # ON DELETE CASCADE on trades/nav handles the rest.
        conn.execute("DELETE FROM backtest_trades WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM backtest_nav WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM backtest_runs WHERE id = ?", (run_id,))


# --- App settings (Settings-page config: API keys + TRADINGAGENTS_* settings) ---

def get_app_settings() -> dict:
    """All persisted settings as ``{env_var_name: value}``."""
    with get_db() as conn:
        rows = conn.execute("SELECT skey, svalue FROM app_settings").fetchall()
    return {r["skey"]: r["svalue"] for r in rows}


def set_app_setting(skey: str, svalue: str):
    """Upsert one setting keyed by its environment-variable name."""
    now = datetime.utcnow().isoformat() + "Z"
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM app_settings WHERE skey = ?", (skey,)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE app_settings SET svalue = ?, updated_at = ? WHERE skey = ?",
                (svalue, now, skey),
            )
        else:
            conn.execute(
                "INSERT INTO app_settings (skey, svalue, updated_at) VALUES (?, ?, ?)",
                (skey, svalue, now),
            )


def delete_app_setting(skey: str):
    with get_db() as conn:
        conn.execute("DELETE FROM app_settings WHERE skey = ?", (skey,))


def load_app_settings_into_environ() -> int:
    """Copy persisted settings into ``os.environ`` at startup.

    The DB is the source of truth for UI-managed config, so these OVERWRITE any
    seed values from .env / panel env vars (which only matter on a first boot
    before anything has been saved). Must run before the LLM clients / scheduler
    read their keys, and before ``settings.reload_overrides_from_env``. Returns
    the number of settings loaded.
    """
    settings = get_app_settings()
    for k, v in settings.items():
        if v is None:
            continue
        os.environ[k] = v
    return len(settings)


def checkpoint_sqlite():
    """Best-effort WAL checkpoint on shutdown — SQLite only, no-op otherwise.

    Truncating the WAL on a clean stop keeps the next startup's recovery short
    (a large un-checkpointed WAL is part of why the UI looked momentarily empty
    right after a restart). Non-SQLite backends manage their own logs.
    """
    try:
        engine = _get_engine()
        if engine.dialect.name != "sqlite":
            return
        with engine.begin() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:  # pragma: no cover - shutdown best-effort
        logger.warning("WAL checkpoint on shutdown failed", exc_info=True)
