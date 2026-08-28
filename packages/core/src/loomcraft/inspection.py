"""Bounded, read-only table inspection helpers.

This optional utility covers the common CSV/TSV/JSON/SQLite case without
pulling a data-science stack into the core package. Hosts can replace it with a
stronger adapter (DuckDB, pandas, or a remote data service).
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, List, Optional


class InspectionError(ValueError):
    pass


def _safe_file(path: os.PathLike, max_bytes: int) -> Path:
    value = Path(path)
    try:
        resolved = value.resolve(strict=True)
        info = resolved.stat()
    except (FileNotFoundError, OSError) as exc:
        raise InspectionError("source file is unavailable") from exc
    if not resolved.is_file() or value.is_symlink():
        raise InspectionError("source must be a regular file")
    if info.st_size > max_bytes:
        raise InspectionError("source exceeds inspection size limit")
    return resolved


def _column_summary(rows: List[Any], columns: List[str]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for column in columns:
        values = [row.get(column) for row in rows if isinstance(row, dict) and row.get(column) not in (None, "")]
        kinds = sorted({type(value).__name__ for value in values})
        result.append({"name": column, "non_empty": len(values), "types": kinds})
    return result


def inspect_table_file(path: os.PathLike, *, source_ref: Optional[str] = None, requested_format: str = "auto", max_rows: int = 100, encoding: str = "utf-8", delimiter: Optional[str] = None, table: Optional[str] = None, max_bytes: int = 16 * 1024 * 1024) -> Dict[str, Any]:
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1 or max_rows > 1000:
        raise InspectionError("max_rows must be between 1 and 1000")
    if len(encoding) > 100 or any(ord(char) < 32 for char in encoding):
        raise InspectionError("encoding is invalid")
    file_path = _safe_file(path, max_bytes)
    suffix = file_path.name.lower()
    fmt = requested_format.lower()
    if fmt == "auto":
        if suffix.endswith((".jsonl", ".ndjson")):
            fmt = "jsonl"
        elif suffix.endswith(".json"):
            fmt = "json"
        elif suffix.endswith((".tsv", ".tab")):
            fmt = "tsv"
        elif suffix.endswith((".db", ".sqlite", ".sqlite3")):
            fmt = "sqlite"
        else:
            fmt = "csv"
    if fmt in {"csv", "tsv", "delimited"}:
        return _inspect_delimited(file_path, source_ref, max_rows, encoding, "\t" if fmt == "tsv" else delimiter)
    if fmt in {"json", "jsonl", "ndjson"}:
        return _inspect_json(file_path, source_ref, max_rows, encoding, fmt)
    if fmt in {"sqlite", "db"}:
        return _inspect_sqlite(file_path, source_ref, max_rows, table)
    if fmt in {"xlsx", "xlsm", "xls", "xlsb", "ods"}:
        raise InspectionError("spreadsheet inspection requires a host adapter")
    raise InspectionError("unsupported table format %r" % requested_format)


def _base(source_ref: Optional[str], path: Path, rows: List[Dict[str, Any]], columns: List[str], truncated: bool) -> Dict[str, Any]:
    return {
        "source": {"requested": source_ref or path.name, "filename": path.name},
        "shape": {"sample_rows": len(rows), "columns": len(columns)},
        "columns": _column_summary(rows, columns),
        "rows": rows,
        "truncated": truncated,
    }


def _inspect_delimited(path: Path, source_ref: Optional[str], max_rows: int, encoding: str, delimiter: Optional[str]) -> Dict[str, Any]:
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            chosen = delimiter or (csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter if sample else ",")
            reader = csv.DictReader(handle, delimiter=chosen)
            columns = [str(item) for item in (reader.fieldnames or [])]
            rows: List[Dict[str, Any]] = []
            for row in reader:
                if len(rows) >= max_rows:
                    return _base(source_ref, path, rows, columns, True)
                rows.append({str(key): value for key, value in row.items() if key is not None})
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InspectionError("delimited table could not be read") from exc
    return _base(source_ref, path, rows, columns, False)


def _inspect_json(path: Path, source_ref: Optional[str], max_rows: int, encoding: str, fmt: str) -> Dict[str, Any]:
    try:
        if fmt in {"jsonl", "ndjson"}:
            rows: List[Any] = []
            truncated = False
            with path.open("r", encoding=encoding) as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    if len(rows) >= max_rows:
                        truncated = True
                        break
                    value = json.loads(line)
                    rows.append(value if isinstance(value, dict) else {"value": value})
        else:
            value = json.loads(path.read_text(encoding=encoding))
            if isinstance(value, list):
                truncated = len(value) > max_rows
                rows = [item if isinstance(item, dict) else {"value": item} for item in value[:max_rows]]
            elif isinstance(value, dict):
                rows = [value]
                truncated = False
            else:
                rows = [{"value": value}]
                truncated = False
    except (OSError, UnicodeError, ValueError) as exc:
        raise InspectionError("JSON table could not be read") from exc
    columns = sorted({key for row in rows if isinstance(row, dict) for key in row})
    return _base(source_ref, path, rows, columns, truncated)


def _inspect_sqlite(path: Path, source_ref: Optional[str], max_rows: int, table: Optional[str]) -> Dict[str, Any]:
    connection = None
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % quote(path.as_posix(), safe="/"), uri=True)
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        selected = table or (tables[0] if tables else None)
        if selected is None or selected not in tables:
            raise InspectionError("SQLite table is not available")
        # Table names cannot be bound; select only names discovered from the
        # sqlite_master query and quote double quotes defensively.
        quoted = '"%s"' % selected.replace('"', '""')
        cursor = connection.execute("SELECT * FROM %s LIMIT ?" % quoted, (max_rows + 1,))
        columns = [item[0] for item in cursor.description or []]
        values = cursor.fetchmany(max_rows + 1)
        rows = [dict(zip(columns, row)) for row in values[:max_rows]]
        return {**_base(source_ref, path, rows, columns, len(values) > max_rows), "tables": tables, "table": selected}
    except InspectionError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise InspectionError("SQLite table could not be read") from exc
    finally:
        if connection is not None:
            connection.close()
