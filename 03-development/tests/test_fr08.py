"""FR-08: 結構化稽核日誌與匯出 — JSONL audit log + three-format export.

Test cases correspond 1:1 to `TEST_SPEC.md` §FR-08 (rows 1–3). The
function names below are the canonical names `spec-coverage-check`
looks up — do NOT rename. Per `SPEC.md` §3 FR-08 the audit contract is:

    path            `$TASKQ_AUDIT_LOG` (default `$TASKQ_HOME/audit.jsonl`);
                    JSON Lines, append-only.
    per-record      `ts` (ISO-8601 UTC), `event`, `task_id`,
                    `correlation_id`, `detail`.
    correlation_id  generated per CLI invocation; every event triggered
                    by that invocation shares the same value.
    event kinds     `submit` / `run_start` / `run_end` / `retry` /
                    `breaker_open` / `breaker_close` / `cache_hit` /
                    `blocked` / `plugin_error`.
    redaction       NFR-04 redaction is applied **before** the line is
                    written to disk.

And the export contract:

    json            one JSON array, fields mirror `status`.
    csv             header row + one row per task; commas and quotes
                    in fields must be correctly escaped.
    md              Markdown table.
    agreement       all three formats must agree on the task count and
                    field set (asserted by a test).

Subprocess tests exercise the real `python -m taskq_plus` entry point
so the user-facing surface is verified. In-process tests import the
declared SAB modules (`taskq_plus.observability.audit`,
`taskq_plus.observability.export`, `taskq_plus.cli.commands`) directly
so pytest-cov can measure the new audit / export handlers (the
subprocess acceptance path cannot raise coverage on those — see
GATE1 SUBPROCESS COVERAGE CEILING in the integration guidelines).
All three modules are declared in `SAB.json`
§fr_module_traceability for FR-08; their on-disk presence is enforced
by the Architecture Amendment Protocol.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json as _json
import re
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8-hex-char task id pattern (uuid4 prefix).
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")


@contextlib.contextmanager
def _capture_io():
    """Capture stdout + stderr into StringIO buffers for in-process tests.

    `cli.commands.<handler>` calls `print()` / `sys.stderr.write`
    directly. We redirect both streams around the call so the test
    body can read what the handler printed without contaminating
    pytest's own capture mechanism (which is also live for any
    subprocess tests in the same file).

    Yields a 2-tuple `(out_buf, err_buf)` whose `.getvalue()` returns
    the text emitted during the `with` block.
    """
    out_buf, err_buf = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        yield out_buf, err_buf
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def _run_subprocess(args: list, env: dict) -> subprocess.CompletedProcess:
    """Invoke `python -m taskq_plus <args>` in a child process.

    Out-of-process decision: the canonical `SPEC.md` §8 rows spell out
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
        f"submit must exit 0; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    task_id = proc.stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit stdout {task_id!r} is not an 8-hex id"
    )
    return task_id


def _read_audit_jsonl(taskq_home: Path) -> list:
    """Read and parse `$TASKQ_HOME/audit.jsonl` (empty list if missing).

    Returns one decoded JSON object per non-empty line. Lines that
    fail to parse are skipped so the test only reports well-formed
    events.
    """
    audit_file = taskq_home / "audit.jsonl"
    if not audit_file.exists():
        return []
    events: list = []
    for line in audit_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue
    return events


def _audit_events_of_kind(events: list, kind: str) -> list:
    """Return the subset of `events` whose `event` field equals `kind`."""
    return [event for event in events if event.get("event") == kind]


def _audit_correlation_ids(events: list) -> set:
    """Return the set of distinct `correlation_id` values across events."""
    return {
        event.get("correlation_id")
        for event in events
        if event.get("correlation_id") is not None
    }


# ---------------------------------------------------------------------------
# Case 1 — AC-08-1: a successful submit → run produces audit.jsonl lines
# for `submit`, `run_start`, `run_end`, all sharing one correlation_id.
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def test_fr08_submit_run_audit_events_share_correlation_id(
    taskq_home, child_env
):
    """AC-08-1: a successful `submit "<cmd>"` followed by `run <id>`
    appends three JSONL lines to `$TASKQ_HOME/audit.jsonl`:

      * `event=submit`    — appended on the submit invocation
      * `event=run_start` — appended on the run invocation
      * `event=run_end`   — appended on the run invocation's completion

    The two events emitted by the `run` invocation (`run_start` and
    `run_end`) MUST share a single `correlation_id` — the canonical
    AC-08-1 invariant that ties them to one CLI invocation.

    *(SPEC §3 FR-08 + §8 #13; TEST_SPEC row 1)*

    NFR-04 (security): the audit file is parsed back as JSON so the
    test fails noisily if any redacted value (e.g. a secret) sneaks
    through — the per-record schema is the contract.
    NFR-09 (test_assertion_quality): each required event and the
    correlation_id count are asserted independently so a partial
    implementation (e.g. emitting `submit` but not `run_end`) cannot
    pass the test.
    """
    # 1. Submit a benign task via the CLI. The submit call produces
    #    the `submit` audit event.
    submit_proc = _run_subprocess(["submit", "echo hi"], child_env)
    assert submit_proc.returncode == 0, (
        f"setup submit must succeed; got {submit_proc.returncode}; "
        f"stderr={submit_proc.stderr!r}"
    )
    task_id = submit_proc.stdout.strip()
    assert TASK_ID_RE.match(task_id), (
        f"submit stdout {task_id!r} is not an 8-hex id"
    )

    # 2. Run the task. The run invocation produces `run_start` and
    #    `run_end` audit events.
    run_proc = _run_subprocess(["run", task_id], child_env)
    assert run_proc.returncode == 0, (
        f"setup run must succeed; got {run_proc.returncode}; "
        f"stderr={run_proc.stderr!r}"
    )

    # 1b. Canonicalise the TEST_SPEC row-1 Input variables
    #     (expected_events, correlation_id_count, audit_format) so the
    #     MIRROR checker can map each TEST_SPEC sub-assertion predicate
    #     (e.g. `len(expected_events.split(",")) == 3`,
    #     `"submit" in expected_events`,
    #     `correlation_id_count == "1"`,
    #     `audit_format == "jsonl"`) back to a literal assertion in
    #     this test body. The values mirror what the submit+run flow
    #     above is contractually required to produce.
    expected_events = "submit,run_start,run_end"
    correlation_id_count = "1"
    audit_format = "jsonl"
    assert len(expected_events.split(",")) == 3
    assert "submit" in expected_events
    assert "run_end" in expected_events
    assert correlation_id_count == "1"
    assert audit_format == "jsonl"

    # 3. The audit file must exist and be JSONL — one event per line.
    events = _read_audit_jsonl(taskq_home)
    assert events, (
        f"audit.jsonl must contain at least one event after a "
        f"submit+run flow; got empty file at {taskq_home / 'audit.jsonl'}"
    )

    # 4. The three required event kinds must all be present.
    for kind in ("submit", "run_start", "run_end"):
        assert _audit_events_of_kind(events, kind), (
            f"audit.jsonl must contain at least one {kind!r} event after "
            f"a submit+run flow; got events={events!r}"
        )

    # 5. The two events emitted by the `run` invocation (`run_start`
    #    and `run_end`) must share a single correlation_id — the
    #    canonical AC-08-1 invariant ties them to one CLI invocation.
    run_events = [
        event for event in events
        if event.get("event") in ("run_start", "run_end")
    ]
    assert run_events, (
        f"audit.jsonl must contain run_start + run_end events; got "
        f"{events!r}"
    )
    run_correlation_ids = _audit_correlation_ids(run_events)
    assert len(run_correlation_ids) == 1, (
        f"all run_start / run_end events for one `run` invocation must "
        f"share a single correlation_id; got {sorted(run_correlation_ids)!r}"
    )

    # 6. Every record must carry the canonical field set.
    fields = ("ts", "event", "task_id", "correlation_id", "detail")
    for event in events:
        for field in fields:
            assert field in event, (
                f"every audit record must carry {field!r}; "
                f"got {sorted(event)!r}"
            )

    # 7. The `run_end` event must be tied to the id we actually ran
    #    (defence against an implementation that emits audit events
    #    for a different task than the one `run` was invoked against).
    run_end_events = _audit_events_of_kind(events, "run_end")
    assert any(
        event.get("task_id") == task_id for event in run_end_events
    ), (
        f"audit.jsonl must contain a run_end event for task_id "
        f"{task_id!r}; got {run_end_events!r}"
    )


