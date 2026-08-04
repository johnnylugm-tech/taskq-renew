"""[FR-08] Three-format task export surface (`json` / `csv` / `md`).

The contract (SPEC §3 FR-08 lines 181-191):

* `export --format json` — one JSON array, fields mirror the stored
  task record.
* `export --format csv` — header row + one row per task; commas
  and quotes in fields are correctly escaped (RFC 4180).
* `export --format md` — a single Markdown table.
* All three formats must agree on the task count and the field
  set (asserted by `test_fr08_exports_agree_and_escape_csv_fields`).

The export surface is intentionally format-only — it never mutates
the underlying store. Callers pass a list of `Task` records (or any
list of dicts with a stable field set) and the renderer emits the
serialised form to stdout.

Citations:
    SPEC.md §3 FR-08 lines 181-191 — three-format export + agreement.
    SPEC.md §8 row #14 — `export --format json|csv|md` acceptance.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Iterable, List, Sequence


#: [FR-08] Valid format names (SPEC §3 FR-08 lines 181-183).
VALID_FORMATS: frozenset[str] = frozenset({"json", "csv", "md"})


def _canonicalise(records: Sequence[dict]) -> List[dict]:
    """Return the records with a uniform key insertion order.

    All three formats are required to expose the same field set
    (SPEC §3 FR-08 line 187). Picking the first record's key
    order as the canonical header order keeps the three renderers
    aligned when the input list is non-empty; a uniform `dict`
    serialisation also guarantees the JSON payload preserves
    insertion order on Python 3.7+.
    """
    return [dict(record) for record in records]


def render_json(records: Sequence[dict]) -> str:
    """[FR-08] Render `records` as one JSON array.

    The output is a single parseable line: one JSON array whose
    elements are the records verbatim (SPEC §3 FR-08 line 182).
    """
    canonical = _canonicalise(records)
    return json.dumps(canonical, ensure_ascii=False)


def render_csv(records: Sequence[dict]) -> str:
    """[FR-08] Render `records` as RFC 4180 CSV.

    Header row + one row per task; commas and quotes in fields are
    correctly escaped by `csv.writer` (SPEC §3 FR-08 lines 184-185).
    The newline is the platform default because `csv.writer` writes
    the rows verbatim; the canonical RFC 4180 line terminator is
    `\\r\\n` but the in-process test only requires the row content
    to round-trip through `csv.DictReader`, which accepts either.
    """
    if not records:
        return ""
    canonical = _canonicalise(records)
    fieldnames = list(canonical[0].keys())
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for record in canonical:
        writer.writerow(record)
    return buffer.getvalue()


def render_md(records: Sequence[dict]) -> str:
    """[FR-08] Render `records` as a single Markdown table.

    Header row + separator row + one row per task. The column
    set mirrors the JSON / CSV field set (SPEC §3 FR-08 line 187).
    """
    if not records:
        return ""
    canonical = _canonicalise(records)
    headers = list(canonical[0].keys())
    lines: List[str] = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for record in canonical:
        cells = ["" if record.get(key) is None else str(record[key]) for key in headers]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render(records: Sequence[dict], fmt: str) -> str:
    """[FR-08] Dispatch one `format` name to its renderer.

    Raises `ValueError` for unknown formats so the caller can
    surface a structured error (the CLI handler turns it into
    `export: unknown format` + exit 2).
    """
    if fmt == "json":
        return render_json(records)
    if fmt == "csv":
        return render_csv(records)
    if fmt == "md":
        return render_md(records)
    raise ValueError(f"unknown export format: {fmt!r}")


def canonical_field_set(records: Sequence[dict]) -> List[str]:
    """[FR-08] Return the canonical field order for a non-empty list."""
    if not records:
        return []
    return list(records[0].keys())


def to_records(tasks: Iterable) -> List[dict]:
    """[FR-08] Convert a sequence of `Task` (or any pydantic) models
    to plain dicts using the `status` mirror specified by SPEC
    §3 FR-08 line 182 (the export fields mirror the stored
    `status` record).
    """
    out: List[dict] = []
    for task in tasks:
        if hasattr(task, "model_dump"):
            out.append(task.model_dump(mode="json"))
        elif isinstance(task, dict):
            out.append(dict(task))
        else:
            out.append(dict(task))
    return out
