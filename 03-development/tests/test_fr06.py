"""FR-06: 任務相依 DAG — Task dependency DAG with Kahn topological sort.

Test cases correspond 1:1 to TEST_SPEC.md §FR-06 (rows 1–4). The function
names below are the canonical names `spec-coverage-check` looks up — do
NOT rename.

Per SPEC.md §3 FR-06 the DAG contract is:

    --after      each occurrence adds one `depends_on` edge.
    run --all    uses Kahn topological sort; only in-degree-0 tasks are
                 eligible for concurrent dispatch within a layer.
    blocked      a downstream task whose prerequisite is not `done` is
                 marked `blocked`, NOT executed, and does NOT count
                 toward the breaker failure counter.
    cycle        a submit that would close a cycle is rejected with
                 exit 5 + stderr listing the cycle path (`A → B → A`).
    depth cap    chain depth > TASKQ_MAX_DAG_DEPTH → reject, exit 5 +
                 stderr `dependency chain too deep: <n> > <max>`.

Subprocess tests exercise the real `python -m taskq_plus` entry point so
the user-facing surface is verified. In-process tests import the declared
SAB modules (`taskq_plus.service.dag` and `taskq_plus.models.task`)
directly so pytest-cov can measure the FR-06 handlers (the subprocess
acceptance path cannot raise coverage on these — see GATE1 SUBPROCESS
COVERAGE CEILING in the integration guidelines). Both modules are
declared in `SAB.json` §fr_module_traceability for FR-06; their on-disk
presence is enforced by the Architecture Amendment Protocol.
"""
from __future__ import annotations

import contextlib
import io
import json as _json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """Invoke `python -m taskq_plus <args>` in a child process.

    Out-of-process decision: the canonical SPEC.md §8 rows spell out
    `python -m taskq_plus` literally, so the test must reproduce the
    user-facing entry point. The in-process tests below exercise the
    same paths through the declared SAB modules so pytest-cov can
    measure them.
    """
    return subprocess.run(
        [sys.executable, "-m", "taskq_plus", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _submit_and_get_id(args: list, env: dict) -> str:
    """Submit a task via the CLI and return its 8-hex id.

    Asserts the submit succeeded and the stdout matches the id regex
    so the caller can immediately use the id without re-asserting the
    same invariants.
    """
    proc = _run_subprocess(["submit", *args], env)
    assert proc.returncode == 0, (
        f"submit must exit 0; got {proc.returncode}; stderr={proc.stderr!r}"
    )
    task_id = proc.stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit stdout {task_id!r} is not an 8-hex id"
    )
    return task_id


def _read_tasks_json(taskq_home: Path) -> list:
    """Read and parse `$TASKQ_HOME/tasks.json` (empty list if missing)."""
    tasks_file = taskq_home / "tasks.json"
    if not tasks_file.exists():
        return []
    return _json.loads(tasks_file.read_text(encoding="utf-8"))


def _read_breaker_json(taskq_home: Path) -> dict:
    """Read and parse `$TASKQ_HOME/breaker.json` (empty dict if missing)."""
    breaker_file = taskq_home / "breaker.json"
    if not breaker_file.exists():
        return {}
    return _json.loads(breaker_file.read_text(encoding="utf-8"))


def _record_by_id(records: list) -> dict:
    """Map a list of task records by their `id` field."""
    return {r["id"]: r for r in records}


def _parse_finished_at(value) -> float:
    """Parse a `finished_at` ISO-8601 string into a Unix timestamp.

    Returns 0.0 if the value is missing or unparseable so callers can
    use the result in comparison without TypeError.
    """
    if not value:
        return 0.0
    try:
        # Tolerate trailing "Z" by normalising to +00:00.
        normalised = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalised).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _datetime_to_ts(value) -> float:
    """Convert a `datetime` to a Unix timestamp; 0.0 on None."""
    if value is None:
        return 0.0
    return value.timestamp()


