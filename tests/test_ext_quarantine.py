"""[EXT] features may contribute to ranking. They may never act as a filter. Spec §3.7.

The spec is emphatic that unvalidated extrapolations must not hard-filter anything in v1.
A comment saying so decays; this does not. Two layers of enforcement:

1. The registry splits `doc_features()` from `ext_features()`, and the gate layer is
   constructed from the former.
2. The AST scan below fails if any `[EXT]` feature name appears anywhere in the gate
   module — including in a string, so building a column name dynamically does not sneak
   past it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from qms.features.registry import all_features, doc_features, ext_features

GATES_MODULE = Path(__file__).resolve().parents[1] / "src" / "qms" / "rules" / "gates.py"


def test_both_namespaces_are_populated():
    """Guards against the quarantine passing because one side is empty."""
    assert doc_features(), "no [DOC] features registered"
    assert ext_features(), "no [EXT] features registered"


def test_namespaces_partition_the_registry():
    doc, ext, every = set(doc_features()), set(ext_features()), set(all_features())
    assert doc | ext == every
    assert not (doc & ext), f"features claiming both provenances: {doc & ext}"


def test_consolidation_module_is_entirely_ext():
    """Spec §3.7: 'ALL [EXT]'. Nothing [DOC] may drift into that module."""
    from qms.features import consolidation  # noqa: F401

    offenders = [
        name
        for name, spec in all_features().items()
        if spec.provenance == "DOC"
        and (spec.builder.__module__ or "").endswith("features.consolidation")
    ]
    assert not offenders, f"[DOC] features declared in the [EXT]-only module: {offenders}"


def test_ext_features_are_absent_from_the_gate_module():
    """The structural half of the quarantine."""
    if not GATES_MODULE.exists():
        pytest.skip("rules/gates.py not written yet")

    source = GATES_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)

    leaked = sorted(referenced & set(ext_features()))
    assert not leaked, (
        f"[EXT] feature(s) {leaked} are referenced in rules/gates.py. EXT metrics may "
        "rank but may never filter (spec §3.7) — move this to rules/rank.py."
    )


def test_gate_module_does_not_import_the_ext_namespace():
    if not GATES_MODULE.exists():
        pytest.skip("rules/gates.py not written yet")

    tree = ast.parse(GATES_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith("features.consolidation"):
                pytest.fail("rules/gates.py imports the [EXT] consolidation module")
            for alias in node.names:
                assert alias.name != "ext_features", (
                    "rules/gates.py imports ext_features(); gates are built from "
                    "doc_features() only"
                )
