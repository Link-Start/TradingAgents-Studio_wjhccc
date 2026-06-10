"""One-shot data migration between TradingAgents-Studio databases.

Copies every row from a *source* database (typically the legacy SQLite file)
into a *target* database (e.g. MySQL) so switching backends doesn't lose your
history. It is:

  * **Idempotent** — rows whose primary key already exists in the target are
    skipped, so re-running is safe and resumable.
  * **Non-destructive** — the source is opened read-only-style (we only SELECT)
    and the target is never truncated; only missing rows are inserted.
  * **Schema-creating** — the target's tables are created first via the shared
    MetaData (``create_all``, IF NOT EXISTS), so a fresh empty MySQL works.

Tables are processed in foreign-key dependency order (``analyses`` before
``agent_events``, ``paper_accounts`` before ``paper_*``, etc.) and explicit
primary keys are preserved so cross-table references stay valid.

Usage
-----
    # SQLite → MySQL (target taken from $TRADINGAGENTS_DB_URL when omitted)
    python -m web.backend.migrate_db \
        --source sqlite:///~/.tradingagents/web_state.db \
        --target mysql+pymysql://user:pass@host:3306/tradingagents?charset=utf8mb4

If ``--source`` is omitted it defaults to the legacy SQLite file
(``TRADINGAGENTS_WEB_DB`` or ``~/.tradingagents/web_state.db``). If ``--target``
is omitted it defaults to ``$TRADINGAGENTS_DB_URL``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import MetaData, create_engine, insert, select

from .database import _DB_PATH, _METADATA


def _default_source_url() -> str:
    return "sqlite:///" + Path(_DB_PATH).as_posix()


def migrate(source_url: str, target_url: str) -> dict:
    """Copy all rows source → target idempotently. Returns per-table counts."""
    src = create_engine(source_url, future=True)
    tgt = create_engine(target_url, future=True)

    # 1) Ensure the full schema exists on the target (never drops).
    _METADATA.create_all(tgt)

    # 2) Reflect the source so we tolerate schema drift (missing/extra columns).
    src_meta = MetaData()
    src_meta.reflect(bind=src)

    summary: dict[str, dict] = {}
    with src.connect() as sconn, tgt.begin() as tconn:
        # sorted_tables is FK-dependency order, so parents are inserted first.
        for table in _METADATA.sorted_tables:
            name = table.name
            if name not in src_meta.tables:
                summary[name] = {"inserted": 0, "skipped": 0, "note": "absent in source"}
                continue
            src_table = src_meta.tables[name]
            cols = [c.name for c in table.columns if c.name in src_table.columns]
            pk = list(table.primary_key.columns)[0]

            existing = {row[0] for row in tconn.execute(select(table.c[pk.name]))}
            src_rows = sconn.execute(
                select(*[src_table.c[c] for c in cols])
            ).mappings().all()

            new_rows = [
                {c: row[c] for c in cols}
                for row in src_rows
                if row[pk.name] not in existing
            ]
            if new_rows:
                # Chunk to keep statements a sane size on large tables.
                for i in range(0, len(new_rows), 500):
                    tconn.execute(insert(table), new_rows[i:i + 500])
            summary[name] = {
                "inserted": len(new_rows),
                "skipped": len(src_rows) - len(new_rows),
            }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate Studio DB data between backends.")
    parser.add_argument("--source", default=None,
                        help="Source SQLAlchemy URL (default: legacy SQLite file).")
    parser.add_argument("--target", default=None,
                        help="Target SQLAlchemy URL (default: $TRADINGAGENTS_DB_URL).")
    args = parser.parse_args(argv)

    source_url = args.source or _default_source_url()
    target_url = args.target or os.getenv("TRADINGAGENTS_DB_URL")
    if not target_url:
        parser.error("no --target given and TRADINGAGENTS_DB_URL is not set")
    if source_url == target_url:
        parser.error("source and target are the same database")

    # Expand a leading ~ in a sqlite:/// path for convenience.
    if source_url.startswith("sqlite:///~"):
        source_url = "sqlite:///" + Path(source_url[len("sqlite:///"):]).expanduser().as_posix()

    print(f"Source: {source_url}")
    print(f"Target: {target_url}")
    summary = migrate(source_url, target_url)

    total_new = sum(s["inserted"] for s in summary.values())
    print("\nPer-table results:")
    for name, s in summary.items():
        note = f"  ({s['note']})" if s.get("note") else ""
        print(f"  {name:18} +{s['inserted']:>6} inserted   {s['skipped']:>6} skipped{note}")
    print(f"\nDone. {total_new} new rows migrated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