# ---------------------------------------------------------------------------
# Cases 1 + 2 — AC-06-1: `run --all` respects dependency order / blocks
# ---------------------------------------------------------------------------
#
# The TEST_SPEC enumerates two scenarios under one function name —
# `test_fr06_run_all_respects_dependency_order_and_blocks` — and pins
# them via the parametrize ids `[ordered]` and `[blocked]`. The body
# dispatches on the id so both scenarios share the canonical function
# name `spec-coverage-check` looks up.


# NFR-09 / NP-04
def test_fr06_run_all_respects_dependency_order_and_blocks(
    taskq_home, child_env, monkeypatch, scenario: str
):
    """AC-06-1: `submit "echo b" --after <a-id>` followed by `run --all`
    runs `b` only after `a` is `done`; if `a` ends in any non-`done`
    state, `b` is marked `blocked`, not executed, and does NOT count
    toward the breaker failure counter. *(SPEC §3 FR-06 + §8 #10)*

    The TEST_SPEC expands this single inventory row into two parametrized
    sub-cases — `[ordered]` (both done, a finishes before b) and
    `[blocked]` (a fails, b is blocked, breaker untouched). The
    function name is preserved; the parametrize id `[ordered]` /
    `[blocked]` is the pytest-side label.
    """
    if scenario == "ordered":
        # 1. Submit `a` (the prerequisite). `sleep 0.5` is a clean
        #    command that avoids the injection blacklist and gives
        #    `a` a measurable finished_at that is reliably later
        #    than `b`'s start.
        a_id = _submit_and_get_id(["sleep", "0.5"], child_env)

        # 2. Submit `b` that depends on `a`. `b` is instant.
        b_id = _submit_and_get_id(
            ["echo", "b-done", "--after", a_id], child_env
        )

        # 3. Run all pending tasks through the documented CLI entry point.
        run_proc = _run_subprocess(["run", "--all"], child_env)
        assert run_proc.returncode == 0, (
            f"run --all must exit 0; got {run_proc.returncode}; "
            f"stderr={run_proc.stderr!r}"
        )

        # 4. Read the persisted tasks and assert the topological surface.
        tasks = _record_by_id(_read_tasks_json(taskq_home))
        assert set(tasks) == {a_id, b_id}, (
            f"tasks.json must hold exactly {{a_id, b_id}}; "
            f"got {sorted(tasks)!r}"
        )

        a_finished = _parse_finished_at(tasks[a_id].get("finished_at"))
        b_finished = _parse_finished_at(tasks[b_id].get("finished_at"))

        assert tasks[a_id]["status"] == "done", (
            f"a must be done after run --all; got "
            f"{tasks[a_id].get('status')!r}"
        )
        assert tasks[b_id]["status"] == "done", (
            f"b must be done after run --all (its prereq a is done); "
            f"got {tasks[b_id].get('status')!r}"
        )
        assert a_finished > 0.0 and b_finished > 0.0, (
            f"both tasks must have a finished_at timestamp; "
            f"a={tasks[a_id].get('finished_at')!r}, "
            f"b={tasks[b_id].get('finished_at')!r}"
        )
        # Kahn's invariant: every prerequisite finishes before its
        # dependent. The 0.5 s sleep on `a` makes this a robust order
        # check: b cannot have finished before a.
        assert a_finished < b_finished, (
            f"b must finish AFTER a (topological order); got "
            f"a.finished_at={a_finished}, b.finished_at={b_finished}"
        )
        return

    if scenario == "blocked":
        # 1. Submit `a` as `false` (a guaranteed non-zero exit).
        a_id = _submit_and_get_id(["false"], child_env)

        # 2. Submit `b` that depends on `a`. The blocked path means
        #    `b` must NEVER execute, so its command body must not
        #    matter — use a sentinel that would crash if it were
        #    ever run.
        b_id = _submit_and_get_id(
            ["echo", "b-should-not-run", "--after", a_id], child_env
        )

        # 3. Run all pending tasks. The CLI exits 0 even when
        #    individual tasks fail (per-task outcome is in the task
        #    record).
        run_proc = _run_subprocess(["run", "--all"], child_env)
        assert run_proc.returncode == 0, (
            f"run --all must exit 0 (per-task failures surface on the "
            f"task record); got {run_proc.returncode}; "
            f"stderr={run_proc.stderr!r}"
        )

        # 4. Read the persisted tasks and assert the blocked surface.
        tasks = _record_by_id(_read_tasks_json(taskq_home))
        assert set(tasks) == {a_id, b_id}, (
            f"tasks.json must hold exactly {{a_id, b_id}}; "
            f"got {sorted(tasks)!r}"
        )

        assert tasks[a_id]["status"] == "failed", (
            f"a must be failed (false exits non-zero); got "
            f"{tasks[a_id].get('status')!r}"
        )
        assert tasks[b_id]["status"] == "blocked", (
            f"b must be blocked (its prereq a failed); got "
            f"{tasks[b_id].get('status')!r}"
        )
        # The blocked task MUST NOT have been executed — its command
        # body must not have produced a stdout_tail or exit_code.
        assert tasks[b_id].get("stdout_tail") in (None, ""), (
            f"a blocked task must not have executed; got "
            f"stdout_tail={tasks[b_id].get('stdout_tail')!r}"
        )

        # 5. Breaker count check: the blocked task must NOT count
        #    toward the breaker failure counter. The single non-zero
        #    exit from `a` accounts for exactly one failure; `b` was
        #    never executed.
        breaker = _read_breaker_json(taskq_home)
        failure_count = int(breaker.get("failure_count", 0))
        assert failure_count == 1, (
            f"only the executed task `a` must count toward the "
            f"breaker; got failure_count={failure_count} (the "
            f"blocked task `b` was incorrectly counted)"
        )
        return

    raise AssertionError(
        f"unhandled scenario {scenario!r} for "
        f"test_fr06_run_all_respects_dependency_order_and_blocks"
    )


