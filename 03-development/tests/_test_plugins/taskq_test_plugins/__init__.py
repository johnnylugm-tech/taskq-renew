"""Test-only plugin package.

This package exists solely so `TASKQ_PLUGINS` test fixtures can
reference real, importable Python modules. The package is NOT part of
the production source — it lives under `03-development/tests/` and is
only injected into the child process's `PYTHONPATH` by the FR-07 test
suite (the production code never imports it).

The two fixtures provided are:

- `taskq_test_plugins.noop` — exposes both `pre_run` and `post_run`
  hooks with no side effects; used for the FR-07 happy-path
  `plugins list` test.
- `taskq_test_plugins.raiser` — exposes a `pre_run` hook that raises
  `RuntimeError` on every invocation; used to drive the
  plugin-error / plugin-disabled acceptance tests.
"""
