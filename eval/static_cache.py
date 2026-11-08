"""Cached path scores of the static recommendation evaluation, collected once and ranked many times.

A record keeps what combine() reads from collect_path_scores(reader, player, {hidden}, scheme): the
score of every candidate on the peer, tag and studio paths, the players of every candidate (the
popular score is ln(1 + players)) and the weights of all visible seeds. The target "v0" keeps the top
50 of recommend_games_v0 instead.

Layout: <cache>/<stage>/manifest.json and <cache>/<stage>/<target>/part-<run>.jsonl.gz, one gzip member
per record. A record counts once its line is in the run's part-<run>.idx (key, byte offset, length),
which is written after the member is on disk. A killed run leaves at most an unlisted member and a cut-off
index line, which do not count; the next run skips every listed key and writes new parts, so nothing is
collected twice or kept twice.

Workers are spawned processes with one reader each (the neo4j driver is not fork-safe). Transient
neo4j errors are retried with backoff; a key that still fails is reported and left for the next run.
"""

import atexit
import fcntl
import gzip
import json
import logging
import math
import multiprocessing
import os
import sys
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import numpy as np
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from app.api import static_recommend
from app.api.static_recommend import PATHS, SCORE_DECIMALS, PathScores, SeedScheme, collect_path_scores, ordered_sum
from app.api.static_recommend_v0 import recommend_games_v0

V0 = "v0"
V0_TOP = 50
SCORE_PATHS = ("peer", "tag", "studio")
RETRYABLE = (TransientError, ServiceUnavailable, SessionExpired)
RETRIES = 3
BACKOFF_SECONDS = 2.0
PROGRESS_SECONDS = 30.0
# Candidates whose total is this close to the hidden game's are compared exactly as combine() does.
NEAR = 1e-9

Case = tuple[str, str, int, str]  # (player_id, stratum, rep, hidden appid), as eval.static_split.cases
Key = tuple[str, int]  # (player_id, rep)


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


# ---- records ---------------------------------------------------------------------------------------


def make_record(player_id: str, rep: int, hidden: str, target: str, path_scores: PathScores) -> dict[str, Any]:
    paths = path_scores["paths"]
    return {
        "player_id": player_id,
        "rep": rep,
        "hidden": hidden,
        "target": target,
        "seed_weights": [seed["weight"] for seed in path_scores["seeds"]],
        "scores": {path: {appid: row["score"] for appid, row in paths[path].items()} for path in SCORE_PATHS},
        "players": {appid: row["players"] for appid, row in paths["popular"].items()},
    }


def make_v0_record(player_id: str, rep: int, hidden: str, appids: list[str]) -> dict[str, Any]:
    return {"player_id": player_id, "rep": rep, "hidden": hidden, "target": V0, "top": appids}


def path_scores_from_record(record: Mapping[str, Any]) -> PathScores:
    """The PathScores combine() needs, without evidence."""
    paths = {
        path: {appid: {"score": score} for appid, score in rows.items()} for path, rows in record["scores"].items()
    }
    paths["popular"] = {
        appid: {"score": math.log1p(players), "players": players} for appid, players in record["players"].items()
    }
    return PathScores(
        player_id=record["player_id"], seeds=[{"weight": weight} for weight in record["seed_weights"]], paths=paths
    )


def encode(record: Mapping[str, Any]) -> bytes:
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
    return gzip.compress(line.encode("ascii"), compresslevel=6, mtime=0)


@dataclass(frozen=True)
class Entry:
    player_id: str
    rep: int
    hidden: str
    part: str
    offset: int
    length: int


def read_record(entry: Entry) -> dict[str, Any]:
    with open(entry.part, "rb") as handle:
        handle.seek(entry.offset)
        record = json.loads(gzip.decompress(handle.read(entry.length)))
    if (record["player_id"], record["rep"], record["hidden"]) != (entry.player_id, entry.rep, entry.hidden):
        raise ValueError(f"{entry.part} at {entry.offset} does not hold {entry.player_id} rep {entry.rep}")
    return record


# ---- the cache of one stage ------------------------------------------------------------------------