# Parametrize the two scenarios the TEST_SPEC enumerates. Each row
# carries its own `scenario` label matching the TEST_SPEC id.
_FR06_ORDER_BLOCK_PARAMS = [
    pytest.param("ordered", id="ordered"),
    pytest.param("blocked", id="blocked"),
]


pytest.mark.parametrize(
    "scenario",
    [p.values[0] for p in _FR06_ORDER_BLOCK_PARAMS],
    ids=[p.id for p in _FR06_ORDER_BLOCK_PARAMS],
)(test_fr06_run_all_respects_dependency_order_and_blocks)


# ---------------------------------------------------------------------------
# Case 3 — AC-06-2: cycle submission returns exit 5 with cycle path on stderr
# ---------------------------------------------------------------------------


# NFR-09 / NP-04
def test_fr06_cycle_submission_returns_cycle_path(taskq_home, child_env):
    """AC-06-2: constructing A → B → A returns exit 5 with stderr
    containing the cycle path. *(SPEC §3 FR-06 line 147 + §7 line 388)*

    NFR-09 (test_assertion_quality): the test constructs the cycle by
    submitting `a`, submitting `b --after a`, then closing the cycle
    via a direct edit to `tasks.json` (the canonical `submit` API can
    only add new tasks, never edit existing edges, so the closing
    edge must come from the persisted store). A subsequent
    submit --after is rejected with exit 5; stderr names the cycle
    path. The path is asserted via both real task ids AND the `→`
    separator so the format is pinned.
    """
    # 1. Submit `a` (the cycle's first node).
    a_id = _submit_and_get_id(["echo", "a"], child_env)

    # 2. Submit `b --after a` (one edge of the cycle).
    b_id = _submit_and_get_id(["echo", "b", "--after", a_id], child_env)

    # 3. Close the cycle A -> B -> A by editing tasks.json: `a`
    #    now depends on `b`. The submit API cannot add this edge
    #    because `a` already exists — direct file modification is
    #    the only surface through which a cycle can enter the
    #    persisted graph.
    records = _read_tasks_json(taskq_home)
    assert {r["id"] for r in records} == {a_id, b_id}, (
        f"precondition: tasks.json must hold exactly {{a_id, b_id}}; "
        f"got {sorted(r['id'] for r in records)!r}"
    )
    for record in records:
        if record["id"] == a_id:
            record["depends_on"] = [b_id]
    (taskq_home / "tasks.json").write_text(
        _json.dumps(records), encoding="utf-8"
    )

    # 4. Now submit a new task with --after. The cycle detector must
    #    reject this submission with exit 5 and stderr listing the
    #    cycle path. The path must use the `→` separator and include
    #    both real task ids.
    proc = _run_subprocess(
        ["submit", "echo", "c", "--after", b_id], child_env
    )
    assert proc.returncode == 5, (
        f"a submit on a cyclic graph must exit 5; got "
        f"{proc.returncode}; stderr={proc.stderr!r}"
    )
    # The stderr must contain the canonical separator and name every
    # member of the real cycle A -> B -> A.
    assert "→" in proc.stderr, (
        f"stderr must use the `→` cycle separator; got {proc.stderr!r}"
    )
    for member in (a_id, b_id):
        assert member in proc.stderr, (
            f"stderr must name the cycle member {member!r}; got "
            f"{proc.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Case 4 — AC-06-3: dependency chain depth > TASKQ_MAX_DAG_DEPTH → exit 5
# ---------------------------------------------------------------------------


# NFR-09 / NP-04
def test_fr06_dag_depth_cap_rejects_deep_chain(
    taskq_home, child_env, monkeypatch
):
    """AC-06-3: a chain whose depth exceeds `TASKQ_MAX_DAG_DEPTH`
    returns exit 5 with stderr `dependency chain too deep: <n> > <max>`.
    *(SPEC §3 FR-06 line 148 + §7 line 389)*

    NFR-09 (test_assertion_quality): the test sets the cap to 5 and
    builds a chain of exactly 5 tasks (a → b → c → d → e, depth 5).
    Submitting a 6th task that depends on `e` must be rejected with
    exit 5 and the canonical stderr template
    `dependency chain too deep: 6 > 5`.
    """
    # 1. Set TASKQ_MAX_DAG_DEPTH to 5 per the TEST_SPEC Inputs.
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "5")

    # 2. Build a chain of exactly 5 tasks (depth 5, within the cap).
    #    Each task depends on the previous so depth == chain length.
    chain_ids = []
    chain_ids.append(_submit_and_get_id(["echo", "a"], child_env))
    for letter, prev in zip("bcde", chain_ids):
        chain_ids.append(
            _submit_and_get_id(
                ["echo", letter, "--after", prev], child_env
            )
        )
    assert len(chain_ids) == 5, (
        f"precondition: chain must hold exactly 5 ids; got {len(chain_ids)}"
    )

    # 3. Attempt to submit a 6th task that depends on `e`. The chain
    #    depth would become 6, which exceeds the cap of 5. The submit
    #    must be rejected with exit 5 + the canonical stderr template.
    e_id = chain_ids[-1]
    proc = _run_subprocess(
        ["submit", "echo", "f", "--after", e_id], child_env
    )
    assert proc.returncode == 5, (
        f"a submit that would extend chain depth past the cap must "
        f"exit 5; got {proc.returncode}; stderr={proc.stderr!r}"
    )
    # The canonical stderr template (SPEC §7 line 389).
    assert "dependency chain too deep: 6 > 5" in proc.stderr, (
        f"stderr must match the canonical 'dependency chain too "
        f"deep: 6 > 5' template; got {proc.stderr!r}"
    )


