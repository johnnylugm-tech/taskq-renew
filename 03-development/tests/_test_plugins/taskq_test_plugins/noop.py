"""Taskq plugin fixture — no-op hooks.

Exposes both `pre_run` and `post_run` with no side effects, so the
FR-07 `plugins list` acceptance test can assert that the loader
recognises the module name and reports both hooks as registered.

Citations:
    SPEC.md §3 FR-07 — "A plugin is a Python module exposing
        `pre_run(task) -> None` and/or `post_run(task, result) -> None`."
"""


def pre_run(task) -> None:
    """No-op pre-execution hook. Returns None."""


def post_run(task, result) -> None:
    """No-op post-execution hook. Returns None."""
