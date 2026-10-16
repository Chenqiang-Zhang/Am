import ast
import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from utils import settings


def graph_data_dir(graph_name: str) -> Path:
    directory = settings.ROOT / "data" / graph_name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_jsonl(path: Path, records: Iterable[dict[str, Any]], append: bool = False) -> None:
    with path.open("a" if append else "w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                yield json.loads(line)


def read_gzip_records(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield ast.literal_eval(line)