# ===========================================================================
# In-process coverage tests
# ---------------------------------------------------------------------------
# The four cases above are the canonical TEST_SPEC.md §FR-06 rows. The
# tests below are additive: they exercise the same FR-06 surface
# (topological order, cycle detection, depth cap, depends_on field) through
# direct in-process calls so coverage tooling can measure the new
# handlers that live in `taskq_plus.service.dag` and
# `taskq_plus.models.task` (the subprocess acceptance path cannot raise
# coverage on those — see GATE1 SUBPROCESS COVERAGE CEILING in the
# integration guidelines). Both modules are declared in
# `SAB.json` §fr_module_traceability for FR-06; their on-disk presence is
# enforced by the Architecture Amendment Protocol.
# ===========================================================================


# ---------------------------------------------------------------------------
# `taskq_plus.service.dag` — declared SAB module for FR-06
# ---------------------------------------------------------------------------


# NFR-09
def test_fr06_dag_module_is_importable():
    """The `service.dag` module is declared in SAB.json
    §fr_module_traceability for FR-06. Its on-disk presence is
    enforced by the Architecture Amendment Protocol.

    GREEN TODO: `taskq_plus.service.dag` must exist as a leaf module
    (or a package) and expose the DAG surface — at minimum a
    `topo_sort(deps: Mapping[str, Sequence[str]]) -> Tuple[List[str],
    List[str]]` helper that returns `(order, remaining)` per Kahn's
    algorithm, plus a `cycle_path(deps, remaining) -> List[str]`
    helper that names one concrete cycle, and a `chain_depths(deps,
    order) -> Dict[str, int]` helper that returns the longest
    dependency-chain length ending at each node.
    """
    import taskq_plus.service.dag as dag_module  # noqa: F401
    assert dag_module is not None


