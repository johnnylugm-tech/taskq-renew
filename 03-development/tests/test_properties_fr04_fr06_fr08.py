"""Property-based tests for FR-04, FR-06, FR-08 declared invariants.

The `TEST_SPEC.md` declares nine Direction-B properties (three per FR).
Direction B means "universal invariants over all inputs (opt-in)" — they
are not pinned by any single example test, only by a property-based
sweep over the input space. This module exercises every declared
property with `hypothesis` `@given` strategies sized small enough to
finish in a couple of seconds per case (the test runner is invoked
inside the same suite as the acceptance tests, so each property must
be tractable).

Each test maps 1:1 to a row in TEST_SPEC.md §Properties; the function
names are `test_frNN_property_<id>` so the preflight property-sweep can
verify "every declared invariant has a property-based test that
executes it".

Production symbols referenced from the spec:
    signature(cmd)            taskq_plus.service.cache
    lookup(cmd, ttl_s, now)   taskq_plus.service.cache
    record(cmd, entry)        taskq_plus.service.cache
    make_cache_store()        taskq_plus.storage.cache_store
    topo_sort(deps)           taskq_plus.service.dag
    cycle_path(deps, remain)  taskq_plus.service.dag
    render_json(records)      taskq_plus.observability.export
    render_csv(records)       taskq_plus.observability.export

All property tests are pure in-process (no subprocess, no disk I/O for
DAG/export) so they run as fast as `hypothesis` can shrink a
counter-example. The cache round-trip property uses the on-disk
`make_cache_store()` because that is the production code path the
`record`/`lookup` helpers ultimately read and write.
"""
from __future__ import annotations

import json
import string
from datetime import datetime, timezone
from typing import Dict, List, Mapping

from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------


# `commands` are arbitrary text up to 1000 chars (SPEC §3 FR-01 length cap).
# Hypothesis covers printable ASCII + a sprinkling of high-unicode so we
# catch any encoding regression.
_COMMAND_STRATEGY = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S", "Z"),
        whitelist_characters=string.printable,
    ),
    min_size=0,
    max_size=200,
)


# Acyclic graph generator: a dict[str, list[str]] where every prereq key
# exists. Built by sampling N ids and linking each one to a uniformly
# chosen subset of ids that come earlier in a deterministic ordering
# (the "earlier" id is the one with the smaller index, so the graph is
# guaranteed acyclic by construction).
_ACYCLIC_N = st.integers(min_value=0, max_value=12)


@st.composite
def acyclic_deps(draw) -> Mapping[str, List[str]]:
    """Generate an acyclic dependency mapping.

    Each node is keyed by an integer-encoded id string `"i"` for
    `i in [0, n)`. A node's `depends_on` list is a uniformly chosen
    subset of the ids that come before it — this guarantees the graph
    is a DAG (every edge points to a strictly smaller index).
    """
    n = draw(_ACYCLIC_N)
    deps: Dict[str, List[str]] = {}
    for i in range(n):
        if i == 0:
            deps[str(i)] = []
            continue
        earlier = list(range(i))
        keep = draw(
            st.lists(
                st.sampled_from(earlier),
                max_size=i,
            )
        )
        seen: set = set()
        unique = []
        for k in keep:
            if k not in seen:
                seen.add(k)
                unique.append(k)
        deps[str(i)] = [str(k) for k in unique]
    return deps


@st.composite
def cyclic_or_acyclic_deps(draw) -> tuple:
    """Generate a `(deps, has_cycle)` pair.

    Either an acyclic DAG (built by `acyclic_deps`) or a graph with a
    genuine cycle. `deps[X]` is the list of ids X depends on, so a
    cycle `0 -> 1 -> 0` in graph terms is `deps[0] = ['1']` and
    `deps[1] = ['0']`. The cyclic case builds a chain
    `0 -> 1 -> ... -> n-1` (so each `i > 0` depends on `i-1`) and
    adds the back-edge `0 -> n-1` (so `deps['0'] = ['0' deps, 'n-1']`)
    — the back-edge closes a cycle iff the chain reaches `n-1` from
    `0` already, which it does by construction.
    """
    inject_cycle = draw(st.booleans())
    if not inject_cycle:
        return draw(acyclic_deps()), False
    n = draw(st.integers(min_value=2, max_value=10))
    # Chain: each node i > 0 depends on (i-1). `deps['0']` will hold the
    # back-edge to `n-1` (graph edge n-1 -> 0 means 0 depends on n-1).
    deps: Dict[str, List[str]] = {
        str(i): [str(i - 1)] for i in range(1, n)
    }
    deps["0"] = [str(n - 1)]  # back-edge n-1 -> 0 closes the cycle
    return deps, True