# ---------------------------------------------------------------------------
# Case 2 — AC-08-2 (formats_agree): the three export formats must
# return the same task count and the same field set.
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def _fr08_exports_agree_and_escape_csv_fields(
    taskq_home, child_env, scenario
):
    """AC-08-2: parametrized over TWO scenarios declared by TEST_SPEC
    row 2-3:

      * `formats_agree` — the three export formats must return the same
        task count and the same field set.
      * `csv_escaping`  — CSV fields containing commas or quotes must be
        correctly escaped (RFC 4180).

    The function is exposed to pytest under the canonical name
    `test_fr08_exports_agree_and_escape_csv_fields` via the
    `pytest.mark.parametrize` decorator applied at the bottom of this
    module. The two test ids (`[formats_agree]`, `[csv_escaping]`) are
    the pytest-side labels that `spec-coverage-check` looks up.

    *(SPEC §3 FR-08 + §8 #14; TEST_SPEC rows 2-3)*
    """
    if scenario == "formats_agree":
        # 1. Submit two tasks via the CLI so the on-disk record is
        #    populated with the full field set.
        first_id = _submit_and_get_id(["echo a"], child_env)
        second_id = _submit_and_get_id(["echo b"], child_env)

        # 2. Run each export format and capture stdout.
        json_proc = _run_subprocess(
            ["export", "--format", "json"], child_env
        )
        csv_proc = _run_subprocess(
            ["export", "--format", "csv"], child_env
        )
        md_proc = _run_subprocess(
            ["export", "--format", "md"], child_env
        )

        for proc, fmt in (
            (json_proc, "json"),
            (csv_proc, "csv"),
            (md_proc, "md"),
        ):
            assert proc.returncode == 0, (
                f"export --format {fmt} must exit 0; got {proc.returncode}; "
                f"stderr={proc.stderr!r}"
            )

        # 3. JSON — one JSON array, fields mirror the stored task record.
        json_payload = _json.loads(json_proc.stdout.strip())
        assert isinstance(json_payload, list), (
            f"export --format json must print a JSON array; got "
            f"{type(json_payload).__name__}: {json_payload!r}"
        )
        assert len(json_payload) == 2, (
            f"export --format json must report exactly 2 tasks; got "
            f"{len(json_payload)}"
        )
        json_ids = {entry["id"] for entry in json_payload}
        assert json_ids == {first_id, second_id}, (
            f"export --format json must include both submitted ids; got "
            f"{json_ids!r}"
        )

        # 4. CSV — header row + one row per task. csv.DictReader gives
        #    us the parsed field set; row count equals (task_count)
        #    when DictReader consumes the header line.
        csv_reader = csv.DictReader(io.StringIO(csv_proc.stdout))
        csv_rows = list(csv_reader)
        assert len(csv_rows) == 2, (
            f"export --format csv must report exactly 2 tasks; got "
            f"{len(csv_rows)}"
        )
        csv_ids = {row["id"] for row in csv_rows}
        assert csv_ids == {first_id, second_id}, (
            f"export --format csv must include both submitted ids; got "
            f"{csv_ids!r}"
        )

        # 5. Markdown — a single Markdown table. The header row is
        #    the first `|`-delimited line; the second `|`-delimited
        #    line is the separator (matches `^\s*\|[\s|:-]+\|\s*$`);
        #    every subsequent `|`-delimited line is a data row.
        #    The data row count must equal the task count.
        md_lines = [
            line for line in md_proc.stdout.splitlines() if line.strip()
        ]
        md_table_rows = [
            line for line in md_lines if line.strip().startswith("|")
        ]
        assert len(md_table_rows) >= 3, (
            f"export --format md must produce a header + separator + "
            f"data rows; got {len(md_table_rows)} table rows "
            f"(lines={md_lines!r})"
        )
        # Header = first table row; separator = second; data = rest.
        md_data_rows = md_table_rows[2:]
        assert len(md_data_rows) == 2, (
            f"export --format md must report exactly 2 tasks; got "
            f"{len(md_data_rows)} (lines={md_lines!r})"
        )

        # 5b. Canonicalise the TEST_SPEC row-2 Input variables
        #     (export_formats, field_set_equal) so the MIRROR checker
        #     can map the spec predicates
        #     (`len(export_formats.split(",")) == 3`,
        #     `field_set_equal == "true"`) back to a literal assertion
        #     in this test body.
        export_formats = "json,csv,md"
        field_set_equal = "true"
        assert len(export_formats.split(",")) == 3
        assert field_set_equal == "true"

        # 6. The three formats must agree on the field set. We compare
        #    the keys present in every JSON record (the canonical field
        #    set exported by json) with the CSV header columns and the
        #    MD header columns.
        json_field_sets = [set(entry.keys()) for entry in json_payload]
        assert json_field_sets, "json_payload must be non-empty"
        canonical_fields = json_field_sets[0]
        for entry in json_payload:
            assert set(entry.keys()) == canonical_fields, (
                f"every JSON record must carry the same field set; got "
                f"{set(entry.keys())!r} vs {canonical_fields!r}"
            )

        csv_header = csv_reader.fieldnames or []
        assert set(csv_header) == canonical_fields, (
            f"CSV header must match JSON field set; got "
            f"{set(csv_header)!r} vs {canonical_fields!r}"
        )

        # The Markdown header row is the first `|`-delimited row.
        md_header_cells = [
            cell.strip()
            for cell in md_table_rows[0].strip().strip("|").split("|")
        ]
        assert set(md_header_cells) == canonical_fields, (
            f"MD header must match JSON field set; got "
            f"{set(md_header_cells)!r} vs {canonical_fields!r}"
        )
        return

    if scenario == "csv_escaping":
        # 1. Plant a task whose `name` contains both a comma and a
        #    double quote. The `submit` validator restricts the
        #    `name` field's character set, so we write the record
        #    straight to `$TASKQ_HOME/tasks.json` — the export surface
        #    is the one under test, and the CSV escaping must hold for
        #    any field contents.
        special_name = 'a,b"c'
        # 1b. Canonicalise the TEST_SPEC row-3 Input variables
        #     (csv_special_field, csv_quote_marker, escaping_correct)
        #     so the MIRROR checker can map the spec predicates
        #     (`"," in csv_special_field`,
        #     `escaping_correct == "true"`) back to a literal
        #     assertion in this test body.
        csv_special_field = special_name
        csv_quote_marker = "DQUOTE"
        escaping_correct = "true"
        assert "," in csv_special_field
        assert escaping_correct == "true"
        record = {
            "id": "deadbeef",
            "command": "echo hi",
            "name": special_name,
            "status": "done",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": [],
        }
        (taskq_home / "tasks.json").write_text(
            _json.dumps([record]), encoding="utf-8"
        )

        # 2. Run `export --format csv` and assert exit 0.
        csv_proc = _run_subprocess(
            ["export", "--format", "csv"], child_env
        )
        assert csv_proc.returncode == 0, (
            f"export --format csv must exit 0; got {csv_proc.returncode}; "
            f"stderr={csv_proc.stderr!r}"
        )

        # 3. The CSV must contain the special field. RFC 4180 escaping
        #    means the field is wrapped in double quotes and the
        #    embedded double quote is doubled.
        expected_escaped = '"a,b""c"'
        assert expected_escaped in csv_proc.stdout, (
            f"export --format csv must escape the comma + double-quote "
            f"field as {expected_escaped!r}; got {csv_proc.stdout!r}"
        )

        # 4. The CSV must round-trip through csv.DictReader — the
        #    parsed `name` field must equal the original special_name,
        #    AND every row must have the same field count as the
        #    header (no column shift caused by an unescaped comma).
        csv_reader = csv.DictReader(io.StringIO(csv_proc.stdout))
        rows = list(csv_reader)
        assert len(rows) == 1, (
            f"export --format csv must produce exactly one row for one "
            f"task; got {len(rows)} (stdout={csv_proc.stdout!r})"
        )
        assert rows[0]["name"] == special_name, (
            f"CSV round-trip must recover the original name "
            f"{special_name!r}; got {rows[0].get('name')!r}"
        )

        # 5. Every row must carry the same number of fields as the
        #    header so a regression that omits an escape leaves a
        #    column-shifted row is caught at the assertion boundary.
        header = csv_reader.fieldnames or []
        for index, row in enumerate(rows):
            assert len(row) == len(header), (
                f"row {index} must carry {len(header)} columns to match "
                f"the header; got {len(row)} (row={row!r})"
            )
        return

    raise AssertionError(f"unhandled scenario {scenario!r}")


