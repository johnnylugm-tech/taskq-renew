"""[NFR-03] Direct tests for `storage.atomic` exception branches.

The atomic_write_json guard wraps both `mkstemp` and the `fdopen +
json.dump + chmod + os.replace` block in `try/except Exception` to
clean up the temp file before re-raising. Two exception sites are
uncovered by the FR tests because they need controlled failures:

* `mkstemp` raising → `except Exception: raise` (no cleanup yet)
* `os.unlink(tmp_name)` failing with FileNotFoundError → `except
  FileNotFoundError: pass` (TOCTOU window already passed)

The FR tests do exercise the happy path through `_write_atomic`, but
those modules are not the unit under test here — they route through
the same code, but coverage measurement splits per executable
statement. The branches sit in `atomic.py:49-50` and `atomic.py:59-60`
and need this dedicated sweep.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from taskq_plus.storage.atomic import atomic_write_json


def test_atomic_write_json_propagates_mkstemp_failure(tmp_path: Path) -> None:
    """[NFR-03] `mkstemp` failure → re-raise, no temp leak.

    atomic_write_json's first `try` exists to satisfy the fd-leak
    invariant: if `mkstemp` raises, no file descriptor is open and the
    caller sees the same exception they would have seen without the
    helper. Coverage of lines 49-50.
    """
    target = tmp_path / "mkstemp_fail.json"

    with patch(
        "taskq_plus.storage.atomic.tempfile.mkstemp",
        side_effect=OSError("simulated mkstemp failure"),
    ):
        with pytest.raises(OSError, match="simulated mkstemp failure"):
            atomic_write_json(target, {"x": 1})

    assert not target.exists()


def test_atomic_write_json_unlink_filenotfound_is_swallowed(
    tmp_path: Path,
) -> None:
    """[NFR-03] `os.unlink(tmp_name)` raising FileNotFoundError → pass.

    The write path raises before cleanup; cleanup calls `os.unlink`
    which can race with the failure (mkstemp succeeded, write failed,
    but the temp file was already removed by the time we get here).
    The guard `except FileNotFoundError: pass` swallows that and the
    original exception still propagates. Coverage of lines 59-60.
    """
    target = tmp_path / "write_then_unlink.json"

    real_unlink = Path.unlink

    def _raise_filenotfound(self: Path) -> None:
        if str(self).endswith(".json.tmp"):
            raise FileNotFoundError(str(self))
        real_unlink(self)

    with patch(
        "taskq_plus.storage.atomic.json.dump",
        side_effect=ValueError("simulated dump failure"),
    ):
        with patch("taskq_plus.storage.atomic.os.unlink", side_effect=FileNotFoundError("raced")):
            with pytest.raises(ValueError, match="simulated dump failure"):
                atomic_write_json(target, {"x": 1})

    assert not target.exists()
