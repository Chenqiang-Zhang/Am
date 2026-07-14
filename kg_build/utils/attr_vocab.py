"""
Growable (attr_type, value) vocabulary database shared by extract_product_attributes.py
and extract_review_mentions.py.

attr_vocab.yaml is seeded once by propose_attr_vocab.py (rule-based details keys +
an LLM proposal from a product/review sample, reviewed by hand), then GROWS during
real extraction: the LLM is told to reuse an existing (attr_type, value) pair
whenever one fits, and may introduce a new one only when truly necessary. Whenever
that happens, the new pair is added to this same database — both the "new value for
an existing attr_type" case and the "brand new attr_type" case are handled the same
way (append/create). Growth is persisted immediately so later batches in the SAME
run (not just future runs) see it and can reuse it too, which is what actually fixes
the original bug (a per-run snapshot that never grew across ~11k independent batch
calls). Growth is meant to be rare, not the default path, and is reported at the end
of a run (added_types/added_values) so a human can review and, if needed, prune or
merge entries afterward.

  attr_vocab.yaml:
    attr_types:
      - name: game_genre
        description: "The genre of the game"
        values: [rpg, fps, racing, puzzle, platformer]
      - name: reviewer_comment
        description: "General free-text remark"
        values: []   # empty = open value; only attr_type membership matters

  detail_key_map.yaml (separate, static, human-reviewed — does not grow at runtime):
    "Genre": game_genre
    "Item model number": null   # null = drop this metadata key entirely
"""
from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from utils.text_utils import normalize_attr_type, normalize_value


def load_attr_vocab_entries(path: Path) -> list[dict[str, Any]]:
    """Return [{"name": ..., "description": ..., "values": [...]}, ...] in file order.
    Used to seed a GrowableVocab and by propose_attr_vocab.py when writing the draft."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("attr_types", []) or []
    return [
        {
            "name": str(e.get("name", "")).strip(),
            "description": str(e.get("description", "")).strip(),
            "values": [str(v).strip() for v in (e.get("values") or []) if str(v).strip()],
        }
        for e in entries
        if e.get("name")
    ]


def load_detail_key_map(path: Path) -> dict[str, str | None]:
    """Return {raw details-dict key: canonical attr_type or None (= drop)}. Static —
    reviewed by hand once via propose_attr_vocab.py, does not grow at runtime."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return {str(k): (str(v) if v else None) for k, v in data.items()}


class GrowableVocab:
    """Thread-safe (attr_type, value) vocabulary. Seeded from attr_vocab.yaml at
    construction; resolve() looks up a pair and, when allow_growth=True and the
    pair isn't already known, adds it (new value under an existing attr_type, or a
    brand new attr_type) instead of rejecting it. Call save() to persist growth —
    do this after every processed batch so a crash doesn't lose it, and later
    batches in the same run see the update via prompt_text()."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # name -> {"description": str, "values": set[str] | None}  (None = open value)
        self._entries: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._added_types: "Counter[str]" = Counter()
        self._added_values: "Counter[str]" = Counter()
        for e in load_attr_vocab_entries(path):
            name = normalize_attr_type(e["name"])
            if name in self._entries:
                continue
            values = {normalize_value(v) for v in e["values"]} if e["values"] else None
            self._entries[name] = {"description": e["description"], "values": values}
            self._order.append(name)

    def resolve(self, attr_type: str, value: str, allow_growth: bool) -> tuple[str, str] | None:
        """Return (normalized_attr_type, normalized_value) if the pair is known, or
        if allow_growth=True and it just got added. Returns None only for malformed
        (empty) input — there is no more "out of vocabulary, drop it" case once
        growth is allowed; that's the whole point."""
        t = normalize_attr_type(attr_type)
        v = normalize_value(value)
        if not t or not v:
            return None
        with self._lock:
            entry = self._entries.get(t)
            if entry is None:
                if not allow_growth:
                    return None
                self._entries[t] = {"description": "", "values": {v}}
                self._order.append(t)
                self._added_types[t] += 1
                return t, v
            if entry["values"] is None:  # open attr_type: any value already accepted
                return t, v
            if v in entry["values"]:
                return t, v
            if not allow_growth:
                return None
            entry["values"].add(v)
            self._added_values[f"{t}={v}"] += 1
            return t, v

    def type_count(self) -> int:
        with self._lock:
            return len(self._order)

    def prompt_text(self) -> str:
        """Render the current vocabulary (name, description, allowed values) for the
        LLM system prompt. Call this fresh for every LLM call (not once per run) so
        growth from earlier batches in the same run is visible to later ones."""
        with self._lock:
            lines: list[str] = []
            for name in self._order:
                e = self._entries[name]
                head = f"  {name}: {e['description']}" if e["description"] else f"  {name}"
                lines.append(head)
                if e["values"] is not None:
                    lines.append(f"    allowed values: {', '.join(sorted(e['values']))}")
            return "\n".join(lines)

    def growth_summary(self) -> str | None:
        """One-line human-readable summary of what got added this run, or None if
        nothing grew."""
        with self._lock:
            n_types, n_values = sum(self._added_types.values()), sum(self._added_values.values())
            if not n_types and not n_values:
                return None
            parts = []
            if n_types:
                parts.append(f"{n_types} new attr_type(s): {', '.join(self._added_types)}")
            if n_values:
                top = ", ".join(list(self._added_values)[:15])
                parts.append(f"{n_values} new value(s) under existing attr_types: {top}")
            return " | ".join(parts)

    def save(self) -> None:
        with self._lock:
            data = {
                "attr_types": [
                    {
                        "name": name,
                        "description": self._entries[name]["description"],
                        "values": sorted(self._entries[name]["values"]) if self._entries[name]["values"] is not None else [],
                    }
                    for name in self._order
                ]
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
