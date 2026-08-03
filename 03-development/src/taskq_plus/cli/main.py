"""[FR-01] Top-level `python -m taskq_plus` dispatcher.

`nargs=REMAINDER` is the documented argparse passthrough: the
subcommand's own parser (e.g. `submit`'s) sees the exact tokens after
the subcommand name, so empty-string commands and `--flag=value`
shapes both reach validation untouched. The contract `required=True`
on the `command` choice means `handler` is *guaranteed* set by the
time `main()` is called — there is no `None` branch to write.

The disk backend is used here (rather than the in-memory one) so the
subprocess test surface in `03-development/tests/test_fr01.py` reads
and writes the real `$TASKQ_HOME/tasks.json`. The in-process test
surface in the same file deliberately bypasses this module and goes
through `taskq_plus.cli.commands.submit` directly so it can exercise
the validation paths under `pytest-cov`.

Citations:
    SPEC.md §6 line 337 — `taskq_plus.cli.main` location.
    SPEC.md §6 line 338 — `python -m taskq_plus` entry point.
    SPEC.md §8 lines 406-408 — acceptance commands.
"""
from __future__ import annotations

import argparse
from typing import List, Optional

from taskq_plus.cli import commands
from taskq_plus.storage.task_store import reset_store_cache


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taskq",
        description="A dependency-aware task queue.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    submit_p = sub.add_parser("submit", help="Submit a new task.")
    submit_p.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="Forwarded to `taskq submit`.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """[FR-01] Dispatch one CLI invocation; return an exit code."""
    # The subprocess test path expects each fresh `python -m taskq_plus`
    # call to see a clean in-process cache (none of the subprocess
    # test paths share an in-process store anyway, but resetting keeps
    # the boundary explicit).
    reset_store_cache()

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "submit":
        return commands.submit(args.args, use_disk=True)

    # `required=True` on the subparsers makes this branch unreachable.
    return 1
