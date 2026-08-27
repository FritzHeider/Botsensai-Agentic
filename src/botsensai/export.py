"""Data export utilities for Botsensai SQLite store.

Exports historical launches, labelled outcomes, metric evaluations, and paper
trades to CSV, JSON Lines, or Parquet for analysis in pandas and Jupyter.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console

from botsensai.config import Settings

console = Console()

SUPPORTED_TABLES = ["launches", "outcomes", "market_snapshots", "paper_orders", "metric_values"]


def _fetch_table_records(
    db_path: Path, table_clean: str, limit: int | None
) -> tuple[list[str], list[sqlite3.Row]]:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if table_clean not in SUPPORTED_TABLES:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_clean,))
        if not cursor.fetchone():
            conn.close()
            raise ValueError(f"Unknown table '{table_clean}'. Available: {', '.join(SUPPORTED_TABLES)}")

    query = f"SELECT * FROM {table_clean}"
    if limit:
        query += f" LIMIT {int(limit)}"

    cursor.execute(query)
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    conn.close()
    return columns, rows


def _write_csv(dest: Path, columns: Sequence[str], rows: Sequence[sqlite3.Row]) -> None:
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[col] for col in columns])


def _write_json(dest: Path, rows: Sequence[sqlite3.Row], jsonl: bool = False) -> None:
    with dest.open("w", encoding="utf-8") as fh:
        if jsonl:
            for row in rows:
                fh.write(json.dumps(dict(row), default=str) + "\n")
        else:
            data = [dict(row) for row in rows]
            json.dump(data, fh, indent=2, default=str)


def _write_parquet(dest: Path, rows: Sequence[sqlite3.Row]) -> None:
    import pandas as pd

    data = [dict(row) for row in rows]
    df = pd.DataFrame(data)
    df.to_parquet(dest, index=False)


def export_data(
    settings: Settings,
    table: str,
    output_format: str = "csv",
    out_file: str | Path | None = None,
    limit: int | None = None,
) -> Path:
    """Export SQLite table records to the requested format."""
    table_clean = table.strip().lower()
    columns, rows = _fetch_table_records(settings.path(settings.db_path), table_clean, limit)

    dest = Path(out_file) if out_file else Path(f"data/{table_clean}.{output_format}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "csv":
        _write_csv(dest, columns, rows)
    elif output_format == "json":
        _write_json(dest, rows, jsonl=False)
    elif output_format == "jsonl":
        _write_json(dest, rows, jsonl=True)
    elif output_format == "parquet":
        _write_parquet(dest, rows)
    else:
        raise ValueError(f"Unsupported format '{output_format}'. Use csv, json, jsonl, or parquet.")

    return dest