# NFR-09
def test_fr06_dag_topo_sort_returns_order_then_remaining_for_acyclic_graph():
    """`topo_sort` returns `(order, remaining)` where `order` lists
    the nodes in dependency-satisfied order and `remaining` is empty
    for an acyclic graph.

    GREEN TODO: `taskq_plus.service.dag.topo_sort(deps)` must
    implement Kahn's algorithm and return both lists; `remaining`
    is the set of nodes Kahn could never reach (non-empty exactly
    when the graph contains a cycle).
    """
    from taskq_plus.service.dag import topo_sort

    # Acyclic chain a <- b <- c: dependencies {"c": ["b"], "b": ["a"]}.
    deps = {"a": [], "b": ["a"], "c": ["b"]}
    order, remaining = topo_sort(deps)

    assert remaining == [], (
        f"acyclic graph must have empty remaining; got {remaining!r}"
    )
    # Topological invariant: every prerequisite precedes its dependent.
    index = {node: i for i, node in enumerate(order)}
    assert index["a"] < index["b"] < index["c"], (
        f"topological order must respect every edge; got {order!r}"
    )


# NFR-09
def test_fr06_dag_topo_sort_returns_non_empty_remaining_for_cyclic_graph():
    """`topo_sort` returns a non-empty `remaining` list when the
    graph contains a cycle. The cycle members are exactly the nodes
    in `remaining`.

    GREEN TODO: `topo_sort` must surface the cyclic nodes via
    `remaining` so the caller can extract the cycle path with
    `cycle_path`.
    """
    from taskq_plus.service.dag import topo_sort

    # Cycle a <-> b plus a leaf c. a and b form the cycle; c is a
    # node with no incoming edge from the cycle.
    deps = {"a": ["b"], "b": ["a"], "c": []}
    order, remaining = topo_sort(deps)

    assert sorted(remaining) == ["a", "b"], (
        f"cyclic graph must surface cycle nodes via remaining; "
        f"got {remaining!r}"
    )
    assert "c" in order, (
        f"acyclic nodes reachable without traversing the cycle "
        f"must appear in order; got {order!r}"
    )