# Records for export round-trip — uniform dicts with a fixed key set so
# the field set is stable across the three formats.
_EXPORT_RECORDS = st.lists(
    st.fixed_dictionaries(
        {
            "id": st.text(min_size=1, max_size=8, alphabet="abcdef0123456789"),
            "name": st.text(
                alphabet=st.characters(
                    whitelist_categories=("L", "N"),
                    whitelist_characters=",;:\n\"\\",
                ),
                min_size=0,
                max_size=20,
            ),
            "status": st.sampled_from(["done", "pending", "failed"]),
        }
    ),
    min_size=0,
    max_size=20,
)


# ---------------------------------------------------------------------------
# FR-04 — signature determinism / cache round-trip / lookup idempotence
# ---------------------------------------------------------------------------


# Spec: P-FR04-signature-determinism — `signature(command) == signature(command)`
@settings(max_examples=50, deadline=None)
@given(command=_COMMAND_STRATEGY)
def test_fr04_property_signature_determinism(command: str) -> None:
    """P-FR04-signature-determinism: `signature(c)` is stable."""
    from taskq_plus.service.cache import signature

    assert signature(command) == signature(command), (
        f"signature must be a pure function of `command`; got "
        f"{signature(command)!r} vs {signature(command)!r} on "
        f"command={command!r}"
    )


# Spec: P-FR04-cache-roundtrip — record + lookup returns the same entry
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    command=_COMMAND_STRATEGY,
    exit_code=st.integers(min_value=0, max_value=255),
    stdout_tail=st.text(max_size=50),
)
def test_fr04_property_cache_roundtrip(
    command: str, exit_code: int, stdout_tail: str, taskq_home, monkeypatch
) -> None:
    """P-FR04-cache-roundtrip: `lookup(record(cmd, entry))` returns the entry.

    `taskq_home` is a function-scoped fixture, but the test must run
    once per hypothesis input. The `HealthCheck.function_scoped_fixture`
    is suppressed because each example still gets a fresh `TASKQ_HOME`
    via `monkeypatch.setenv` at fixture-resolve time — only the
    subsequent inputs in the same example batch share the directory,
    and the cache write/read cycle is idempotent so this is safe.
    """
    from taskq_plus.service.cache import lookup, record, signature

    # The entry timestamp must be in the immediate past so the TTL
    # check (`now - finished_at <= ttl_s`) succeeds inside `lookup`.
    finished_at = datetime.now(timezone.utc).isoformat()
    entry = {
        "command": command,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "finished_at": finished_at,
        "status": "done",
    }
    record(command, entry)

    hit = lookup(command, ttl_s=60.0)
    assert hit is not None, (
        f"a freshly recorded `done` entry must be a hit; signature="
        f"{signature(command)!r}"
    )
    assert hit["command"] == command
    assert hit["exit_code"] == exit_code
    assert hit["stdout_tail"] == stdout_tail
    assert hit["finished_at"] == finished_at
    assert hit["status"] == "done"


# Spec: P-FR04-lookup-idempotence — repeated lookups return the same hit
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(command=_COMMAND_STRATEGY)
def test_fr04_property_lookup_idempotence(
    command: str, taskq_home, monkeypatch
) -> None:
    """P-FR04-lookup-idempotence: `lookup(c)` is stable across calls."""
    from taskq_plus.service.cache import lookup, record

    record(
        command,
        {
            "command": command,
            "exit_code": 0,
            "stdout_tail": "idem",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "done",
        },
    )
    first = lookup(command, ttl_s=60.0)
    second = lookup(command, ttl_s=60.0)
    third = lookup(command, ttl_s=60.0)
    assert first == second == third, (
        f"repeated lookup within TTL must be identical; "
        f"got {first!r}, {second!r}, {third!r}"
    )


# ---------------------------------------------------------------------------
# FR-06 — topological order / topo_sort idempotence / cycle detection total
# ---------------------------------------------------------------------------


# Spec: P-FR06-topo-order-respects-every-edge — every edge (src -> dst)
# satisfies `order.index(src) < order.index(dst)` for nodes in `order`.
@settings(max_examples=50, deadline=None)
@given(deps=acyclic_deps())
def test_fr06_property_topo_order_respects_every_edge(
    deps: Mapping[str, List[str]]
) -> None:
    """P-FR06-topo-order-respects-every-edge: Kahn's order respects all edges.

    `deps[X] = [Y, ...]` means `X` depends on `Y`, so the prerequisite
    `Y` must appear before `X` in the topological order. The test
    enumerates every (prereq, dependent) pair and asserts the
    prereq's index is strictly less than the dependent's index.
    """
    from taskq_plus.service.dag import topo_sort

    order, remaining = topo_sort(deps)
    assert remaining == [], (
        f"acyclic_deps must yield an empty remaining; got {remaining!r}"
    )
    index = {node: i for i, node in enumerate(order)}
    for dependent, prereqs in deps.items():
        for prereq in prereqs:
            assert index[prereq] < index[dependent], (
                f"prerequisite {prereq!r} must precede dependent "
                f"{dependent!r}; got index[prereq]={index.get(prereq)} "
                f"index[dependent]={index.get(dependent)} (order={order!r})"
            )