class StageCache:
    def __init__(self, root: Path, stage: str) -> None:
        self.directory = Path(root) / stage

    def check_manifest(self, manifest: Mapping[str, Any], write: bool) -> None:
        """Refuse a cache made from another split or graph, other targets of the same name or other scoring
        constants (every field but the targets must be equal); with write, add the new targets."""
        path = self.directory / "manifest.json"
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if stored is None and not write:
            raise SystemExit(f"no cache in {self.directory}; run collect first")
        merged = dict(manifest, targets=dict(stored["targets"] if stored else {}))
        if stored:
            for field in manifest:
                if field != "targets" and stored.get(field) != manifest[field]:
                    raise SystemExit(f"{path} has another {field}; use another --cache-dir")
        for name, definition in manifest["targets"].items():
            if merged["targets"].setdefault(name, definition) != definition:
                raise SystemExit(f"{path} defines target {name} differently; use another --cache-dir")
        if write and merged != stored:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, path)

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / ".lock", "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(f"another collect is writing {self.directory}") from None
            yield

    def entries(self, target: str) -> dict[Key, Entry]:
        """Every listed record of a target. A line cut off by a kill, or one past the end of its part,
        does not count."""
        entries: dict[Key, Entry] = {}
        for index in sorted((self.directory / target).glob("part-*.idx")):
            part = index.with_suffix(".jsonl.gz")
            size = part.stat().st_size if part.exists() else 0
            for line in index.read_text(encoding="ascii").splitlines(keepends=True):
                if not line.endswith("\n"):
                    continue
                player_id, rep, hidden, offset, length = line.rstrip("\n").split("\t")
                entry = Entry(player_id, int(rep), hidden, str(part), int(offset), int(length))
                if entry.offset + entry.length > size:
                    continue
                if (player_id, entry.rep) in entries:
                    raise ValueError(f"{target}: {player_id} rep {rep} is listed twice in {self.directory}")
                entries[(player_id, entry.rep)] = entry
        return entries

    def runs(self) -> list[dict[str, Any]]:
        path = self.directory / "runs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def add_run(self, summary: Mapping[str, Any]) -> None:
        with open(self.directory / "runs.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, sort_keys=True) + "\n")


class PartWriter:
    """Writes gzip members to a new part file of a run and lists each after it is written. A part is
    never reopened, so a line cut off by a kill stays the last line of its index."""

    def __init__(self, directory: Path, run: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.part = open(directory / f"part-{run}.jsonl.gz", "xb")
        self.index = open(directory / f"part-{run}.idx", "x", encoding="ascii")

    def write(self, player_id: str, rep: int, hidden: str, member: bytes) -> None:
        offset = self.part.tell()
        self.part.write(member)
        self.part.flush()
        self.index.write(f"{player_id}\t{rep}\t{hidden}\t{offset}\t{len(member)}\n")
        self.index.flush()

    def close(self) -> None:
        self.part.close()
        self.index.close()


def check_complete(cache: StageCache, target: str, cases: Sequence[Case]) -> dict[Key, Entry]:
    """The entries of every case, refusing a cache that misses one or hides another game."""
    entries = cache.entries(target)
    missing = [(player_id, rep) for player_id, _, rep, _ in cases if (player_id, rep) not in entries]
    if missing:
        raise SystemExit(f"{cache.directory}/{target}: {len(missing)} of {len(cases)} records are missing; run collect")
    for player_id, _, rep, hidden in cases:
        if entries[(player_id, rep)].hidden != hidden:
            raise SystemExit(f"{target}: {player_id} rep {rep} hides another game than the split")
    return {(player_id, rep): entries[(player_id, rep)] for player_id, _, rep, _ in cases}


# ---- workers ---------------------------------------------------------------------------------------

_reader: Any = None


def start_worker(reader_factory: Callable[[], Any] | None) -> None:
    """Initializer of a spawned worker: quiet neo4j logs and one reader for the process."""
    global _reader
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    if reader_factory is not None:
        _reader = reader_factory()
        atexit.register(_reader.close)


@contextmanager
def local_reader(reader_factory: Callable[[], Any] | None) -> Iterator[None]:
    """The worker state in this process, for one run without a pool."""
    global _reader
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    _reader = reader_factory() if reader_factory is not None else None
    try:
        yield
    finally:
        if _reader is not None:
            _reader.close()
        _reader = None


def with_retries(call: Callable[[], Any], what: str, sleep: Callable[[float], None] = time.sleep) -> Any:
    for attempt in range(RETRIES + 1):
        try:
            return call()
        except RETRYABLE as error:
            if attempt == RETRIES:
                raise
            delay = BACKOFF_SECONDS * 2**attempt
            log(f"retry {attempt + 1}/{RETRIES} of {what} in {delay:.0f} s: {type(error).__name__}")
            sleep(delay)


def effective_scheme(scheme: SeedScheme, visible: int) -> tuple[str, int | None]:
    """Schemes with the same value collect the same path scores."""
    limit = scheme.peer_limit
    return scheme.weighting, None if limit is None or limit >= visible else limit


Target = tuple[str, SeedScheme | None]  # (name, seed scheme), None for v0


def collect_task(
    task: tuple[str, int, str, tuple[Target, ...]],
) -> list[tuple[str, bytes | None, str | None, float | None]]:
    """(target, gzip member or None, error or None, seconds of the queries or None when an equivalent
    scheme's scores were reused) for every target of one player and rep."""
    player_id, rep, hidden, targets = task
    hidden_appids = frozenset({hidden})
    collected: dict[tuple[str, int | None], PathScores] = {}
    visible: int | None = None
    results = []
    for name, scheme in targets:
        started, queried = time.perf_counter(), True
        what = f"{player_id} rep {rep} {name}"
        try:
            if scheme is None:
                ranked = with_retries(
                    lambda: recommend_games_v0(_reader, player_id, V0_TOP, "en", hidden_appids), what
                )
                record = make_v0_record(player_id, rep, hidden, [item.appid for item in ranked])
            else:
                path_scores = collected.get(effective_scheme(scheme, visible)) if visible is not None else None
                queried = path_scores is None
                if queried:
                    path_scores = with_retries(
                        lambda: collect_path_scores(_reader, player_id, hidden_appids, scheme), what
                    )
                    visible = len(path_scores["seeds"])
                    collected[effective_scheme(scheme, visible)] = path_scores
                record = make_record(player_id, rep, hidden, name, path_scores)
            seconds = time.perf_counter() - started if queried else None
            results.append((name, encode(record), None, seconds))
        except Exception as error:  # reported by the caller, never dropped
            results.append((name, None, f"{type(error).__name__}: {error}", time.perf_counter() - started))
    return results


def executor(workers: int, initializer: Callable[..., None], initargs: tuple[Any, ...]) -> ProcessPoolExecutor:
    context = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(workers, mp_context=context, initializer=initializer, initargs=initargs)


def unordered_results(
    function: Callable[[Any], Any], tasks: Iterable[Any], workers: int, reader_factory: Callable[[], Any] | None
) -> Iterator[tuple[Any, Any, BaseException | None]]:
    """(task, result, error) as tasks finish; in this process when workers <= 1."""
    if workers <= 1:
        with local_reader(reader_factory):
            for task in tasks:
                try:
                    result = function(task)
                except Exception as error:
                    yield task, None, error
                else:
                    yield task, result, None
        return
    with executor(workers, start_worker, (reader_factory,)) as pool:
        queue = iter(tasks)
        running: dict[Future, Any] = {}
        while True:
            while len(running) < 2 * workers and (task := next(queue, None)) is not None:
                running[pool.submit(function, task)] = task
            if not running:
                return
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                task = running.pop(future)
                error = future.exception()
                yield task, None if error else future.result(), error


def ordered_results(function: Callable[[Any], Any], tasks: Iterable[Any], workers: int) -> Iterator[Any]:
    """function(task) for every task, in order, without the database."""
    if workers <= 1:
        with local_reader(None):
            yield from map(function, tasks)
        return
    with executor(workers, start_worker, (None,)) as pool:
        pending: deque[Future] = deque()
        for task in tasks:
            pending.append(pool.submit(function, task))
            if len(pending) >= 4 * workers:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()


class Progress:
    def __init__(self, label: str, total: int) -> None:
        self.label, self.total, self.done, self.failed = label, total, 0, 0
        self.started = self.reported = time.monotonic()

    def advance(self, done: int = 1, failed: int = 0) -> None:
        self.done += done
        self.failed += failed
        now = time.monotonic()
        if now - self.reported >= PROGRESS_SECONDS or self.done == self.total:
            self.reported = now
            rate = self.done / max(now - self.started, 1e-9)
            eta = (self.total - self.done) / rate if rate else 0.0
            log(
                f"{self.label}: {self.done:,}/{self.total:,} ({self.done / max(self.total, 1):.1%}), "
                f"{rate:.2f}/s, ETA {time.strftime('%H:%M:%S', time.gmtime(eta))}, failed {self.failed}"
            )


def collect(
    cache: StageCache,
    targets: Mapping[str, SeedScheme | None],
    cases_by_target: Mapping[str, Sequence[Case]],
    reader_factory: Callable[[], Any],
    workers: int,
) -> dict[str, Any]:
    """Collect every record of the targets that is not in the cache yet. Returns the run summary,
    whose failures list the keys that are still missing."""
    run = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with cache.lock():
        wanted: dict[tuple[str, int, str], list[Target]] = {}
        for name, scheme in targets.items():
            listed = cache.entries(name)
            for player_id, _, rep, hidden in cases_by_target[name]:
                if (player_id, rep) not in listed:
                    wanted.setdefault((player_id, rep, hidden), []).append((name, scheme))
        tasks = [(player_id, rep, hidden, tuple(chosen)) for (player_id, rep, hidden), chosen in wanted.items()]
        progress = Progress(f"collect {cache.directory.name}", sum(len(task[3]) for task in tasks))
        writers: dict[str, PartWriter] = {}
        written = {name: 0 for name in targets}
        queried: dict[str, list[float]] = {name: [] for name in targets}
        failures: list[dict[str, Any]] = []
        started = time.time()
        try:
            for task, results, error in unordered_results(collect_task, tasks, workers, reader_factory):
                player_id, rep, hidden, chosen = task
                if error is not None:
                    results = [(name, None, f"{type(error).__name__}: {error}", 0.0) for name, _ in chosen]
                for name, member, message, spent in results:
                    if member is None:
                        failures.append({"player_id": player_id, "rep": rep, "target": name, "error": message})
                        log(f"failed {player_id} rep {rep} {name}: {message}")
                        continue
                    if name not in writers:
                        writers[name] = PartWriter(cache.directory / name, run)
                    writers[name].write(player_id, rep, hidden, member)
                    written[name] += 1
                    if spent is not None:
                        queried[name].append(spent)
                progress.advance(len(results), sum(member is None for _, member, _, _ in results))
        except BrokenProcessPool as error:
            raise SystemExit(f"a worker process died ({error}); what was written is kept, run collect again") from error
        finally:
            for writer in writers.values():
                writer.close()
            summary = {
                "run": run,
                "workers": workers,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
                "wall_seconds": round(time.time() - started, 1),
                "planned": progress.total,
                "written": progress.done - progress.failed,
                "failures": failures,
                "records": written,
                "reused": {name: written[name] - len(queried[name]) for name in targets},
                "seconds_per_query": {
                    name: round(sum(values) / len(values), 4) for name, values in queried.items() if values
                },
            }
            cache.add_run(summary)
    return summary


# ---- ranking cached records ------------------------------------------------------------------------


class HiddenRank:
    """The hidden game's rank in combine()'s full ranking of one record, for many λ: totals in numpy,
    and every candidate within NEAR of the hidden game's total compared exactly as combine() does."""

    def __init__(self, record: Mapping[str, Any]) -> None:
        players = record["players"]
        self.appid_list = sorted(players)
        position = {appid: index for index, appid in enumerate(self.appid_list)}
        self.appids = np.array(self.appid_list)
        self.hidden_appid = record["hidden"]
        self.hidden = position[self.hidden_appid]
        self.players = np.array([players[appid] for appid in self.appid_list], dtype=np.int64)
        self.scores = {path: dict(record["scores"][path]) for path in SCORE_PATHS}
        self.scores["popular"] = {appid: math.log1p(count) for appid, count in players.items()}
        self.positive: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for path, rows in self.scores.items():
            chosen = [(position[appid], score) for appid, score in rows.items() if score > 0]
            self.positive[path] = (
                np.array([index for index, _ in chosen], dtype=np.int64),
                np.array([score for _, score in chosen], dtype=np.float64),
            )
        self.highest = {path: max(rows.values(), default=0.0) for path, rows in self.scores.items()}
        self.seed_weight = ordered_sum(record["seed_weights"])

    def divisor(self, path: str, studio_normalization: str) -> float:
        if path == "studio" and studio_normalization == "seed_weight":
            return self.seed_weight
        return self.highest[path]

    def rank(self, weights: Mapping[str, float], studio_normalization: str) -> int:
        active = [
            (path, weights.get(path, 0.0), self.divisor(path, studio_normalization))
            for path in PATHS
            if weights.get(path, 0.0) != 0 and self.divisor(path, studio_normalization) > 0
        ]
        total = np.zeros(len(self.appid_list))
        scored = np.zeros(len(self.appid_list), dtype=bool)
        for path, weight, divisor in active:
            positions, scores = self.positive[path]
            total[positions] += weight * scores / divisor
            scored[positions] = True
        hidden = self.hidden
        if not scored[hidden]:
            count = self.players[hidden]
            ahead = ~scored & (
                (self.players > count) | ((self.players == count) & (self.appids < self.hidden_appid))
            )
            return int(scored.sum()) + int(ahead.sum()) + 1
        target = total[hidden]
        ahead = int(np.count_nonzero(scored & (total > target + NEAR)))
        own_key = self.key(hidden, active)
        for index in np.flatnonzero(scored & (np.abs(total - target) <= NEAR)):
            if index != hidden and self.key(int(index), active) < own_key:
                ahead += 1
        return ahead + 1

    def key(self, index: int, active: list[tuple[str, float, float]]) -> tuple[float, str]:
        """combine()'s sort key of one candidate, from the same float operations."""
        appid = self.appid_list[index]
        parts = {}
        for path, weight, divisor in active:
            score = self.scores[path].get(appid, 0.0)
            if score > 0:
                parts[path] = weight * score / divisor
        return -round(ordered_sum(parts.values()), SCORE_DECIMALS), appid


def full_ranking(path_scores: PathScores, weights: Mapping[str, float], studio_normalization: str) -> list[str]:
    """combine()'s ranking of every candidate."""
    everything = len(path_scores["paths"]["popular"])
    return [item["appid"] for item in static_recommend.combine(path_scores, everything, weights, studio_normalization)]


def worker_reader() -> Any:
    return _reader


Spec = tuple[str, Mapping[str, float], str]  # (name, λ per path, studio normalization)


def hidden_rank_task(task: tuple[Entry, tuple[Spec, ...]]) -> list[int]:
    entry, specs = task
    table = HiddenRank(read_record(entry))
    return [table.rank(weights, normalization) for _, weights, normalization in specs]


def rank_hidden(
    cache: StageCache, target: str, cases: Sequence[Case], specs: Sequence[Spec], workers: int
) -> dict[str, dict[str, list[int]]]:
    """The hidden game's rank of every case under every spec: spec -> player -> rank of each rep."""
    entries = check_complete(cache, target, cases)
    ranks: dict[str, dict[str, list[int]]] = {name: {} for name, _, _ in specs}
    progress = Progress(f"rank {cache.directory.name} {target}", len(cases))
    tasks = ((entries[(player_id, rep)], tuple(specs)) for player_id, _, rep, _ in cases)
    for (player_id, _, _, _), values in zip(cases, ordered_results(hidden_rank_task, tasks, workers)):
        for (name, _, _), rank in zip(specs, values):
            ranks[name].setdefault(player_id, []).append(rank)
        progress.advance()
    return ranks


def ranking_task(task: tuple[Entry, tuple[Spec, ...], int]) -> dict[str, tuple[int, list[str] | None]]:
    entry, specs, top = task
    path_scores = path_scores_from_record(read_record(entry))
    results = {}
    for name, weights, normalization in specs:
        ranking = full_ranking(path_scores, weights, normalization)
        results[name] = (ranking.index(entry.hidden) + 1, ranking[:top] if entry.rep == 0 else None)
    return results


def rank_with_combine(
    cache: StageCache, target: str, cases: Sequence[Case], specs: Sequence[Spec], workers: int, top: int
) -> dict[str, dict[Key, tuple[int, list[str] | None]]]:
    """The hidden game's rank and the rep-0 top list of every case under every spec, from combine()."""
    entries = check_complete(cache, target, cases)
    results: dict[str, dict[Key, tuple[int, list[str] | None]]] = {name: {} for name, _, _ in specs}
    progress = Progress(f"rank {cache.directory.name} {target}", len(cases))
    tasks = ((entries[(player_id, rep)], tuple(specs), top) for player_id, _, rep, _ in cases)
    for (player_id, _, rep, _), ranked in zip(cases, ordered_results(ranking_task, tasks, workers)):
        for name, value in ranked.items():
            results[name][(player_id, rep)] = value
        progress.advance()
    return results