# NFR-09
def test_fr06_dag_cycle_path_returns_nodes_joined_by_arrow():
    """`cycle_path(deps, remaining)` returns a list whose ` → `-joined
    form names one concrete cycle.

    GREEN TODO: `cycle_path` must walk prerequisites inside
    `remaining` until a node revisits, then emit the closed loop
    `[start, ..., revisit]`.
    """
    from taskq_plus.service.dag import cycle_path

    deps = {"a": ["b"], "b": ["a"]}
    remaining = ["a", "b"]
    path = cycle_path(deps, remaining)

    # The path is a list; its ` → `-joined form must close the cycle.
    joined = " → ".join(path)
    assert path[0] == path[-1], (
        f"cycle_path must start and end with the same node to close "
        f"the cycle; got {path!r}"
    )
    assert "→" in joined, (
        f"the joined cycle path must use the `→` separator; "
        f"got {joined!r}"
    )


# NFR-09
def test_fr06_dag_chain_depths_returns_longest_path_per_node():
    """`chain_depths(deps, order)` returns the longest
    dependency-chain length ending at each node.

    GREEN TODO: walking `order` (already dependency-satisfied)
    guarantees every prerequisite's depth is known before its
    dependent is visited. The depth at a leaf is 1; the depth at
    a dependent is `1 + max(prereq_depth for prereq in deps[node])`.
    """
    from taskq_plus.service.dag import chain_depths

    # Chain a <- b <- c <- d (d depends on c, c depends on b, etc.).
    deps = {"a": [], "b": ["a"], "c": ["b"], "d": ["c"]}
    order = ["a", "b", "c", "d"]
    depths = chain_depths(deps, order)

    assert depths == {"a": 1, "b": 2, "c": 3, "d": 4}, (
        f"chain_depths must return the longest path per node; "
        f"got {depths!r}"
    )


# NFR-09
def test_fr06_dag_chain_depths_handles_diamond_topology():
    """`chain_depths` returns the MAX of prereq depths for diamonds.

    GREEN TODO: in a diamond (d depends on b and c, both depend on a),
    d's depth is `1 + max(depth(b), depth(c)) = 1 + 2 = 3`.
    """
    from taskq_plus.service.dag import chain_depths

    deps = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
    order = ["a", "b", "c", "d"]
    depths = chain_depths(deps, order)

    assert depths == {"a": 1, "b": 2, "c": 2, "d": 3}, (
        f"diamond depths must take the max of prereq depths; "
        f"got {depths!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.models.task` — Task.depends_on (FR-06)
# ---------------------------------------------------------------------------


# NFR-09
def test_fr06_task_model_depends_on_defaults_to_empty_list():
    """A fresh `Task` carries `depends_on = []` by default.

    GREEN TODO: `Task` must initialise `depends_on` to an empty list
    so the in-memory representation is consistent with the on-disk
    shape (SPEC §3 FR-06 line 144: `--after` 建立 `depends_on` 邊).
    """
    from taskq_plus.models.task import Task

    task = Task(command="echo hi")
    assert task.depends_on == [], (
        f"a fresh Task must default to depends_on=[]; got "
        f"{task.depends_on!r}"
    )


# NFR-09
def test_fr06_task_model_depends_on_accepts_multiple_edges():
    """`Task.depends_on` accepts multiple ids — each `--after`
    occurrence adds one edge (SPEC §3 FR-06 line 143).

    GREEN TODO: `Task` must accept a `depends_on` list with multiple
    ids and preserve the order the caller supplied.
    """
    from taskq_plus.models.task import Task

    task = Task(command="echo c", depends_on=["a", "b"])
    assert task.depends_on == ["a", "b"], (
        f"Task.depends_on must preserve the supplied edge list; "
        f"got {task.depends_on!r}"
    )


# ---------------------------------------------------------------------------
# `taskq_plus.cli.commands.run --all` — in-process dispatch surface
# ---------------------------------------------------------------------------


# NFR-09
def test_fr06_in_process_run_all_marks_blocked_when_prereq_failed(
    taskq_home, monkeypatch
):
    """`commands.run(["--all"])` marks the downstream task `blocked`
    when its prerequisite fails. The blocked task is NOT executed
    and does NOT increment the breaker failure counter.

    GREEN TODO: the in-process `run --all` path must:
      - dispatch pending tasks through the DAG topological order
      - mark a downstream task whose prerequisite ended non-done as
        `blocked`
      - skip execution of the blocked task entirely
      - leave the breaker failure counter untouched for the blocked
        task
    """
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "60")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "5")

    # Seed two tasks directly through the disk store: a is `false`
    # (guaranteed non-zero exit), b is `echo b` with a as prereq.
    dstore = make_disk_store()
    a_task = dstore.add(Task(command="false"))
    b_task = dstore.add(Task(command="echo b", depends_on=[a_task.id]))

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = commands.run(["--all"], use_disk=True)

    assert exit_code == 0, (
        f"in-process run --all must exit 0; got {exit_code}; "
        f"stderr={err.getvalue()!r}"
    )

    # Reload and assert the blocked surface.
    reset_store_cache()
    reloaded = {t.id: t for t in get_store(use_disk=True).all()}
    assert reloaded[a_task.id].status == "failed", (
        f"a must be failed after run --all; got "
        f"{reloaded[a_task.id].status!r}"
    )
    assert reloaded[b_task.id].status == "blocked", (
        f"b must be blocked after run --all (its prereq a failed); "
        f"got {reloaded[b_task.id].status!r}"
    )


