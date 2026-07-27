"""Spec §9: "If you type a number into a rule function, you have made a mistake."

This walks the AST of the rules and sizing packages and fails on any numeric constant
outside a small structural allowlist. It turns a code-review convention — the kind that
holds for three weeks — into something the build enforces.

The point is not pedantry. A threshold hardcoded in a rule function is invisible to
`config-check`, silently diverges from the documented value, and cannot be swept in a
future backtest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "qms"
GUARDED_PACKAGES = ("rules", "sizing")

# Structural constants only:
#   0, 1   indices, off-by-one arithmetic, empty checks
#   -1     reverse indexing
#   2      pairs
#   100    percent conversion, which is a unit not a threshold
ALLOWED = frozenset({0, 1, -1, 2, 100})


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for package in GUARDED_PACKAGES:
        files.extend(sorted((SRC / package).rglob("*.py")))
    return files


def test_the_guarded_packages_exist():
    """Otherwise this whole module passes by finding nothing to check."""
    files = _guarded_files()
    assert len(files) >= 5, f"expected the rules and sizing modules, found {files}"


@pytest.mark.parametrize("path", _guarded_files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_numeric_literals_in_rule_code(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value in ALLOWED:
            continue
        offenders.append(f"line {node.lineno}: {value!r}")

    assert not offenders, (
        f"{path.relative_to(SRC)} contains hardcoded number(s):\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery threshold belongs in config/scan.yaml (spec §9)."
    )


def test_allowlist_stays_small():
    """A growing allowlist is how this rule dies. Make expanding it deliberate."""
    assert len(ALLOWED) <= 6, "the allowlist is creeping — justify each addition"