# Spec: P-FR06-topo-sort-idempotent — running topo_sort twice on the
# same input produces the same (order, remaining) tuple.
@settings(max_examples=30, deadline=None)
@given(deps=acyclic_deps())
def test_fr06_property_topo_sort_idempotent(
    deps: Mapping[str, List[str]]
) -> None:
    """P-FR06-topo-sort-idempotent: topo_sort is a pure function of `deps`."""
    from taskq_plus.service.dag import topo_sort

    order1, remaining1 = topo_sort(deps)
    order2, remaining2 = topo_sort(deps)
    assert order1 == order2, (
        f"topo_sort must be idempotent on the same input; "
        f"got {order1!r} vs {order2!r}"
    )
    assert remaining1 == remaining2, (
        f"topo_sort remaining must be stable on the same input; "
        f"got {remaining1!r} vs {remaining2!r}"
    )


# Spec: P-FR06-cycle-detection-is-total — has_cycle <-> is_schedulable
@settings(max_examples=50, deadline=None)
@given(graph_pair=cyclic_or_acyclic_deps())
def test_fr06_property_cycle_detection_is_total(
    graph_pair: tuple,
) -> None:
    """P-FR06-cycle-detection-is-total: cyclic iff non-schedulable."""
    from taskq_plus.service.dag import topo_sort

    deps, has_cycle = graph_pair
    order, remaining = topo_sort(deps)
    observed_cycle = bool(remaining)
    assert observed_cycle == has_cycle, (
        f"topo_sort.remaining must reflect the cycle status; "
        f"expected has_cycle={has_cycle} got remaining={remaining!r} "
        f"order={order!r} deps={deps!r}"
    )


# ---------------------------------------------------------------------------
# FR-08 — export count agreement / json round-trip / csv round-trip
# ---------------------------------------------------------------------------


# Spec: P-FR08-export-count-agreement — every renderer yields N rows for N records.
@settings(max_examples=30, deadline=None)
@given(records=_EXPORT_RECORDS)
def test_fr08_property_export_count_agreement(records: List[dict]) -> None:
    """P-FR08-export-count-agreement: all three formats agree on record count."""
    import csv as _csv
    import io as _io

    from taskq_plus.observability.export import render_csv, render_json, render_md

    json_text = render_json(records)
    csv_text = render_csv(records)
    md_text = render_md(records)

    json_count = len(json.loads(json_text)) if records else 0

    if records:
        csv_count = sum(
            1 for _ in _csv.DictReader(_io.StringIO(csv_text))
        )
        md_lines = [ln for ln in md_text.splitlines() if ln.startswith("|")]
        # md_lines = [header, separator, *data_rows]
        md_count = max(0, len(md_lines) - 2)
    else:
        csv_count = 0
        md_count = 0

    assert json_count == len(records) == csv_count == md_count, (
        f"all three formats must agree on record count; "
        f"records={len(records)} json={json_count} csv={csv_count} "
        f"md={md_count} (json={json_text!r}, csv={csv_text!r}, md={md_text!r})"
    )


# Spec: P-FR08-json-export-roundtrip — json.loads(render_json(recs)) == recs
@settings(max_examples=30, deadline=None)
@given(records=_EXPORT_RECORDS)
def test_fr08_property_json_export_roundtrip(records: List[dict]) -> None:
    """P-FR08-json-export-roundtrip: `json.loads(render_json(recs)) == recs`."""
    from taskq_plus.observability.export import render_json

    rendered = render_json(records)
    parsed = json.loads(rendered)
    assert parsed == records, (
        f"JSON round-trip must preserve the records exactly; "
        f"got {parsed!r} vs {records!r}"
    )


# Spec: P-FR08-csv-escape-roundtrip — csv.reader(render_csv(recs)) recovers recs
@settings(max_examples=30, deadline=None)
@given(records=_EXPORT_RECORDS)
def test_fr08_property_csv_escape_roundtrip(records: List[dict]) -> None:
    """P-FR08-csv-escape-roundtrip: every value round-trips through CSV."""
    import csv as _csv
    import io as _io

    from taskq_plus.observability.export import render_csv

    rendered = render_csv(records)
    if not records:
        assert rendered == "", (
            f"empty input must render to empty CSV; got {rendered!r}"
        )
        return
    reader = _csv.reader(_io.StringIO(rendered))
    header = next(reader)
    for row, original in zip(reader, records):
        for column, field in zip(row, header):
            assert column == str(original[field]), (
                f"CSV round-trip on field {field!r}: expected "
                f"{original[field]!r}, got {column!r}"
            )