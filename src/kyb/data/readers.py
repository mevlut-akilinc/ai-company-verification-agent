"""Streaming readers for the raw bulk files (never load a whole file into memory)."""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _open_members(path: Path, suffixes: tuple[str, ...]) -> Iterator[io.TextIOWrapper]:
    """Yield text streams for matching members of a zip, or the file itself if not zipped."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith("/") or not name.lower().endswith(suffixes):
                    continue
                with zf.open(name) as raw:
                    yield io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    else:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            yield fh


def iter_jsonl(paths: Iterable[Path]) -> Iterator[dict]:
    for p in paths:
        for fh in _open_members(p, (".json", ".jsonl", ".txt")):
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def iter_csv(paths: Iterable[Path]) -> Iterator[dict]:
    """CSV rows as dicts. Header names are stripped: Companies House headers
    contain leading spaces (e.g. ' CompanyNumber')."""
    for p in paths:
        for fh in _open_members(p, (".csv",)):
            reader = csv.reader(fh)
            try:
                header = [h.strip().lstrip("﻿") for h in next(reader)]
            except StopIteration:
                continue
            for row in reader:
                if len(row) < len(header):
                    row = row + [""] * (len(header) - len(row))
                yield dict(zip(header, row))


def require_columns(row: dict, columns: Iterable[str], source: str) -> None:
    missing = [c for c in columns if c not in row]
    if missing:
        raise KeyError(
            f"{source}: expected columns {missing} not found. "
            f"Available (first 15): {list(row)[:15]}. The upstream format may have changed; "
            f"update the column constants in kyb/data/subset.py."
        )


def iter_psc(paths: Iterable[Path]) -> Iterator[tuple[str, dict]]:
    """Yield (company_number, data) from the PSC snapshot. Skips the trailing totals record."""
    for rec in iter_jsonl(paths):
        cn = rec.get("company_number")
        data = rec.get("data")
        if cn and isinstance(data, dict):
            yield cn, data


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)
