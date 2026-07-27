"""Layer 2: features. Vectorized indicators computed once and written back to disk.

Every feature may read only bars at index <= i. Enforced by tests/test_causality.py,
which enumerates the registry rather than a hand-maintained list.
"""
