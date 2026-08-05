"""[FR-01/FR-02] Pydantic models for task submission, storage, and
execution results.

Citations:
    SPEC.md §3 FR-01 lines 72-94 — required validation rules and the
        shape of a successful submission (id, status, command, name,
        created_at, depends_on).
    SPEC.md §3 FR-01 line 88 — uuid4 前 8 hex task id.
    SPEC.md §3 FR-01 lines 80-82 — non-empty / length / injection
        character rules; reject on any failure.
    SPEC.md §3 FR-01 line 82 — seven-character injection blacklist
        (`;` `|` `&` `$` `>` `<` `` ` ``).
    SPEC.md §3 FR-02 lines 105-118 — executor result fields
        (`exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`,
        `finished_at`).
    SRS.md line 81 — `pydantic` v2 model `TaskSubmission`.
    SRS.md line 53 — validation via `pydantic` v2 models.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

# pragma: no error-handling

#: [FR-01] Seven-character injection blacklist (SPEC.md §3 line 82).
INJECTION_CHARS: frozenset[str] = frozenset(";|&$><`")

#: [FR-01] Inclusive length cap (SPEC.md §3 line 81: `> 1000` rejects,
#: 1000 itself is accepted).
COMMAND_MAX_LEN: int = 1000


def _new_task_id() -> str:
    """[FR-01] Generate an 8-hex-char task id (uuid4 prefix).

    Citations:
        SPEC.md §3 FR-01 line 88 — uuid4 前 8 hex.
    """
    return uuid4().hex[:8]


def _utcnow() -> datetime:
    """[FR-01] UTC `datetime` for `created_at` reproducibility."""
    return datetime.now(timezone.utc)


class TaskSubmission(BaseModel):
    """[FR-01] Validated submission payload.

    Enforces the non-empty, length, and injection-character rules from
    SPEC.md §3 FR-01 lines 80-82. Storage-layer rules (name uniqueness
    across existing tasks; `--after` ids that exist) are enforced in
    `taskq_plus.cli.commands` because they require a store lookup the
    pure model cannot perform.

    Citations:
        SPEC.md §3 FR-01 lines 80-82 — non-empty / length / injection.
        SPEC.md §3 FR-01 line 84 — `--name` / `--after` parsing shape.
        SRS.md line 81 — pydantic v2 model `TaskSubmission`.
    """

    model_config = {"frozen": False}

    command: str = Field(..., description="Shell command (validated).")
    name: Optional[str] = Field(default=None, description="Optional friendly name.")
    depends_on: List[str] = Field(default_factory=list, description="Dependency ids.")

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        # SPEC.md §3 line 80 — non-empty / whitespace-only reject.
        if not value or not value.strip():
            raise ValueError("command is empty")
        # SPEC.md §3 line 81 — length cap is strict-greater-than 1000.
        if len(value) > COMMAND_MAX_LEN:
            raise ValueError(
                f"command length {len(value)} exceeds {COMMAND_MAX_LEN}"
            )
        # SPEC.md §3 line 82 — injection blacklist (NFR-02).
        bad = sorted(c for c in INJECTION_CHARS if c in value)
        if bad:
            raise ValueError(
                "command contains injection character(s): " + " ".join(bad)
            )
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class Task(BaseModel):
    """[FR-01/FR-02] Persisted task record (the on-disk shape).

    Citations:
        SPEC.md §3 FR-01 lines 88-91 — id / status / command / name /
            created_at / depends_on.
        SPEC.md §3 FR-02 lines 115-118 — `exit_code`, `stdout_tail`,
            `stderr_tail`, `duration_ms`, `finished_at` populated after
            execution.
    """

    id: str = Field(default_factory=_new_task_id)
    status: str = "pending"
    command: str
    name: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    depends_on: List[str] = Field(default_factory=list)
    # --- FR-02 result fields (populated after execution) ---
    exit_code: Optional[int] = None
    stdout_tail: Optional[str] = None
    stderr_tail: Optional[str] = None
    duration_ms: Optional[int] = None
    finished_at: Optional[datetime] = None
    # --- FR-04 cache replay marker ---
    cached: bool = False
