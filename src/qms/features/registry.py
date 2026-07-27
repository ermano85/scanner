"""Feature registry.

Features register themselves here, which buys two things:

* **The causality suite enumerates the registry.** A new feature is causality-tested the
  moment it is written, with no test edit. A feature you forget to test is impossible.
* **`[EXT]` quarantine.** Every feature declares its provenance. `[EXT]` features live in
  a separate namespace, and `rules/gates.py` may only see `[DOC]` ones — so spec §3.7's
  "EXT may rank but may never filter" is a structural property rather than a comment
  someone eventually ignores.

Each feature is a `pl.Expr` over a frame sorted by `(symbol, date)`. The builder applies
`.over("symbol")` so a single expression computes the whole panel at once, and computes
in dependency order so a feature may read another feature's column.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import polars as pl

from qms.config import ScanConfig

Provenance = Literal["DOC", "EXT"]

ExprBuilder = Callable[[ScanConfig], pl.Expr]
WarmupFn = Callable[[ScanConfig], int]

# Columns that arrive from the ingest layer rather than being computed here.
BASE_COLUMNS = frozenset({"symbol", "date", "open", "high", "low", "close", "volume", "adjclose"})


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    builder: ExprBuilder
    provenance: Provenance
    warmup: WarmupFn
    deps: tuple[str, ...] = ()
    partition: bool = True
    description: str = ""


_REGISTRY: dict[str, FeatureSpec] = {}


def feature(
    name: str,
    *,
    provenance: Provenance,
    warmup: WarmupFn | int = 0,
    deps: tuple[str, ...] = (),
    partition: bool = True,
    description: str = "",
) -> Callable[[ExprBuilder], ExprBuilder]:
    """Register a feature. `warmup` may be a constant or a function of the config."""

    def decorate(builder: ExprBuilder) -> ExprBuilder:
        if name in _REGISTRY:
            raise ValueError(f"feature {name!r} is already registered")
        warmup_fn: WarmupFn = warmup if callable(warmup) else (lambda _cfg, n=warmup: n)
        _REGISTRY[name] = FeatureSpec(
            name=name,
            builder=builder,
            provenance=provenance,
            warmup=warmup_fn,
            deps=deps,
            partition=partition,
            description=description or (builder.__doc__ or "").strip().split("\n")[0],
        )
        return builder

    return decorate


def _load_feature_modules() -> None:
    """Import the modules that populate the registry. Idempotent."""
    from qms.features import consolidation, liquidity, momentum, trend, volatility  # noqa: F401


def all_features() -> dict[str, FeatureSpec]:
    _load_feature_modules()
    return dict(_REGISTRY)


def doc_features() -> dict[str, FeatureSpec]:
    """`[DOC]` features only — the ones a hard gate is allowed to read."""
    return {n: s for n, s in all_features().items() if s.provenance == "DOC"}


def ext_features() -> dict[str, FeatureSpec]:
    """`[EXT]` features — ranking only. Never a gate. Spec §3.7."""
    return {n: s for n, s in all_features().items() if s.provenance == "EXT"}


def resolve_order(specs: dict[str, FeatureSpec]) -> list[FeatureSpec]:
    """Topologically sort features so dependencies are computed first."""
    ordered: list[FeatureSpec] = []
    placed: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in placed or name in BASE_COLUMNS:
            return
        if name in visiting:
            raise ValueError(f"circular feature dependency involving {name!r}")
        spec = specs.get(name)
        if spec is None:
            raise ValueError(f"feature {name!r} depends on something unregistered")
        visiting.add(name)
        for dep in spec.deps:
            visit(dep)
        visiting.discard(name)
        placed.add(name)
        ordered.append(spec)

    for name in sorted(specs):
        visit(name)
    return ordered


def build_features(
    bars: pl.DataFrame,
    cfg: ScanConfig,
    specs: dict[str, FeatureSpec] | None = None,
) -> pl.DataFrame:
    """Compute every registered feature over a bar panel.

    `bars` must carry BASE_COLUMNS. The result is the input plus one column per feature.

    Causality note: every expression here is backward-looking, and `.over("symbol")` on a
    frame sorted by `(symbol, date)` preserves that. Nothing in this function may
    introduce a centred window, a full-history normalisation, or a back-fill — see
    tests/test_causality.py, which enumerates this registry and proves it.
    """
    specs = specs if specs is not None else all_features()
    missing = BASE_COLUMNS - set(bars.columns)
    if missing:
        raise ValueError(f"bar frame is missing base column(s): {sorted(missing)}")

    frame = bars.sort(["symbol", "date"])
    for spec in resolve_order(specs):
        expr = spec.builder(cfg)
        if spec.partition:
            expr = expr.over("symbol")
        frame = frame.with_columns(expr.alias(spec.name))
    return frame


def feature_warmup(cfg: ScanConfig, specs: dict[str, FeatureSpec] | None = None) -> dict[str, int]:
    """Bars of history each feature needs before it may emit a non-null value."""
    specs = specs if specs is not None else all_features()
    return {name: spec.warmup(cfg) for name, spec in specs.items()}
