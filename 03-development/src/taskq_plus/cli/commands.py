"""[FR-01] CLI command handlers.

The `submit` function is the canonical entry point that the test suite
calls in-process (`taskq_plus.cli.commands.submit(argv)`); the
`python -m taskq_plus` entry point dispatches the same function via
`taskq_plus.cli.main`.

In-process callers (the test suite) see an isolated in-memory store so
per-test isolation is preserved without a global reset; subprocess
callers go through `main` which uses the disk-backed store so the
real `tasks.json` round-trip is exercised.

Citations:
    SPEC.md §3 FR-01 lines 72-94 — submission rules + accepted shape.
    SPEC.md §7 line 383 — 空/非法命令 → exit 2, stderr 說明.
    SPEC.md §7 line 385 — `--after` 指向不存在的 id → exit 2, stderr
        `unknown dependency: <id>`.
    SPEC.md §6 line 337 — `cli/commands` module location.
    SPEC.md §8 lines 406-408 — canonical acceptance commands.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from pydantic import ValidationError

from taskq_plus.models.task import Task, TaskSubmission
from taskq_plus.storage.task_store import get_store


def _build_submit_parser() -> argparse.ArgumentParser:
    """[FR-01] Build the `submit` argument parser.

    `command` is `nargs="?"` so an empty string reaches pydantic
    validation as the empty-command reject case (SPEC §3 line 80)
    instead of being swallowed by argparse's flag-detection.
    """
    parser = argparse.ArgumentParser(
        prog="taskq submit",
        description="Submit a new task to the queue.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="",
        help="Shell command to run (validated).",
    )
    parser.add_argument("--name", default=None, help="Optional friendly name.")
    parser.add_argument(
        "--after",
        action="append",
        default=[],
        dest="after",
        help="Task id this submission depends on (repeatable).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the new task as JSON instead of just the id.",
    )
    return parser


def _format_validation_error(err: ValidationError) -> str:
    """[FR-01] Reduce a pydantic ValidationError to a single-line message.

    Pydantic v2 returns a list of error dicts; we surface the first
    message verbatim so the user sees the same wording pydantic used
    to decide the rule was violated.
    """
    errors = err.errors()
    if not errors:
        return "validation failed"
    first = errors[0]
    msg = first.get("msg", "validation failed")
    # Pydantic prefixes ValueError messages with "Value error, ".
    prefix = "Value error, "
    if msg.startswith(prefix):
        msg = msg[len(prefix):]
    return msg


def _emit_stderr_error(message: str) -> None:
    """[FR-01] Print `submit: <message>` to stderr in one place.

    Centralising the `submit:` prefix keeps the user-facing wording
    consistent across the validation, name-uniqueness, and
    dependency-existence rejection paths.
    """
    print(f"submit: {message}", file=sys.stderr)


def submit(argv: Optional[List[str]] = None, *, use_disk: bool = False) -> int:
    """[FR-01] Run a `submit` invocation.

    `argv` is the token list *after* the leading `submit` keyword (the
    in-process helper in `tests/test_fr01.py` passes the user tokens
    directly; the `python -m taskq_plus` dispatcher strips the
    `submit` keyword before calling this function).

    `use_disk=True` selects the on-disk backend (used by the
    `python -m taskq_plus` entry point so the subprocess tests
    exercise the real `$TASKQ_HOME/tasks.json` round-trip); the
    default in-process backend is `InMemoryBackend` so the in-process
    test surface stays isolated from any on-disk state (the test
    fixture gives each test a unique `TASKQ_HOME`, so the per-test
    in-memory cache also gets a unique key).

    Returns:
        0 — submission persisted.
        2 — validation / uniqueness / dependency rule failed.

    Citations:
        SPEC.md §7 line 383 — exit 2 on 空/非法命令.
        SPEC.md §7 line 385 — exit 2 on `--after` 不存在.
    """
    parser = _build_submit_parser()
    args = parser.parse_args(list(argv) if argv is not None else [])

    try:
        submission = TaskSubmission(
            command=args.command,
            name=args.name,
            depends_on=list(args.after),
        )
    except ValidationError as exc:
        _emit_stderr_error(_format_validation_error(exc))
        return 2

    store = get_store(use_disk=use_disk)

    # Name uniqueness: pending/running only (SPEC §3 line 83).
    if submission.name is not None and store.contains_name(submission.name):
        _emit_stderr_error(f"duplicate name: {submission.name}")
        return 2

    # Dependency existence: every --after id must already exist
    # (SPEC §3 line 84).
    for dep in submission.depends_on:
        if not store.has_id(dep):
            _emit_stderr_error(f"unknown dependency: {dep}")
            return 2

    task = Task(
        command=submission.command,
        name=submission.name,
        depends_on=submission.depends_on,
    )
    stored = store.add(task)

    if args.as_json:
        print(json.dumps({"id": stored.id, "status": stored.status}))
    else:
        print(stored.id)
    return 0
