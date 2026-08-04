"""Taskq plugin fixture — always-raises pre_run hook.

Used by the FR-07 plugin-error / plugin-disabled acceptance tests to
exercise the "a plugin that raises must not abort task execution"
behaviour and the "after 3 consecutive failures the plugin is
disabled" carve-out.

Citations:
    SPEC.md §3 FR-07 — "Plugin 拋出例外 → 不得中斷任務執行: 記錄
        plugin_error 稽核事件"。
    SPEC.md §3 FR-07 — "連續 3 次失敗的 plugin 於本次執行內停用".
"""


def pre_run(task) -> None:
    """Always raise — exercises the plugin-error recovery path."""
    raise RuntimeError("raiser: pre_run always raises")


def post_run(task, result) -> None:
    """No-op post-execution hook (raises only on pre_run)."""
