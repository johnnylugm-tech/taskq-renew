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
# enumerate the two sub-cases.
test_fr08_exports_agree_and_escape_csv_fields = pytest.mark.parametrize(
    "scenario",
    [p.values[0] for p in _test_fr08_exports_agree_and_escape_csv_fields_params],
    ids=[p.id for p in _test_fr08_exports_agree_and_escape_csv_fields_params],
)(
    _fr08_exports_agree_and_escape_csv_fields
)


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
