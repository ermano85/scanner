"""Layer 3: rules. Pure functions over the feature store.

All thresholds live in config/scan.yaml. There are no numeric literals in this package —
tests/test_no_literals.py walks the AST and fails the build if one appears.
"""