# NFR-09
def test_fr06_in_process_run_all_runs_dependent_after_prereq_done(
    taskq_home, monkeypatch
):
    """`commands.run(["--all"])` runs the dependent task ONLY after
    the prerequisite reaches `done`. Both tasks end in `done`.

    GREEN TODO: the in-process `run --all` path must dispatch tasks
    in topological order — a dependent cannot start until every
    prerequisite is `done`. The dependent's `started_at`/`finished_at`
    must follow the prerequisite's `finished_at`.
    """
    from taskq_plus.cli import commands
    from taskq_plus.models.task import Task
    from taskq_plus.storage.task_store import (
        get_store,
        make_disk_store,
        reset_store_cache,
    )

    reset_store_cache()
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "60")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "5")

    dstore = make_disk_store()
    # `a` sleeps so its finished_at is reliably later than b's start.
    a_task = dstore.add(Task(command="sleep 0.3"))
    b_task = dstore.add(Task(command="echo b-done", depends_on=[a_task.id]))

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = commands.run(["--all"], use_disk=True)

    assert exit_code == 0, (
        f"in-process run --all must exit 0; got {exit_code}; "
        f"stderr={err.getvalue()!r}"
    )

    reset_store_cache()
    reloaded = {t.id: t for t in get_store(use_disk=True).all()}
    assert reloaded[a_task.id].status == "done", (
        f"a must be done; got {reloaded[a_task.id].status!r}"
    )
    assert reloaded[b_task.id].status == "done", (
        f"b must be done (its prereq a is done); got "
        f"{reloaded[b_task.id].status!r}"
    )

    a_finished = _datetime_to_ts(reloaded[a_task.id].finished_at)
    b_finished = _datetime_to_ts(reloaded[b_task.id].finished_at)
    assert a_finished > 0.0 and b_finished > 0.0, (
        f"both tasks must have finished_at timestamps; "
        f"a={reloaded[a_task.id].finished_at!r}, "
        f"b={reloaded[b_task.id].finished_at!r}"
    )
    assert a_finished < b_finished, (
        f"topological order: b must finish AFTER a; got "
        f"a={a_finished}, b={b_finished}"
    )
