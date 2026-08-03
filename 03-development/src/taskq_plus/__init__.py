"""taskq_plus — a small dependency-aware task queue CLI.

[FR-01] This package root only exposes the version marker; the layered
subpackages (`models` < `storage` < `service` < `observability` < `cli`)
carry the behaviour so the `.importlinter` layer contract stays enforceable.

Citations:
    SPEC.md §6 lines 330-360 — declared package tree.
    SPEC.md §6 line 375 — layer rule `cli > observability > service >
        storage > models`; `config` is an independence module.
"""
from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