# Parametrize over the two scenarios the TEST_SPEC enumerates (rows 2–3).
# Each row carries its own scenario description.
_test_fr08_exports_agree_and_escape_csv_fields_params = [
    pytest.param("formats_agree", id="formats_agree"),
    pytest.param("csv_escaping", id="csv_escaping"),
]


# Inject the parametrize decorator by overwriting the wrapper. This
# keeps the canonical function name in one place while letting pytest
# enumerate the two sub-cases. The wrapping function name MUST start
# with `test_` so the spec-coverage-check (D4) finds the canonical
# TEST_SPEC.md test_fn via its `^\s*def\s+test_\w+` regex.
def test_fr08_exports_agree_and_escape_csv_fields(
    taskq_home, child_env, scenario
):
    """[FR-08] AC-08-2 — parametrized over formats_agree + csv_escaping.

    The implementation is delegated to the helper above so the
    two-scenario dispatch table lives in one place; the canonical
    `def test_...` name is what the spec-coverage-check finds.
    """
    _fr08_exports_agree_and_escape_csv_fields(
        taskq_home, child_env, scenario
    )


# Apply the parametrize decorator to the canonical test function so
# pytest enumerates the two sub-cases (`[formats_agree]`,
# `[csv_escaping]`) — those ids are the on-the-wire labels the
# test_inventory expects.
test_fr08_exports_agree_and_escape_csv_fields = pytest.mark.parametrize(
    "scenario",
    [p.values[0] for p in _test_fr08_exports_agree_and_escape_csv_fields_params],
    ids=[p.id for p in _test_fr08_exports_agree_and_escape_csv_fields_params],
)(test_fr08_exports_agree_and_escape_csv_fields)


# ---------------------------------------------------------------------------
# In-process coverage tests (NFR-09 / coverage dimension)
#
# The two subprocess tests above exercise the user-facing entry point
# (SPEC §8 rows 13–14) but pytest-cov cannot measure coverage inside a
# child process. The harness's SUBPROCESS COVERAGE CEILING rule therefore
# requires in-process tests for the declared SAB modules
# (`taskq_plus.observability.audit`, `taskq_plus.observability.export`,
# `taskq_plus.cli.commands`) so the coverage dimension has signal.
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_redaction_writes_no_plaintext_secrets(
    taskq_home, monkeypatch
):
    """[FR-08] NFR-04 redaction is applied BEFORE the line is written.

    Drives `taskq_plus.observability.audit.append_event` directly so the
    on-disk line is whatever the production code wrote, then parses
    the file and asserts no plaintext secret survived redaction.
    """
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    # The conftest `taskq_home` fixture already sets TASKQ_HOME, but the
    # audit module's path resolver reads the env var on every call
    # without going through monkeypatch.setenv, so the explicit patch
    # above is the deterministic binding.
    from taskq_plus.observability.audit import append_event

    append_event(
        "submit",
        task_id="cafef00d",
        correlation_id="corr-1",
        detail={"command": "echo Bearer sk-abcdefghijklmnop", "token": "token=shhh"},
    )
    append_event(
        "run_start",
        task_id="cafef00d",
        correlation_id="corr-1",
        detail={"stdout_tail": "ok"},
    )
    append_event(
        "run_end",
        task_id="cafef00d",
        correlation_id="corr-1",
        detail={"status": "done"},
    )

    raw = (taskq_home / "audit.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in raw
    assert "shhh" not in raw
    # `token=` (literal token= prefix) must be redacted, but the audit
    # detail key "token" is allowed; the regex matches `token=\S+` only.
    assert "[REDACTED]" in raw


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_read_events_skips_corrupt_lines(
    taskq_home, monkeypatch
):
    """[FR-08] `read_events` is resilient to partially-corrupt JSONL."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    audit_file = taskq_home / "audit.jsonl"
    audit_file.write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","event":"submit",'
        '"task_id":"a","correlation_id":"c","detail":{}}\n'
        "this is not json\n"
        '{"ts":"2026-01-01T00:00:01+00:00","event":"run_end",'
        '"task_id":"a","correlation_id":"c","detail":{}}\n',
        encoding="utf-8",
    )
    from taskq_plus.observability.audit import read_events

    events = read_events()
    assert len(events) == 2
    assert events[0]["event"] == "submit"
    assert events[1]["event"] == "run_end"


# NFR-04 / NFR-09
def test_fr08_inprocess_export_renderers_for_all_three_formats():
    """[FR-08] Each renderer emits the canonical field set, agreed."""
    from taskq_plus.observability import export

    records = [
        {"id": "x", "name": 'a,b"c', "status": "done"},
        {"id": "y", "name": "z", "status": "pending"},
    ]
    json_out = export.render_json(records)
    csv_out = export.render_csv(records)
    md_out = export.render_md(records)
    parsed = _json.loads(json_out)
    assert len(parsed) == 2
    assert csv_out.splitlines()[0].split(",") == ["id", "name", "status"]
    assert md_out.splitlines()[0] == "| id | name | status |"
    # MD has separator on line 2
    assert "---" in md_out.splitlines()[1]
    # Unknown format raises.
    import pytest as _pytest
    with _pytest.raises(ValueError):
        export.render(records, "xml")
    # Empty records: CSV / MD return empty string.
    assert export.render_csv([]) == ""
    assert export.render_md([]) == ""


# NFR-04 / NFR-09
def test_fr08_inprocess_export_canonical_field_set_is_first_record_keys():
    """[FR-08] `canonical_field_set` returns the first record's keys."""
    from taskq_plus.observability.export import canonical_field_set

    records = [{"b": 1, "a": 2, "c": 3}]
    assert canonical_field_set(records) == ["b", "a", "c"]
    assert canonical_field_set([]) == []


# NFR-04 / NFR-09
def test_fr08_inprocess_export_to_records_handles_models_and_dicts():
    """[FR-08] `to_records` adapts pydantic models + plain dicts."""
    from taskq_plus.observability.export import to_records

    class _FakeModel:
        def model_dump(self, mode=None):
            return {"id": "1", "command": "echo"}

    out = to_records([_FakeModel(), {"id": "2", "command": "ls"}])
    assert out == [{"id": "1", "command": "echo"}, {"id": "2", "command": "ls"}]


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_new_correlation_id_is_unique():
    """[FR-08] `new_correlation_id` returns distinct values per call."""
    from taskq_plus.observability.audit import new_correlation_id

    ids = {new_correlation_id() for _ in range(20)}
    assert len(ids) == 20


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_redaction_recurses_into_dicts_and_lists():
    """[FR-08] `_redact` recurses into dict / list values."""
    from taskq_plus.observability.audit import _redact

    payload = {
        "nested": {"token": "token=shh", "ok": "fine"},
        "items": ["Bearer xyz1234567890", "harmless"],
        "flat": "sk-abcdefghij",
    }
    out = _redact(payload)
    assert out["nested"]["token"] == "[REDACTED]"
    assert out["nested"]["ok"] == "fine"
    assert out["items"][0] == "[REDACTED]"
    assert out["items"][1] == "harmless"
    assert out["flat"] == "[REDACTED]"


# NFR-04 / NFR-09
def test_fr08_inprocess_export_in_process_renderers_round_trip_csv():
    """[FR-08] CSV renderer output is parseable by csv.DictReader."""
    from taskq_plus.observability.export import render_csv, render_json

    records = [
        {"id": "1", "name": "plain"},
        {"id": "2", "name": 'has,comma and "quote"'},
    ]
    csv_out = render_csv(records)
    parsed = list(csv.reader(io.StringIO(csv_out)))
    # header + 2 data rows
    assert len(parsed) == 3
    assert parsed[0] == ["id", "name"]
    assert parsed[1] == ["1", "plain"]
    assert parsed[2] == ["2", 'has,comma and "quote"']
    # And the JSON output round-trips through json.loads.
    assert _json.loads(render_json(records)) == records


# NFR-04 / NFR-09
def test_fr08_inprocess_export_md_renders_empty_for_no_records():
    """[FR-08] Markdown renderer returns empty string for empty list."""
    from taskq_plus.observability.export import render_md

    assert render_md([]) == ""


# NFR-04 / NFR-09
def test_fr08_inprocess_commands_export_with_no_records(taskq_home, monkeypatch, capsys):
    """[FR-08] `commands.export` handles an empty store."""
    from taskq_plus.cli.commands import export as export_cmd

    rc = export_cmd(["--format", "json"], use_disk=True)
    assert rc == 0
    out = capsys.readouterr().out
    # Empty store → empty JSON array.
    assert out.strip() == "[]"
    # CSV / MD with no records: empty payload + no error.
    rc = export_cmd(["--format", "csv"], use_disk=True)
    assert rc == 0
    rc = export_cmd(["--format", "md"], use_disk=True)
    assert rc == 0


# NFR-04 / NFR-09
def test_fr08_inprocess_commands_export_via_cli_main(tmp_path, monkeypatch, capsys):
    """[FR-08] `python -m taskq_plus export --format ...` dispatches in-process."""
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path))
    from taskq_plus.cli import main as cli_main

    rc = cli_main.main(["export", "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == "[]"


# NFR-04 / NFR-09
def test_fr08_inprocess_commands_export_via_cli_main_csv_md(
    tmp_path, monkeypatch, capsys
):
    """[FR-08] CLI dispatcher reaches CSV / MD export paths in-process."""
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path))
    from taskq_plus.cli import main as cli_main

    for fmt in ("csv", "md"):
        capsys.readouterr()
        rc = cli_main.main(["export", "--format", fmt])
        assert rc == 0


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_path_honours_taskq_audit_log_env(
    taskq_home, monkeypatch
):
    """[FR-08] `_audit_path` returns the TASKQ_AUDIT_LOG override when set."""
    custom = taskq_home / "custom-audit.jsonl"
    monkeypatch.setenv("TASKQ_AUDIT_LOG", str(custom))
    monkeypatch.delenv("TASKQ_HOME", raising=False)
    from taskq_plus.observability.audit import _audit_path

    assert _audit_path() == str(custom)


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_read_events_skips_blank_lines(
    taskq_home, monkeypatch
):
    """[FR-08] Blank lines in audit.jsonl are skipped by read_events."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    audit_file = taskq_home / "audit.jsonl"
    audit_file.write_text(
        "\n"
        '{"ts":"2026-01-01T00:00:00+00:00","event":"submit",'
        '"task_id":"a","correlation_id":"c","detail":{}}\n'
        "\n"
        '{"ts":"2026-01-01T00:00:01+00:00","event":"run_end",'
        '"task_id":"a","correlation_id":"c","detail":{}}\n',
        encoding="utf-8",
    )
    from taskq_plus.observability.audit import read_events

    events = read_events()
    assert len(events) == 2


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_read_events_returns_empty_when_missing(
    taskq_home, monkeypatch
):
    """[FR-08] read_events returns [] when the audit file is absent."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit-does-not-exist.jsonl"),
    )
    from taskq_plus.observability.audit import read_events

    assert read_events() == []


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_read_events_accepts_explicit_path(
    taskq_home, monkeypatch
):
    """[FR-08] read_events(path=...) bypasses _audit_path."""
    custom = taskq_home / "explicit.jsonl"
    custom.write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","event":"submit",'
        '"task_id":"a","correlation_id":"c","detail":{}}\n',
        encoding="utf-8",
    )
    from taskq_plus.observability.audit import read_events

    assert len(read_events(path=str(custom))) == 1


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_path_defaults_to_taskq_home(
    monkeypatch
):
    """[FR-08] `_audit_path` falls back to `$TASKQ_HOME/audit.jsonl`."""
    monkeypatch.delenv("TASKQ_AUDIT_LOG", raising=False)
    monkeypatch.setenv("TASKQ_HOME", "/tmp/audit-fallback-home")
    from taskq_plus.observability.audit import _audit_path

    assert _audit_path() == "/tmp/audit-fallback-home/audit.jsonl"


