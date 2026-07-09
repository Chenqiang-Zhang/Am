"""
Shared CSV/JSONL read-write helpers for the KG-CSV build scripts
(build_base_graph.py, extract_product_attributes.py, extract_review_mentions.py,
canonicalize_attributes.py, build_attribute_graph.py).
"""
from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Iterator


def read_jsonl_gz(path: Path, max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if max_rows is not None and i >= max_rows:
                break
            line = line.strip()
            if line:
                yield json.loads(line)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Read a plain (non-gzipped) JSONL file, skipping any malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
            count += 1
    return count


def load_done_ids(output_path: Path, id_field: str) -> set[str]:
    """Collect already-written record IDs from a resumable JSONL output file."""
    done: set[str] = set()
    for row in read_jsonl(output_path):
        if rid := row.get(id_field):
            done.add(str(rid))
    return done
