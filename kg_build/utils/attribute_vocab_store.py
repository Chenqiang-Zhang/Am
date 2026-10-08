"""Shared, runtime-growable (attr_type, value) vocabulary used by both
extract_product_attributes.py and extract_review_mentions.py so that both
scripts see — and grow — the same known-attribute database over the course
of a run (and across future runs, since growth is persisted to YAML).

LLM-proposed new pairs are added only when the LLM itself judges
necessity >= NEW_PAIR_MIN_NECESSITY; a passing pair is persisted to the YAML
file as soon as its batch finishes, so later batches — in this same run and
in future runs — load the updated file and treat it as already known.

VOCAB_PATH (attribute_vocab.yaml) is the only file the pipeline reads from
and matches against. Two read-only reference files are kept alongside it so
growth can be reviewed later without diffing VOCAB_PATH by hand:
  - SEED_PATH (attribute_vocab_seed.yaml): frozen snapshot of the
    hand-defined initial vocabulary, never written to by this module.
  - GROWTH_PATH (attribute_vocab_growth.yaml): append-only log of just the
    pairs that were newly accepted at runtime, written alongside VOCAB_PATH.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

from utils.text_utils import normalize_attr_type, normalize_value


_ONTOLOGY_DIR = Path(__file__).resolve().parent.parent / "ontology"
VOCAB_PATH = _ONTOLOGY_DIR / "attribute_vocab.yaml"
SEED_PATH = _ONTOLOGY_DIR / "attribute_vocab_seed.yaml"
GROWTH_PATH = _ONTOLOGY_DIR / "attribute_vocab_growth.yaml"

# LLM が新規 (attr_type, value) を提案した場合の採用ゲート。necessity のみで判定する。
NEW_PAIR_MIN_NECESSITY = 0.9

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _token(value: str) -> str:
    """空白・ハイフン・アンダースコアの表記ゆれを吸収した比較用トークン
    （例: "playstation 4" と "playstation_4" を同一視する）。永続化される
    値そのものの表記には使わない — マッチ判定にのみ使う。"""
    return _TOKEN_RE.sub("_", value.lower()).strip("_")


def load_vocab(path: Path = VOCAB_PATH) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {t: list(v.get("values", [])) for t, v in (data.get("attributes") or {}).items()}


class AttributeVocabStore:
    """attribute_vocab.yaml の読み書き窓口。並行バッチからの読み書きが
    競合しないようロックで直列化する。"""

    def __init__(self, path: Path = VOCAB_PATH, growth_path: Path = GROWTH_PATH) -> None:
        self._path = path
        self._growth_path = growth_path
        self._lock = threading.Lock()
        self._attrs: dict[str, list[str]] = load_vocab(path)

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {t: list(vs) for t, vs in self._attrs.items()}

    def try_add(self, attr_type: str, value: str, necessity: float) -> str | None:
        """既存(attr_type, value)に表記ゆれ込みで一致すれば、その既存の表記を
        返す（ゲート不要）。一致しない場合は necessity がゲートを満たしたときだけ
        新規追加してYAMLに永続化し、そのvalueを返す。満たさなければ None。

        新規追加時は _token() と同じ規則で空白・記号をアンダースコアに統一した
        形で保存する — 手動シード済みの値（playstation_4 等）と表記スタイルを
        揃えるため（そのままだと "disney infinity" のようにスペース付きで
        保存され、シードのアンダースコア区切りと混在してしまう）。
        """
        with self._lock:
            existing = self._resolve_locked(attr_type, value)
            if existing is not None:
                return existing
            if necessity < NEW_PAIR_MIN_NECESSITY:
                return None
            canonical = _token(value)
            if not canonical:
                return None
            bucket = self._attrs.setdefault(attr_type, [])
            bucket.append(canonical)
            self._persist()
            self._append_growth_locked(attr_type, canonical)
            return canonical

    def contains(self, attr_type: str, value: str) -> bool:
        with self._lock:
            return self._resolve_locked(attr_type, value) is not None

    def _resolve_locked(self, attr_type: str, value: str) -> str | None:
        """呼び出し元が既にロックを保持している前提。表記ゆれを吸収して
        一致する既存値があればそれを返す。"""
        token = _token(value)
        for existing in self._attrs.get(attr_type, []):
            if _token(existing) == token:
                return existing
        return None

    def _persist(self) -> None:
        data: dict[str, Any] = {}
        if self._path.exists():
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        existing = data.get("attributes") or {}
        for attr_type, values in self._attrs.items():
            entry = existing.setdefault(attr_type, {})
            existing_values = entry.setdefault("values", [])
            for v in values:
                if v not in existing_values:
                    existing_values.append(v)
        data["attributes"] = existing
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8",
        )

    def _append_growth_locked(self, attr_type: str, value: str) -> None:
        """呼び出し元が既にロックを保持している前提。新規採用された1件を
        attribute_vocab_growth.yaml に追記する（VOCAB_PATH のマッチングには
        使われない、後から見返すための参照専用ログ）。"""
        data: dict[str, Any] = {}
        if self._growth_path.exists():
            data = yaml.safe_load(self._growth_path.read_text(encoding="utf-8")) or {}
        existing = data.get("attributes") or {}
        entry = existing.setdefault(attr_type, {})
        values = entry.setdefault("values", [])
        if value not in values:
            values.append(value)
        data["attributes"] = existing
        self._growth_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8",
        )


def canonicalize_and_gate(
    raw: dict[str, Any], store: AttributeVocabStore, min_confidence: float,
) -> tuple[str, str, float] | None:
    """LLMが返した1件の attr_type/value/confidence/necessity を正規化し、共有
    語彙に照らしてゲートする（extract_product_attributes.py と
    extract_review_mentions.py の共通ロジック）。

    既知の (attr_type, value) はそのまま採用。未知のものは store.try_add() の
    necessity 判定に通った場合だけ採用し、同時にその場でYAMLへ永続化される。
    confidence が min_confidence 未満、または最終的にゲートを通らない場合は
    None を返す。成功時は (attr_type, resolved_value, confidence) を返す —
    呼び出し元は evidence/sentiment など自分固有のフィールドを付け足して
    最終レコードを組み立てる。
    """
    t = normalize_attr_type(str(raw.get("attr_type", "")))
    v = normalize_value(str(raw.get("value", "")))
    if not t or not v:
        return None
    confidence = float(raw.get("confidence", 0))
    if confidence < min_confidence:
        return None
    necessity = float(raw.get("necessity", 0))
    resolved = store.try_add(t, v, necessity)
    if resolved is None:
        return None
    return t, resolved, confidence


def format_vocab_listing(
    vocab: dict[str, list[str]], max_types: int = 60, max_values_per_type: int = 12,
) -> str:
    """プロンプトに埋め込む用の、既知(attr_type, value)一覧のテキスト表現。
    値の多い型を優先して max_types 件まで表示し、各型は max_values_per_type
    件までを例として示す（全件ではない — 省略されている旨を明記する）。"""
    ordered = sorted(vocab.items(), key=lambda kv: len(kv[1]), reverse=True)[:max_types]
    lines = []
    for attr_type, values in ordered:
        if not values:
            lines.append(f"  {attr_type}: (no known values yet)")
            continue
        shown = values[:max_values_per_type]
        suffix = f", ... ({len(values)} known total)" if len(values) > max_values_per_type else ""
        lines.append(f"  {attr_type}: {', '.join(shown)}{suffix}")
    return "\n".join(lines)