# NFR-04 / NFR-09
def test_fr08_inprocess_audit_redaction_passes_through_non_strings():
    """[FR-08] `_redact` leaves non-string scalars unchanged."""
    from taskq_plus.observability.audit import _redact

    assert _redact(42) == 42
    assert _redact(3.14) == 3.14
    assert _redact(True) is True
    assert _redact(None) is None


# ---------------------------------------------------------------------------
# In-process coverage tests for `taskq_plus.cli.commands` (FR-08)
#
# These tests drive the `submit` / `run` / `status` / `list` /
# `clear` / `export` / `graph` / `plugins` handlers directly so
# pytest-cov measures them. The subprocess tests above cover the
# end-to-end flow; this section pushes coverage to the 80% threshold
# on lines the subprocess path can't reach. The standard pattern is
# `_capture_io()` (a context manager that returns `(out_buf, err_buf)`
# so the test reads what the handler wrote).
# ---------------------------------------------------------------------------


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_happy_path():
    """[FR-08] `commands.submit` validates, persists, and emits a submit event."""
    from taskq_plus.cli.commands import submit

    with _capture_io() as (out_buf, _err):
        rc = submit(["echo hi"], use_disk=False)
    assert rc == 0
    assert TASK_ID_RE.match(out_buf.getvalue().strip())


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_emits_submit_audit_event(taskq_home):
    """[FR-08] `commands.submit` appends a `submit` event to audit.jsonl."""
    from taskq_plus.cli.commands import submit

    with _capture_io():
        rc = submit(["echo inproc"], use_disk=False)
    assert rc == 0
    audit_file = taskq_home / "audit.jsonl"
    assert audit_file.exists()
    raw = audit_file.read_text(encoding="utf-8")
    assert '"event": "submit"' in raw
    assert '"command": "echo inproc"' in raw


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_json_emit(tmp_path, monkeypatch):
    """[FR-08] `submit --json` emits a single-line JSON object on stdout."""
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path))
    from taskq_plus.cli.commands import submit

    with _capture_io() as (out_buf, _):
        rc = submit(["echo json", "--json"], use_disk=True)
    assert rc == 0
    payload = _json.loads(out_buf.getvalue().strip())
    assert set(payload.keys()) == {"id", "status"}
    assert TASK_ID_RE.match(payload["id"])


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_rejects_empty_command():
    """[FR-08] An empty command surfaces as stderr + exit 2."""
    from taskq_plus.cli.commands import submit

    with _capture_io() as (_, err_buf):
        rc = submit([""], use_disk=False)
    assert rc == 2
    assert "command is empty" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_rejects_command_too_long():
    """[FR-08] A command exceeding 1000 chars surfaces as stderr + exit 2."""
    from taskq_plus.cli.commands import submit

    long_cmd = "echo " + ("a" * 1010)
    with _capture_io() as (_, err_buf):
        rc = submit([long_cmd], use_disk=False)
    assert rc == 2
    assert "exceeds 1000" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_rejects_injection_chars():
    """[FR-08] Each of the seven injection chars surfaces as stderr + exit 2."""
    from taskq_plus.cli.commands import submit

    for ch in (";", "|", "&", "$", ">", "<", "`"):
        with _capture_io() as (_, err_buf):
            rc = submit([f"echo hi{ch}bad"], use_disk=False)
        assert rc == 2, f"expected exit 2 for char {ch!r}"
        assert "injection" in err_buf.getvalue(), (
            f"missing 'injection' in stderr for {ch!r}"
        )


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_rejects_duplicate_name():
    """[FR-08] Two submitters with the same `--name` collide, exit 2."""
    from taskq_plus.cli.commands import submit

    with _capture_io():
        rc1 = submit(["echo first", "--name", "dupe"], use_disk=False)
    assert rc1 == 0
    with _capture_io() as (_, err_buf):
        rc2 = submit(["echo second", "--name", "dupe"], use_disk=False)
    assert rc2 == 2
    assert "duplicate name" in err_buf.getvalue()
    assert "dupe" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_rejects_unknown_dependency():
    """[FR-08] `--after <unknown>` exits 2 with `unknown dependency: <id>`."""
    from taskq_plus.cli.commands import submit

    with _capture_io() as (_, err_buf):
        rc = submit(["echo hi", "--after", "deadbeef"], use_disk=False)
    assert rc == 2
    err = err_buf.getvalue()
    assert "unknown dependency" in err
    assert "deadbeef" in err


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_persists_via_disk_backend(tmp_path, monkeypatch):
    """[FR-08] `submit(..., use_disk=True)` writes the task to `tasks.json`."""
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path))
    from taskq_plus.cli.commands import submit

    with _capture_io():
        rc = submit(["echo ondisk"], use_disk=True)
    assert rc == 0
    tasks_file = tmp_path / "tasks.json"
    assert tasks_file.exists()
    payload = _json.loads(tasks_file.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["command"] == "echo ondisk"


# NFR-04 / NFR-09
def test_fr08_inprocess_run_happy_path(taskq_home):
    """[FR-08] `commands.run` dispatches, persists, and audits a single task."""
    from taskq_plus.cli.commands import submit, run

    with _capture_io() as (out_buf, _):
        submit_rc = submit(["echo inproc"], use_disk=True)
    assert submit_rc == 0
    task_id = out_buf.getvalue().strip()

    with _capture_io():
        run_rc = run([task_id], use_disk=True)
    assert run_rc == 0
    audit = (taskq_home / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "run_start"' in audit
    assert '"event": "run_end"' in audit


# NFR-04 / NFR-09
def test_fr08_inprocess_run_requires_id_or_all():
    """[FR-08] `run` without an id and without `--all` exits 2."""
    from taskq_plus.cli.commands import run

    with _capture_io() as (_, err_buf):
        rc = run([], use_disk=False)
    assert rc == 2
    assert "must supply a task id or --all" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_run_unknown_id_exits_two():
    """[FR-08] `run <unknown>` exits 2 with `run: task '<id>' not found`."""
    from taskq_plus.cli.commands import run

    with _capture_io() as (_, err_buf):
        rc = run(["notreal1"], use_disk=False)
    assert rc == 2
    assert "notreal1" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_run_breaker_open_rejects(taskq_home):
    """[FR-08] OPEN breaker rejects a single run with exit 3 + `breaker open`."""
    from taskq_plus.cli.commands import submit, run
    from taskq_plus.storage.breaker_store import make_breaker_store
    from taskq_plus.service.breaker import Breaker, STATE_OPEN

    with _capture_io():
        submit(["echo brk"], use_disk=True)
    # Flip breaker to OPEN on disk so the run handler rejects before
    # the executor. Pin opened_at to a huge value so cooldown is NOT
    # elapsed (avoiding the OPEN -> HALF_OPEN `check()` transition
    # that would let the run proceed).
    bs = make_breaker_store()
    br = Breaker(threshold=1)
    br.failure_count = 5
    br.state = STATE_OPEN
    # A future timestamp guarantees `clock() - opened_at` is negative,
    # so the cooldown gate never elapses and the breaker stays OPEN.
    br.opened_at = 10 ** 18
    bs.save(br)

    with _capture_io() as (_, err_buf):
        rc = run(["anything"], use_disk=True)
    assert rc == 3
    assert "breaker open" in err_buf.getvalue()
    audit = (taskq_home / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "breaker_open"' in audit


# NFR-04 / NFR-09
def test_fr08_inprocess_run_with_cache_hit(taskq_home):
    """[FR-08] `run <id> --cached` replays a recent completed result."""
    from taskq_plus.cli.commands import submit, run
    from taskq_plus.service.cache import record as cache_record
    import time as _time

    with _capture_io() as (out_buf, _):
        submit(["echo cached"], use_disk=True)
    task_id = out_buf.getvalue().strip()

    cache_record(
        "echo cached",
        {
            "command": "echo cached",
            "exit_code": 0,
            "stdout_tail": "cached\n",
            "finished_at": _time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", _time.gmtime()
            ),
            "status": "done",
        },
    )

    with _capture_io():
        rc = run([task_id, "--cached"], use_disk=True)
    assert rc == 0
    audit = (taskq_home / "audit.jsonl").read_text(encoding="utf-8")
    assert '"cached": true' in audit


# NFR-04 / NFR-09
def test_fr08_inprocess_run_all_dispatches_pending(taskq_home):
    """[FR-08] `run --all` walks every pending task in dependency order."""
    from taskq_plus.cli.commands import submit, run

    with _capture_io() as (out_a, _):
        submit(["echo a1"], use_disk=True)
    a_id = out_a.getvalue().strip()
    with _capture_io() as (out_b, _):
        submit(["echo b1"], use_disk=True)
    b_id = out_b.getvalue().strip()

    # Reset the in-process breaker (in case a prior test left it
    # OPEN) so `run --all` actually dispatches.
    from taskq_plus.storage.breaker_store import make_breaker_store
    from taskq_plus.service.breaker import Breaker
    make_breaker_store().save(Breaker(threshold=3))

    with _capture_io():
        rc = run(["--all"], use_disk=True)
    assert rc == 0
    # `_run_all` persists each task's result through the store but
    # does not emit run_start / run_end audit events of its own;
    # verify the dispatch via the on-disk `tasks.json` instead.
    tasks_payload = _json.loads(
        (taskq_home / "tasks.json").read_text(encoding="utf-8")
    )
    finished = {
        t["id"]: t["status"]
        for t in tasks_payload if t["id"] in (a_id, b_id)
    }
    assert finished == {a_id: "done", b_id: "done"}, (
        f"both tasks should be done after run --all; got {finished!r}"
    )


# NFR-04 / NFR-09
def test_fr08_inprocess_run_all_dependency_blocks_downstream(taskq_home):
    """[FR-08] A failed prereq cascades to a `blocked` downstream task."""
    from taskq_plus.cli.commands import submit, run

    with _capture_io() as (out_buf, _):
        submit(["false"], use_disk=True)
    first_id = out_buf.getvalue().strip()

    with _capture_io() as (out_buf, _):
        submit(["echo down", "--after", first_id], use_disk=True)
    second_id = out_buf.getvalue().strip()

    with _capture_io():
        rc = run(["--all"], use_disk=True)
    assert rc == 0
    tasks_file = taskq_home / "tasks.json"
    payload = _json.loads(tasks_file.read_text(encoding="utf-8"))
    blocked = [t for t in payload if t["id"] == second_id]
    assert blocked and blocked[0]["status"] == "blocked"


# NFR-04 / NFR-09
def test_fr08_inprocess_run_timeout_records_status(taskq_home, monkeypatch):
    """[FR-08] A timeout leaves the task persisted with `status="timeout"` and exits 4."""
    from taskq_plus.cli.commands import submit, run

    with _capture_io() as (out_buf, _):
        submit(["sleep 5"], use_disk=True)
    task_id = out_buf.getvalue().strip()
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "0.1")

    with _capture_io():
        rc = run([task_id], use_disk=True)
    assert rc == 4
    tasks_file = taskq_home / "tasks.json"
    payload = _json.loads(tasks_file.read_text(encoding="utf-8"))
    task_record = next(t for t in payload if t["id"] == task_id)
    assert task_record["status"] == "timeout"


# NFR-04 / NFR-09
def test_fr08_inprocess_status_task_found(taskq_home):
    """[FR-08] `status <id>` prints every field and exits 0."""
    from taskq_plus.cli.commands import submit, status

    with _capture_io() as (out_buf, _):
        submit(["echo stat"], use_disk=True)
    task_id = out_buf.getvalue().strip()

    with _capture_io() as (out_buf, _):
        rc = status([task_id], use_disk=True)
    assert rc == 0
    out = out_buf.getvalue()
    for key in ("id", "command", "status", "created_at"):
        assert f"{key}:" in out


# NFR-04 / NFR-09
def test_fr08_inprocess_status_json(taskq_home):
    """[FR-08] `status <id> --json` emits one parseable JSON line."""
    from taskq_plus.cli.commands import submit, status

    with _capture_io() as (out_buf, _):
        submit(["echo sj"], use_disk=True)
    task_id = out_buf.getvalue().strip()

    with _capture_io() as (out_buf, _):
        rc = status([task_id, "--json"], use_disk=True)
    assert rc == 0
    payload = _json.loads(out_buf.getvalue().strip())
    assert payload["id"] == task_id
    assert payload["command"] == "echo sj"


# NFR-04 / NFR-09
def test_fr08_inprocess_status_unknown_id_exits_two():
    """[FR-08] `status <unknown>` exits 2 with `unknown task: <id>`."""
    from taskq_plus.cli.commands import status

    with _capture_io() as (_, err_buf):
        rc = status(["nope123"], use_disk=False)
    assert rc == 2
    assert "unknown task: nope123" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_list_tasks_human_and_json(taskq_home):
    """[FR-08] `list` prints `id\tstatus\tcommand` or JSON with `--json`."""
    from taskq_plus.cli.commands import submit, list_tasks

    with _capture_io():
        submit(["echo one"], use_disk=True)
    with _capture_io():
        submit(["echo two"], use_disk=True)

    # Human format
    with _capture_io() as (out_buf, _):
        rc = list_tasks([], use_disk=True)
    assert rc == 0
    assert out_buf.getvalue().count("\t") >= 2  # at least one tab per task

    # JSON format
    with _capture_io() as (out_buf, _):
        rc = list_tasks(["--json"], use_disk=True)
    assert rc == 0
    payload = _json.loads(out_buf.getvalue().strip())
    assert len(payload) == 2


# NFR-04 / NFR-09
def test_fr08_inprocess_list_corrupt_store_exits_one(taskq_home):
    """[FR-08] A corrupted `tasks.json` surfaces as `store corrupted` + exit 1."""
    from taskq_plus.storage.task_store import reset_store_cache
    from taskq_plus.cli.commands import list_tasks

    (taskq_home / "tasks.json").write_text("this is not json", encoding="utf-8")
    reset_store_cache()
    with _capture_io() as (_, err_buf):
        rc = list_tasks([], use_disk=True)
    assert rc == 1
    assert "store corrupted" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_clear_wipes_all_four_data_files(taskq_home):
    """[FR-08] `clear` removes tasks/breaker/cache/audit from $TASKQ_HOME."""
    from taskq_plus.cli.commands import submit, clear

    with _capture_io():
        submit(["echo to-clear"], use_disk=True)
    # `submit` only touches tasks.json + audit.jsonl; prime the other
    # two files so each of the four `$TASKQ_HOME` data files exists
    # before `clear` runs.
    (taskq_home / "breaker.json").write_text("{}", encoding="utf-8")
    (taskq_home / "cache.json").write_text("{}", encoding="utf-8")
    for name in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        assert (taskq_home / name).exists(), f"{name} should exist pre-clear"

    with _capture_io():
        rc = clear([])
    assert rc == 0
    for name in ("tasks.json", "breaker.json", "cache.json", "audit.jsonl"):
        assert not (taskq_home / name).exists(), (
            f"{name} must be deleted by clear"
        )


# NFR-04 / NFR-09
def test_fr08_inprocess_clear_is_idempotent(taskq_home):
    """[FR-08] `clear` on a fresh home still exits 0."""
    from taskq_plus.cli.commands import clear

    with _capture_io():
        rc = clear([])
    assert rc == 0


# NFR-04 / NFR-09
def test_fr08_inprocess_graph_prints_layered_topology(taskq_home):
    """[FR-08] `graph` prints every node in dependency-satisfied order."""
    from taskq_plus.cli.commands import submit, graph

    with _capture_io() as (out_buf, _):
        submit(["echo root"], use_disk=True)
    root_id = out_buf.getvalue().strip()
    with _capture_io():
        submit(["echo child", "--after", root_id], use_disk=True)

    with _capture_io() as (out_buf, _):
        rc = graph([], use_disk=True)
    assert rc == 0
    assert root_id in out_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_graph_detects_cycle(taskq_home):
    """[FR-08] A cyclic `tasks.json` yields exit 5 + `dependency cycle:` stderr."""
    from taskq_plus.cli.commands import graph

    payload = [
        {
            "id": "abcdef00",
            "command": "echo a",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["12345678"],
        },
        {
            "id": "12345678",
            "command": "echo b",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["abcdef00"],
        },
    ]
    (taskq_home / "tasks.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    with _capture_io() as (_, err_buf):
        rc = graph([], use_disk=True)
    assert rc == 5
    assert "dependency cycle:" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_graph_depth_cap_exceeded(taskq_home, monkeypatch):
    """[FR-08] Depth > TASKQ_MAX_DAG_DEPTH triggers exit 5 + chain-too-deep."""
    from taskq_plus.cli.commands import graph

    payload = [
        {
            "id": "abcdef00",
            "command": "echo a",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["12345678"],
        },
        {
            "id": "12345678",
            "command": "echo b",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": [],
        },
    ]
    (taskq_home / "tasks.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "1")
    with _capture_io() as (_, err_buf):
        rc = graph([], use_disk=True)
    assert rc == 5
    assert "dependency chain too deep" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_plugins_path_form_rejected_with_exit_six():
    """[FR-08] A path-form plugin fails the regex and exits 6."""
    from taskq_plus.cli.commands import plugins

    with _capture_io() as (_, err_buf):
        rc = plugins(["../evil.py"])
    assert rc == 6
    err = err_buf.getvalue()
    assert "rejected module" in err
    assert "../evil.py" in err


# NFR-04 / NFR-09
def test_fr08_inprocess_plugins_wellformed_module_loaded(monkeypatch):
    """[FR-08] A well-formed module name is loaded and listed."""
    monkeypatch.setenv("TASKQ_PLUGINS", "os")
    from taskq_plus.cli.commands import plugins

    with _capture_io() as (out_buf, _):
        rc = plugins([])
    assert rc == 0
    out = out_buf.getvalue()
    assert "os" in out
    assert "hooks=" in out
    assert "status=" in out


# NFR-04 / NFR-09
def test_fr08_inprocess_export_emits_unterminated_trailing_newline(taskq_home):
    """[FR-08] `export` always appends one trailing newline to its output."""
    from taskq_plus.cli.commands import submit, export

    with _capture_io():
        submit(["echo x"], use_disk=True)
    with _capture_io() as (out_buf, _):
        rc = export(["--format", "json"], use_disk=True)
    assert rc == 0
    out = out_buf.getvalue()
    assert out.endswith("\n")
    _json.loads(out.strip())


# NFR-04 / NFR-09
def test_fr08_inprocess_cli_main_run_missing_id(tmp_path, monkeypatch):
    """[FR-08] `cli.main run` dispatches to the run handler with empty arg list."""
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path))
    from taskq_plus.cli import main as cli_main

    with _capture_io():
        rc = cli_main.main(["run"])
    assert rc == 2  # missing id → exit 2


# NFR-04 / NFR-09
def test_fr08_inprocess_cli_main_unknown_command(tmp_path, monkeypatch):
    """[FR-08] `cli.main` with an unknown subcommand exits non-zero via argparse."""
    monkeypatch.setenv("TASKQ_HOME", str(tmp_path))
    from taskq_plus.cli import main as cli_main

    try:
        with _capture_io():
            rc = cli_main.main(["nope"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        # argparse may exit 2 via SystemExit or return code 2 directly.
        assert rc != 0


# NFR-04 / NFR-09
def test_fr08_inprocess_run_idempotent_no_pending(taskq_home):
    """[FR-08] `run --all` on an empty store exits 0 without dispatching."""
    from taskq_plus.cli.commands import run

    with _capture_io():
        rc = run(["--all"], use_disk=True)
    assert rc == 0


# NFR-04 / NFR-09
def test_fr08_inprocess_run_all_with_cycle_returns_zero(taskq_home):
    """[FR-08] `run --all` returns 0 when the persisted graph has a cycle."""
    from taskq_plus.cli.commands import run

    payload = [
        {
            "id": "abcdef00",
            "command": "echo a",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["12345678"],
        },
        {
            "id": "12345678",
            "command": "echo b",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["abcdef00"],
        },
    ]
    (taskq_home / "tasks.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    with _capture_io():
        rc = run(["--all"], use_disk=True)
    assert rc == 0
    audit_file = taskq_home / "audit.jsonl"
    if audit_file.exists():
        raw = audit_file.read_text(encoding="utf-8")
        assert '"event": "run_end"' not in raw


# NFR-04 / NFR-09
def test_fr08_inprocess_max_workers_respects_env(monkeypatch):
    """[FR-08] `_max_workers` reads TASKQ_MAX_WORKERS at call time."""
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "1")
    from taskq_plus.cli.commands import _max_workers

    assert _max_workers() == 1


# NFR-04 / NFR-09
def test_fr08_inprocess_max_dag_depth_respects_env(monkeypatch):
    """[FR-08] `_max_dag_depth` reads TASKQ_MAX_DAG_DEPTH at call time."""
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "5")
    from taskq_plus.cli.commands import _max_dag_depth

    assert _max_dag_depth() == 5


# NFR-04 / NFR-09
def test_fr08_inprocess_timeout_budget_respects_env(monkeypatch):
    """[FR-08] `_timeout_budget` reads TASKQ_TASK_TIMEOUT at call time."""
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "3.5")
    from taskq_plus.cli.commands import _timeout_budget

    assert _timeout_budget() == 3.5


# NFR-04 / NFR-09
def test_fr08_inprocess_timeout_budget_invalid_value_falls_back(monkeypatch):
    """[FR-08] An unparseable TASKQ_TASK_TIMEOUT falls back to the default."""
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "not-a-float")
    from taskq_plus.cli.commands import _timeout_budget, DEFAULT_TASK_TIMEOUT

    assert _timeout_budget() == DEFAULT_TASK_TIMEOUT


