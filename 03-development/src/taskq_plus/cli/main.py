"""[FR-01/FR-02/FR-05] Top-level `python -m taskq_plus` dispatcher.

The dispatcher mirrors the same flag surfaces the subcommands expose
so `python -m taskq_plus run --all` reaches the `run` handler without
the `nargs=REMAINDER` tokenisation that hides `--all` from argparse.
The `submit` shim still uses `nargs=REMAINDER` because its argument
shape is intentionally free-form (the `submit` parser takes the
`command` itself as a positional with `nargs="?"`).

The contract `required=True` on the `command` choice means `handler`
is *guaranteed* set by the time `main()` is called — there is no
`None` branch to write.

The disk backend is used here (rather than the in-memory one) so the
subprocess test surface in `03-development/tests/test_fr01.py` reads
and writes the real `$TASKQ_HOME/tasks.json`. The in-process test
surface in the same file deliberately bypasses this module and goes
through `taskq_plus.cli.commands.submit` / `commands.run` directly so
it can exercise the validation paths under `pytest-cov`.

Citations:
    SPEC.md §6 line 337 — `taskq_plus.cli.main` location.
    SPEC.md §6 line 338 — `python -m taskq_plus` entry point.
    SPEC.md §8 lines 406-408 — acceptance commands.
    SPEC.md §3 FR-02 lines 105-118 — execution state machine.
    SPEC.md §3 FR-05 lines 126-137 — `click` 群組化子命令 entry point
        and the eight-command surface.
    SPEC.md §3 FR-05 line 139 — global `--json` single-line output.
    SPEC.md §3 FR-05 line 140 — canonical exit-code roster.
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

    run_p = sub.add_parser("run", help="Run a pending task by id or --all.")
    run_p.add_argument(
        "task_id", nargs="?",
        help="Task id to run.",
    )
    run_p.add_argument(
        "--all", action="store_true", dest="run_all",
        help="Run all pending tasks.",
    )
    run_p.add_argument(
        "--cached", action="store_true", dest="use_cache",
        help="Replay a recent completed result when available.",
    )

    # [FR-05] Query / inspection / maintenance subcommands. Each shim
    # forwards its own tokens verbatim to the matching handler in
    # `taskq_plus.cli.commands`, which owns the real flag surface.
    status_p = sub.add_parser("status", help="Print all fields of one task.")
    status_p.add_argument("task_id", help="Task id to inspect.")
    status_p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the task as a single-line JSON object.",
    )

    list_p = sub.add_parser("list", help="List stored tasks.")
    list_p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the task list as a single-line JSON array.",
    )

    sub.add_parser("clear", help="Remove every data file in $TASKQ_HOME.")

    sub.add_parser("graph", help="Print the task dependency graph.")

    plugins_p = sub.add_parser("plugins", help="List the plugin allowlist.")
    plugins_p.add_argument(
        "specs", nargs="*", default=[],
        help="Plugin module names; defaults to $TASKQ_PLUGINS.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """[FR-01/FR-02/FR-05] Dispatch one CLI invocation; return an exit code.

    Every documented exit code originates in the handler that owns the
    failing condition, not here: `0` success / `2` input validation /
    `3` breaker open / `4` task timeout / `5` dependency cycle or depth
    cap / `6` plugin load failure / `1` other internal error.

    Citations:
        SPEC.md §3 FR-05 lines 130-137 — the eight-command surface.
        SPEC.md §3 FR-05 line 140 — canonical exit-code roster.
    """
    # The subprocess test path expects each fresh `python -m taskq_plus`
    # call to see a clean in-process cache (none of the subprocess
    # test paths share an in-process store anyway, but resetting keeps
    # the boundary explicit).
    reset_store_cache()

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "submit":
        # The user-facing command is free-form; preserve all command tokens
        # as one validated command string before handing off to the handler.
        return commands.submit([" ".join(args.args)], use_disk=True)

    if args.command == "status":
        forwarded = [args.task_id]
        if args.as_json:
            forwarded.append("--json")
        return commands.status(forwarded, use_disk=True)

    if args.command == "list":
        return commands.list_tasks(
            ["--json"] if args.as_json else [], use_disk=True
        )

    if args.command == "clear":
        return commands.clear([])

    if args.command == "graph":
        return commands.graph([], use_disk=True)

    if args.command == "plugins":
        return commands.plugins(list(args.specs))

    # `run` — forward the parsed `task_id` and `--all` flag shape.
    if args.run_all:
        return commands.run(["--all"], use_disk=True)
    if args.task_id is not None:
        forwarded = [args.task_id]
        if args.use_cache:
            forwarded.append("--cached")
        return commands.run(forwarded, use_disk=True)
    return commands.run([], use_disk=True)
