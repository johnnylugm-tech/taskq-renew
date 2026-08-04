"""[FR-07] Plugin loader and hook dispatcher.

The plugin hook contract (SPEC.md §3 FR-07 lines 151-159):

* A plugin is a Python module exposing `pre_run(task) -> None` and/or
  `post_run(task, result) -> None`.
* Loading source is the `TASKQ_PLUGINS` env var (comma-separated
  module names). Each name must match `^[A-Za-z_][A-Za-z0-9_.]*$`
  *before* any import is attempted; a path/URL form fails the
  regex and is rejected with exit 6.
* A plugin that raises from `pre_run` / `post_run` must NOT abort
  task execution — the exception is recorded as a `plugin_error`
  audit event and the runner continues.
* After 3 consecutive failures in one run, the plugin is auto-
  disabled for the remainder of that run and a `plugin_disabled`
  event is appended to the audit log.

Security (NFR-02):
    This module only calls `importlib.import_module` over a name
    that has already passed the regex whitelist. The `eval` / `exec`
    / `__import__` paths and any path- or URL-based loading are
    unreachable from this surface — the SAB layer rules forbid
    upward imports from `service`, so the only dynamic-import
    mechanism reachable from the runner is this one.

The audit append helper lives in this module (rather than the
yet-to-be-implemented `taskq_plus.observability.audit`) because
`service` may not import `observability` per the layer contract.
The append is restricted to the plugin events the FR-07 spec
explicitly defines (`plugin_error`, `plugin_disabled`) so it does
not pre-empt FR-08's audit-event API surface.

Citations:
    SPEC.md §3 FR-07 lines 151-160 — plugin contract.
    SPEC.md §3 FR-07 line 157 — module-name regex.
    SPEC.md §3 FR-07 line 159 — error isolation + auto-disable.
    SPEC.md §5.2 line 314 — `$TASKQ_HOME/audit.jsonl`.
    SPEC.md §4 NFR-02 — no `eval` / `exec` / `__import__` over
        user strings.
    SPEC.md §4 NFR-03 — atomic JSONL append + fsync.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from taskq_plus.config import taskq_home


#: [FR-07] Module-name whitelist regex. A path or URL form fails
#: this regex and is rejected BEFORE `importlib.import_module` is
#: invoked (SPEC §3 FR-07 line 157 — security rule that keeps
#: `TASKQ_PLUGINS` from becoming an arbitrary-code-execution entry
#: point).
PLUGIN_NAME_RE: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

#: [FR-07] Consecutive failures before a plugin is auto-disabled
#: (SPEC §3 FR-07 line 159 — 連續 3 次失敗).
DISABLE_THRESHOLD: int = 3

#: [FR-07] The recognised hook names, in the order `plugins list`
#: reports them (SPEC §3 FR-07 lines 158-160). A module exposing
#: neither is loaded but has no dispatchable surface.
HOOK_NAMES: Tuple[str, ...] = ("pre_run", "post_run")

#: [FR-07] Audit log filename (SPEC §5.2 line 314).
_AUDIT_LOG_FILENAME: str = "audit.jsonl"

#: [FR-07] Process-wide mutex guarding the audit append so
#: concurrent `run --all` plugin failures do not interleave JSONL
#: lines (NFR-03 atomicity).
_AUDIT_LOCK = threading.Lock()


def _audit_path():
    """[FR-07] Resolved path of the audit JSONL file."""
    return taskq_home() / _AUDIT_LOG_FILENAME


def parse_plugin_specs(env_value: str) -> List[str]:
    """[FR-07] Split a comma-separated allowlist into stripped specs.

    Blank segments are dropped so a trailing comma or an all-empty
    `TASKQ_PLUGINS` yields no specs rather than an empty-string spec
    that would fail the name regex and be reported as a rejection.

    This is the single parser for the allowlist: both the registry
    loader and the `plugins list` CLI handler call it, so the two
    surfaces cannot disagree about what counts as a declared spec.
    """
    return [spec.strip() for spec in env_value.split(",") if spec.strip()]


def append_audit_event(event: dict) -> None:
    """[FR-07] Append one JSONL event to `$TASKQ_HOME/audit.jsonl`.

    The write is guarded by a module-level lock so concurrent
    plugin failures from `run --all` cannot interleave lines. The
    file is opened with `O_APPEND` (positional writes) and
    `fsync`'d on every event (NFR-03 — append + fsync contract for
    `audit.jsonl`).

    Args:
        event: A JSON-serialisable dict. The `event` discriminator
            field is required by the audit-trail convention but is
            not asserted here — the FR-07 surface only emits
            `plugin_error` and `plugin_disabled`.
    """
    payload = json.dumps(event, default=str) + "\n"
    with _AUDIT_LOCK:
        fd = os.open(
            str(_audit_path()),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


@dataclass
class PluginRecord:
    """[FR-07] One allowlisted plugin entry.

    Attributes:
        name: The allowlisted module name (verbatim from
            `TASKQ_PLUGINS`).
        module: The imported module object (None when the import
            was rejected or failed).
        hooks: Names of recognised hooks (subset of
            `{"pre_run", "post_run"}`).
        status: One of `loaded`, `rejected`, `failed`.
        error: Short reason string when `status` is not `loaded`.
        consecutive_failures: Counter of consecutive failed hook
            invocations in the current run.
        disabled: Whether the plugin has been auto-disabled for the
            remainder of the current run.
    """

    name: str
    module: Optional[Any] = None
    hooks: List[str] = field(default_factory=list)
    status: str = "not_loaded"
    error: Optional[str] = None
    consecutive_failures: int = 0
    disabled: bool = False


class PluginRegistry:
    """[FR-07] Loaded plugin registry; dispatches hooks per task.

    The constructor reads `TASKQ_PLUGINS` (or an explicit override)
    and `load()` validates each comma-separated spec against the
    module-name regex, then imports the well-formed names via
    `importlib.import_module`. A spec that fails the regex is
    recorded with `status="rejected"` so the operator sees the
    offending name in the `plugins list` output.

    Hook dispatch: `run_pre` and `run_post` iterate the loaded
    records under a per-registry lock. A `disabled` record is
    skipped. A hook that raises increments the record's
    `consecutive_failures` counter and appends a `plugin_error`
    audit event; a successful call resets the counter. After
    `DISABLE_THRESHOLD` consecutive failures the plugin is
    auto-disabled and a `plugin_disabled` event is emitted.
    """

    def __init__(self, plugin_env: Optional[str] = None) -> None:
        self._plugin_env = (
            plugin_env
            if plugin_env is not None
            else os.environ.get("TASKQ_PLUGINS", "")
        )
        self._records: List[PluginRecord] = []
        # Per-registry lock guarding the consecutive_failures /
        # disabled transitions so concurrent `run --all` hook
        # invocations cannot race the disable threshold.
        self._lock = threading.Lock()

    @property
    def records(self) -> List[PluginRecord]:
        """[FR-07] Snapshot of the loaded plugin records."""
        return list(self._records)

    def load(self) -> None:
        """[FR-07] Resolve every allowlisted spec.

        Names that fail the regex become `status="rejected"`
        records; import errors become `status="failed"` records;
        modules with no recognised hook are also `status="failed"`.
        The loader never raises — the runner can still proceed
        with the partially successful records.
        """
        for spec in parse_plugin_specs(self._plugin_env):
            self._records.append(self._resolve_spec(spec))

    @staticmethod
    def _resolve_spec(spec: str) -> PluginRecord:
        """[FR-07] Resolve one allowlisted spec into a `PluginRecord`.

        The regex whitelist is checked *before* the import so a path
        or URL form never reaches `importlib.import_module`
        (SPEC §3 FR-07 line 157, NFR-02). Every failure mode returns
        a record rather than raising, so one bad spec cannot hide the
        rest of the allowlist from `plugins list`.
        """
        record = PluginRecord(name=spec)

        if not PLUGIN_NAME_RE.match(spec):
            record.status = "rejected"
            record.error = "not a module name"
            return record

        try:
            module = importlib.import_module(spec)
        except Exception as exc:  # noqa: BLE001 — NFR-03: re-raise/record
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            return record

        # The module imported, so report it even when it turns out to
        # expose no dispatchable hook — the operator needs to see that
        # the import itself succeeded.
        record.module = module
        record.hooks = [hook for hook in HOOK_NAMES if hasattr(module, hook)]
        if not record.hooks:
            record.status = "failed"
            record.error = "no pre_run or post_run hook"
            return record

        record.status = "loaded"
        return record

    def run_pre(self, task, *, task_id: str, correlation_id: str) -> None:
        """[FR-07] Invoke `pre_run(task)` for every non-disabled plugin.

        Exceptions are caught and logged as `plugin_error` audit
        events; the runner continues. After `DISABLE_THRESHOLD`
        consecutive failures the plugin is auto-disabled and a
        `plugin_disabled` event is emitted.
        """
        self._invoke_phase(
            "pre_run", task, task_id=task_id, correlation_id=correlation_id
        )

    def run_post(
        self,
        task,
        result,
        *,
        task_id: str,
        correlation_id: str,
    ) -> None:
        """[FR-07] Invoke `post_run(task, result)` for every non-disabled plugin."""
        self._invoke_phase(
            "post_run",
            task,
            result,
            task_id=task_id,
            correlation_id=correlation_id,
        )

    def _invoke_phase(
        self,
        hook_name: str,
        task,
        result=None,
        *,
        task_id: str,
        correlation_id: str,
    ) -> None:
        with self._lock:
            snapshot = list(self._records)
        for record in snapshot:
            if record.disabled:
                continue
            if hook_name not in record.hooks:
                continue
            self._call_hook(record, hook_name, task, result, task_id, correlation_id)

    def _call_hook(
        self,
        record: PluginRecord,
        hook_name: str,
        task,
        result,
        task_id: str,
        correlation_id: str,
    ) -> None:
        """[FR-07] Invoke one hook with attempt-counter bookkeeping."""
        with self._lock:
            if record.disabled:
                return
            try:
                if hook_name == "pre_run":
                    record.module.pre_run(task)
                else:
                    record.module.post_run(task, result)
            except Exception as exc:  # noqa: BLE001 — NFR-03: record
                record.consecutive_failures += 1
                self._audit_plugin_event(
                    "plugin_error",
                    record=record,
                    hook_name=hook_name,
                    task_id=task_id,
                    correlation_id=correlation_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if record.consecutive_failures >= DISABLE_THRESHOLD:
                    record.disabled = True
                    self._audit_plugin_event(
                        "plugin_disabled",
                        record=record,
                        hook_name=hook_name,
                        task_id=task_id,
                        correlation_id=correlation_id,
                    )
            else:
                # Successful hook resets the consecutive-failure
                # counter so a single successful pre_run between
                # two failures does not push the plugin to the
                # disable threshold.
                record.consecutive_failures = 0

    @staticmethod
    def _audit_plugin_event(
        event: str,
        *,
        record: PluginRecord,
        hook_name: str,
        task_id: str,
        correlation_id: str,
        **detail: Any,
    ) -> None:
        """[FR-07] Append one plugin lifecycle event to the audit log.

        Both FR-07 events (`plugin_error`, `plugin_disabled`) share
        the same envelope — the offending plugin, the hook that was
        being dispatched, and the current consecutive-failure count —
        so the naming of those fields lives here rather than being
        spelled out at each call site. Event-specific fields (e.g.
        `error`) are passed through `detail`.
        """
        append_audit_event(
            {
                "event": event,
                "task_id": task_id,
                "correlation_id": correlation_id,
                "detail": {
                    "plugin": record.name,
                    "hook": hook_name,
                    "consecutive_failures": record.consecutive_failures,
                    **detail,
                },
            }
        )