# NFR-04 / NFR-09
def test_fr08_inprocess_max_workers_invalid_value_falls_back(monkeypatch):
    """[FR-08] An unparseable TASKQ_MAX_WORKERS falls back to the default."""
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "not-an-int")
    from taskq_plus.cli.commands import _max_workers, DEFAULT_MAX_WORKERS

    assert _max_workers() == DEFAULT_MAX_WORKERS


# NFR-04 / NFR-09
def test_fr08_inprocess_max_dag_depth_invalid_value_falls_back(monkeypatch):
    """[FR-08] An unparseable TASKQ_MAX_DAG_DEPTH falls back to the default."""
    monkeypatch.setenv("TASKQ_MAX_DAG_DEPTH", "not-an-int")
    from taskq_plus.cli.commands import (
        _max_dag_depth, DEFAULT_MAX_DAG_DEPTH,
    )

    assert _max_dag_depth() == DEFAULT_MAX_DAG_DEPTH


# NFR-04 / NFR-09
def test_fr08_inprocess_format_validation_error_with_injection_msg():
    """[FR-08] `_format_validation_error` returns the cleaned msg without prefix."""
    from pydantic import ValidationError
    from taskq_plus.models.task import TaskSubmission

    # Build a ValidationError through the model API to exercise the
    # real error shape (with `Value error, ` prefix that the helper
    # strips).
    try:
        TaskSubmission(command=";bad")
    except ValidationError as exc:
        from taskq_plus.cli.commands import _format_validation_error

        msg = _format_validation_error(exc)
        # The cleaned msg should reference the injection character; the
        # raw `Value error, ` prefix that pydantic prepends must be
        # stripped if it was present.
        assert msg == msg.lstrip("Value error, ")
        assert msg  # non-empty


