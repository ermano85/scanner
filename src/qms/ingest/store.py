"""Parquet persistence, run manifests, and compaction.

The spec requires ingest to be idempotent and resumable. That is implemented here as a
three-step cycle:

1. Fetched rows land in `data/raw/<kind>/date=<run>/batch_NNNN.parquet` in batches.
2. A `_manifest.json` in the same directory records which symbols succeeded, which failed
   transiently, and which failed permanently (404 — delisted or bogus).
3. `compact()` folds the batches into the canonical store, keyed so re-running is a no-op.

Killing the job mid-run therefore loses at most one batch, and a re-run fetches only the
gap. Permanent failures are remembered so a dead ticker is not retried every night.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

MANIFEST_NAME = "_manifest.json"
BATCH_GLOB = "batch_*.parquet"


@dataclass
class Manifest:
    """Resume state for one ingest run."""

    kind: str
    run_date: dt.date
    directory: Path
    completed: set[str] = field(default_factory=set)
    permanent_failures: dict[str, str] = field(default_factory=dict)
    transient_failures: dict[str, str] = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    batch_index: int = 0

    @property
    def path(self) -> Path:
        return self.directory / MANIFEST_NAME

    @classmethod
    def load_or_create(
        cls, kind: str, run_date: dt.date, directory: Path, params: dict | None = None
    ) -> Manifest:
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / MANIFEST_NAME
        if manifest_path.exists():
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing = cls(
                kind=raw["kind"],
                run_date=dt.date.fromisoformat(raw["run_date"]),
                directory=directory,
                completed=set(raw.get("completed", [])),
                permanent_failures=dict(raw.get("permanent_failures", {})),
                transient_failures=dict(raw.get("transient_failures", {})),
                params=raw.get("params", {}),
                batch_index=int(raw.get("batch_index", 0)),
            )
            # Params differing means this is a different job wearing the same date — the
            # resume state does not apply and reusing it would silently skip symbols.
            if params is not None and existing.params != params:
                return cls(kind=kind, run_date=run_date, directory=directory, params=params)
            return existing
        return cls(kind=kind, run_date=run_date, directory=directory, params=params or {})

    def save(self) -> None:
        payload = {
            "kind": self.kind,
            "run_date": self.run_date.isoformat(),
            "params": self.params,
            "batch_index": self.batch_index,
            "completed": sorted(self.completed),
            "permanent_failures": self.permanent_failures,
            "transient_failures": self.transient_failures,
        }
        _atomic_write_text(self.path, json.dumps(payload, indent=2))

    def pending(self, symbols: list[str], retry_permanent: bool = False) -> list[str]:
        """Symbols still to fetch: not completed, and not known-dead."""
        skip = set(self.completed)
        if not retry_permanent:
            skip |= set(self.permanent_failures)
        return [s for s in symbols if s not in skip]

    def record_success(self, symbol: str) -> None:
        self.completed.add(symbol)
        self.transient_failures.pop(symbol, None)
        self.permanent_failures.pop(symbol, None)

    def record_failure(self, symbol: str, reason: str, permanent: bool) -> None:
        if permanent:
            self.permanent_failures[symbol] = reason
            self.transient_failures.pop(symbol, None)
        else:
            self.transient_failures[symbol] = reason

    def next_batch_path(self) -> Path:
        path = self.directory / f"batch_{self.batch_index:04d}.parquet"
        self.batch_index += 1
        return path


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + replace, so an interrupt cannot leave a torn manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def write_parquet_atomic(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    frame.write_parquet(temp_path)
    temp_path.replace(path)


def read_parquet_or_empty(path: Path, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame(schema=schema)
    return pl.read_parquet(path)


def read_batches(directory: Path, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    batches = sorted(directory.glob(BATCH_GLOB))
    if not batches:
        return pl.DataFrame(schema=schema)
    return pl.concat([pl.read_parquet(p) for p in batches], how="vertical_relaxed")


def compact(
    batch_dir: Path,
    target: Path,
    schema: dict[str, pl.DataType],
    keys: list[str],
) -> tuple[int, int]:
    """Fold raw batches into the canonical store.

    Newly fetched rows win over stored rows for the same key, which is what makes a
    re-run a no-op and lets a vendor restatement correct itself. Returns
    (rows_before, rows_after).
    """
    incoming = read_batches(batch_dir, schema)
    existing = read_parquet_or_empty(target, schema)
    rows_before = existing.height

    if incoming.is_empty():
        return rows_before, rows_before

    combined = pl.concat([existing, incoming], how="vertical_relaxed")
    # keep="last" => incoming supersedes existing, since it is concatenated second.
    combined = combined.unique(subset=keys, keep="last").sort(keys)
    write_parquet_atomic(combined, target)
    return rows_before, combined.height
