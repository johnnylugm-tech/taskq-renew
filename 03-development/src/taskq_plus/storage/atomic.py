"""[NFR-03] `storage.atomic` — hub for disk-write durability primitives.

The three stores (`task_store`, `breaker_store`, `cache_store`) all persist
their JSON state with the same `tmp + os.replace` pattern, and
`observability/audit` keeps `audit.jsonl` durable with an `open(O_APPEND)
+ fsync` flush. Both primitives are extracted here once so the storage
hub file earns the intra-layer edges the SAB §2.1 hub-and-spoke rule
requires and the four sibling files do not re-implement NFR-03 in
parallel.

Citations:
    SPEC.md §4 NFR-03 — atomic write invariant.
    SPEC.md §6 line 335 — `tmp + os.replace` 寫入模式.
    SAD.md §2.3.1 — `taskq_plus.storage.atomic` hub.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union


def atomic_write_json(
    path: Union[str, Path],
    payload: Any,
    *,
    tmp_prefix: str = ".taskq-atomic.",
    mode: int = 0o644,
) -> None:
    """[NFR-03] Write `payload` to `path` via `tmp + os.replace`.

    `payload` must be JSON-serialisable (Pydantic `model_dump(mode="json")`
    is the typical source). The temp file is created in the same
    directory as the target so the final `os.replace` is an atomic
    rename on the same filesystem. A mid-write kill leaves the
    previous valid file in place.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # `mkstemp` is called inside a `try` so the file descriptor is
    # closed on any allocation failure (no fd leak), and the cleanup
    # path doesn't rely on a TOCTOU `exists` check before `unlink`.
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=tmp_prefix, suffix=".json.tmp", dir=target.parent
        )
    except BaseException:
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_append_jsonl(
    path: Union[str, Path],
    record: Union[str, Any],
    *,
    mode: int = 0o644,
    serialize: bool = False,
) -> None:
    """[NFR-03] Append one record to a JSONL file and `fsync` it.

    `record` is a pre-serialised line string by default — the
    audit pipeline composes its own line shape (correlation id, ts,
    kind, payload) upstream and only needs durability here. Pass
    `serialize=True` (and a JSON-serialisable object) to dump the
    record through `json.dumps` inside this call instead.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = record if isinstance(record, str) else json.dumps(
        record, ensure_ascii=False
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