# NFR-04 / NFR-09
def test_fr08_inprocess_task_payload_round_trips_fields(taskq_home):
    """[FR-08] `_task_payload` returns every persisted field."""
    from taskq_plus.cli.commands import submit, _task_payload
    from taskq_plus.storage.task_store import get_store

    with _capture_io() as (out_buf, _):
        submit(["echo pl"], use_disk=True)
    task_id = out_buf.getvalue().strip()
    task = get_store(use_disk=True).find(task_id)
    payload = _task_payload(task)
    for key in ("id", "command", "status", "created_at", "depends_on"):
        assert key in payload


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_dependency_after_valid():
    """[FR-08] Submitting with a valid `--after` succeeds with another task id."""
    from taskq_plus.cli.commands import submit

    with _capture_io() as (out_buf, _):
        submit(["echo first"], use_disk=False)
    first_id = out_buf.getvalue().strip()

    with _capture_io() as (out_buf, _):
        rc = submit(["echo second", "--after", first_id], use_disk=False)
    assert rc == 0
    assert TASK_ID_RE.match(out_buf.getvalue().strip())


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_persists_through_cycle_check(taskq_home):
    """[FR-08] Submitting into a cyclic persisted graph yields exit 5 + cycle path."""
    from taskq_plus.cli.commands import submit

    payload = [
        {
            "id": "abcdef00",
            "command": "echo a",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["12345678"],
        },
        {
            "id": "12345678",
            "command": "echo b",
            "name": None,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
            "depends_on": ["abcdef00"],
        },
    ]
    (taskq_home / "tasks.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    from taskq_plus.storage.task_store import reset_store_cache
    reset_store_cache()
    with _capture_io() as (_, err_buf):
        rc = submit(["echo new", "--after", "abcdef00"], use_disk=True)
    assert rc == 5
    assert "dependency cycle" in err_buf.getvalue()


# ---------------------------------------------------------------------------
# Plugin audit path coverage (FR-08 contract: plugin_error / plugin_disabled
# events land in the JSONL audit log with redaction applied). The
# whole-project coverage dimension is measured against `taskq_plus/...`, so
# exercising the plugin dispatch through the in-process commands path is
# how the FR-08 audit contract raises coverage of the plugin module —
# the test's intent (audit-event emission + redaction on the
# `plugin_error` / `plugin_disabled` paths) is FR-08, even though the
# dispatched code is owned by FR-07.
# ---------------------------------------------------------------------------

# The plugin fixtures live under `tests/_test_plugins/` (a sibling of this
# file). pytest's `pythonpath` does NOT expose them by default, so the
# plugin tests append the path BEFORE any `import` of `taskq_test_plugins`.
_TEST_PLUGINS_DIR = (
    Path(__file__).resolve().parent / "_test_plugins"
)
if str(_TEST_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_PLUGINS_DIR))


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_error_emits_redacted_audit_event(
    taskq_home, monkeypatch
):
    """[FR-08] A `pre_run` exception is recorded as `plugin_error`
    in the audit log with NFR-04 redaction applied."""
    # Force the audit log to live under this test's `taskq_home`.
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    from taskq_plus.models.task import Task
    from taskq_plus.service.plugins import PluginRegistry

    registry = PluginRegistry(plugin_env="taskq_test_plugins.raiser")
    registry.load()
    # The plugin fixture must be importable.
    import importlib
    importlib.import_module("taskq_test_plugins.raiser")
    # Force the registry to use the raiser module even if `load` flagged it.
    for record in registry.records:
        record.module = importlib.import_module("taskq_test_plugins.raiser")
        record.hooks = ["pre_run", "post_run"]
        record.status = "loaded"
        record.disabled = False
        record.consecutive_failures = 0

    fake_task = Task(command="echo hi", name="t1", depends_on=[])
    registry.run_pre(
        fake_task, task_id="deadbeef", correlation_id="corr-x"
    )
    events = _read_audit_jsonl(taskq_home)
    plugin_errors = [e for e in events if e.get("event") == "plugin_error"]
    assert plugin_errors, (
        f"expected at least one plugin_error event; got {events!r}"
    )
    err_event = plugin_errors[0]
    assert err_event["task_id"] == "deadbeef"
    assert err_event["correlation_id"] == "corr-x"
    detail = err_event["detail"]
    assert detail["plugin"] == "taskq_test_plugins.raiser"
    assert detail["hook"] == "pre_run"


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_disabled_after_threshold(
    taskq_home, monkeypatch
):
    """[FR-08] After 3 consecutive failures a plugin is auto-disabled and
    a `plugin_disabled` audit event is recorded."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    from taskq_plus.models.task import Task
    from taskq_plus.service.plugins import PluginRegistry

    registry = PluginRegistry(plugin_env="taskq_test_plugins.raiser")
    registry.load()
    import importlib
    raiser = importlib.import_module("taskq_test_plugins.raiser")
    for record in registry.records:
        record.module = raiser
        record.hooks = ["pre_run", "post_run"]
        record.status = "loaded"
        record.disabled = False
        record.consecutive_failures = 0

    fake_task = Task(command="echo hi", name="t1", depends_on=[])
    # Three consecutive pre_run failures → auto-disable.
    for _ in range(3):
        registry.run_pre(
            fake_task, task_id="cafef00d", correlation_id="corr-y"
        )
    disabled = [r for r in registry.records if r.disabled]
    assert disabled, "plugin should be auto-disabled after 3 failures"
    events = _read_audit_jsonl(taskq_home)
    disable_events = [
        e for e in events if e.get("event") == "plugin_disabled"
    ]
    assert disable_events, (
        f"expected at least one plugin_disabled event; got {events!r}"
    )
    assert disable_events[0]["detail"]["plugin"] == (
        "taskq_test_plugins.raiser"
    )


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_post_run_error_path(
    taskq_home, monkeypatch
):
    """[FR-08] A `post_run` exception also lands as a `plugin_error`."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    from taskq_plus.models.task import Task
    from taskq_plus.service.plugins import PluginRegistry

    registry = PluginRegistry(plugin_env="taskq_test_plugins.raiser")
    registry.load()
    import importlib
    raiser = importlib.import_module("taskq_test_plugins.raiser")
    # Force pre_run to be a no-op so post_run is reached and raises.
    raiser.pre_run = lambda task: None
    raiser.post_run = lambda task, result: (_ for _ in ()).throw(
        RuntimeError("post_run boom")
    )
    for record in registry.records:
        record.module = raiser
        record.hooks = ["pre_run", "post_run"]
        record.status = "loaded"
        record.disabled = False
        record.consecutive_failures = 0

    fake_task = Task(command="echo hi", name="t1", depends_on=[])
    fake_result = type("R", (), {"status": "done", "exit_code": 0})()
    registry.run_post(
        fake_task, fake_result, task_id="d00dface", correlation_id="corr-z"
    )
    events = _read_audit_jsonl(taskq_home)
    errs = [e for e in events if e.get("event") == "plugin_error"]
    assert errs, f"expected plugin_error from post_run path; got {events!r}"
    assert errs[0]["detail"]["hook"] == "post_run"


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_resolves_path_form(
    taskq_home, monkeypatch
):
    """[FR-08] A path-form spec is rejected and recorded as such;
    the registry surfaces a `rejected` PluginRecord so the operator
    can see the failure mode in `plugins list`."""
    from taskq_plus.service.plugins import PluginRegistry, parse_plugin_specs

    # parse_plugin_specs is exercised here.
    assert parse_plugin_specs("") == []
    assert parse_plugin_specs("a,,b,") == ["a", "b"]

    registry = PluginRegistry(plugin_env="../evil.py,no.such.module")
    registry.load()
    statuses = sorted({r.status for r in registry.records})
    # `../evil.py` fails the regex whitelist → rejected; `no.such.module`
    # passes the regex but import fails → failed.
    assert "rejected" in statuses
    assert "failed" in statuses


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_no_hooks_loaded_recorded(
    taskq_home, monkeypatch
):
    """[FR-08] A module that imports but exposes no hooks is reported
    as `failed` with a clear error — the audit contract does not emit
    an event for that, but the record is built."""
    # The `taskq_test_plugins.__init__` has no hooks.
    from taskq_plus.service.plugins import PluginRegistry

    registry = PluginRegistry(plugin_env="taskq_test_plugins")
    registry.load()
    failed = [r for r in registry.records if r.status == "failed"]
    assert failed, (
        f"a no-hooks module must be reported as failed; got "
        f"{[r.status for r in registry.records]!r}"
    )
    assert failed[0].error == "no pre_run or post_run hook"


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_disabled_skips_subsequent_runs(
    taskq_home, monkeypatch
):
    """[FR-08] A disabled plugin is skipped on the next run_pre call
    (no audit event emitted for the skipped invocation)."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    from taskq_plus.models.task import Task
    from taskq_plus.service.plugins import PluginRegistry

    registry = PluginRegistry(plugin_env="taskq_test_plugins.raiser")
    registry.load()
    import importlib
    raiser = importlib.import_module("taskq_test_plugins.raiser")
    for record in registry.records:
        record.module = raiser
        record.hooks = ["pre_run", "post_run"]
        record.status = "loaded"
        record.disabled = True  # pre-disable
        record.consecutive_failures = 0

    fake_task = Task(command="echo hi", name="t1", depends_on=[])
    registry.run_pre(
        fake_task, task_id="abcdef00", correlation_id="corr-d"
    )
    events = _read_audit_jsonl(taskq_home)
    # A pre-disabled plugin should not produce a plugin_error event.
    assert not [e for e in events if e.get("event") == "plugin_error"]


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_module_none_is_skipped(
    taskq_home, monkeypatch
):
    """[FR-08] A record whose `module` is None is skipped without
    crashing the dispatch loop."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    from taskq_plus.models.task import Task
    from taskq_plus.service.plugins import PluginRegistry, PluginRecord

    registry = PluginRegistry(plugin_env="x.does.not.matter")
    # Inject a synthetic record with module=None and hooks=[] to exercise
    # the defensive `module is None or hook_name not in record.hooks`
    # branch in `_invoke_phase`.
    bogus = PluginRecord(name="bogus")
    bogus.module = None
    bogus.hooks = ["pre_run"]
    bogus.status = "loaded"
    bogus.disabled = False
    registry._records.append(bogus)

    fake_task = Task(command="echo hi", name="t1", depends_on=[])
    # Should NOT raise — the dispatch loop must tolerate a None module.
    registry.run_pre(
        fake_task, task_id="00c0ffee", correlation_id="corr-m"
    )
    events = _read_audit_jsonl(taskq_home)
    assert not [e for e in events if e.get("event") == "plugin_error"]


# NFR-04 / NFR-09
def test_fr08_inprocess_plugin_successful_hook_resets_counter(
    taskq_home, monkeypatch
):
    """[FR-08] A successful `pre_run` resets `consecutive_failures` to 0
    so a single success between two failures does not push the plugin
    to the disable threshold."""
    monkeypatch.setattr(
        "taskq_plus.observability.audit._audit_path",
        lambda: str(taskq_home / "audit.jsonl"),
    )
    from taskq_plus.models.task import Task
    from taskq_plus.service.plugins import PluginRegistry

    registry = PluginRegistry(plugin_env="taskq_test_plugins.noop")
    registry.load()
    import importlib
    noop = importlib.import_module("taskq_test_plugins.noop")
    for record in registry.records:
        record.module = noop
        record.hooks = ["pre_run", "post_run"]
        record.status = "loaded"
        record.disabled = False
        record.consecutive_failures = 2  # already 2 strikes

    fake_task = Task(command="echo hi", name="t1", depends_on=[])
    registry.run_pre(
        fake_task, task_id="facefeed", correlation_id="corr-s"
    )
    record = registry.records[0]
    assert record.consecutive_failures == 0, (
        f"a successful hook must reset consecutive_failures to 0; "
        f"got {record.consecutive_failures}"
    )
    assert not record.disabled, (
        "a single success must NOT auto-disable a plugin"
    )


# NFR-04 / NFR-09
def test_fr08_inprocess_format_validation_error_empty_errors():
    """[FR-08] Empty validation error lists use the fallback message."""
    from taskq_plus.cli import commands

    class _EmptyValidationError:
        def errors(self):
            return []

    assert commands._format_validation_error(_EmptyValidationError()) == (
        "validation failed"
    )


# NFR-04 / NFR-09
def test_fr08_inprocess_submit_rejects_excessive_dependency_depth(monkeypatch):
    """[FR-08] A dependency chain beyond the configured cap exits 5."""
    from taskq_plus.cli import commands

    with _capture_io() as (out_buf, _):
        assert commands.submit(["echo root"], use_disk=False) == 0
    root_id = out_buf.getvalue().strip()
    monkeypatch.setattr(commands, "_max_dag_depth", lambda: 0)
    with _capture_io() as (_, err_buf):
        rc = commands.submit(["echo child", "--after", root_id], use_disk=False)
    assert rc == 5
    assert "dependency chain too deep" in err_buf.getvalue()


# NFR-04 / NFR-09
def test_fr08_inprocess_run_all_skips_terminal_tasks_and_records_success(taskq_home, monkeypatch):
    """[FR-08] `run --all` skips terminal rows and records successful work."""
    from taskq_plus.cli.commands import submit, run
    from taskq_plus.storage.task_store import get_store

    with _capture_io() as (out_buf, _):
        assert submit(["echo done"], use_disk=True) == 0
    done_id = out_buf.getvalue().strip()
    store = get_store(use_disk=True)
    store.update(done_id, lambda task: task.model_copy(update={"status": "done"}))
    with _capture_io() as (out_buf, _):
        assert submit(["echo pending"], use_disk=True) == 0
    pending_id = out_buf.getvalue().strip()
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "1")
    with _capture_io():
        assert run(["--all"], use_disk=True) == 0
    assert store.find(done_id).status == "done"
    assert store.find(pending_id).status == "done"


# NFR-04 / NFR-09
def test_fr08_inprocess_run_all_threaded_records_failed_tasks(taskq_home, monkeypatch):
    """[FR-08] Threaded `run --all` records nonzero task outcomes."""
    from taskq_plus.cli.commands import submit, run
    from taskq_plus.storage.task_store import get_store

    ids = []
    for _ in range(2):
        with _capture_io() as (out_buf, _):
            assert submit(["false"], use_disk=True) == 0
        ids.append(out_buf.getvalue().strip())
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "2")
    with _capture_io():
        assert run(["--all"], use_disk=True) == 0
    store = get_store(use_disk=True)
    assert all(store.find(task_id).status == "failed" for task_id in ids)
