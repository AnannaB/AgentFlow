"""SQLite database lifecycle and concrete workflow repository."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from agentflow.domain.errors import ConcurrentStateChange, InvalidStateTransition
from agentflow.domain.models import (
    ApprovalRequest,
    RunEvent,
    StepAttempt,
    WorkflowRun,
    WorkflowStep,
    WorkflowTemplate,
)
from agentflow.domain.states import (
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
    RunStatus,
    StepStatus,
)
from agentflow.domain.transitions import ensure_run_transition, ensure_step_transition


class PersistenceError(RuntimeError):
    """Raised when persisted state is missing or cannot be safely updated."""


class SQLiteDatabase:
    """Creates short-lived SQLite connections and applies ordered migrations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with foreign keys enabled and always close it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run operations in a write-locked transaction, rolling back on errors."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def initialize(self) -> None:
        """Create migration metadata and apply every unapplied SQL migration."""
        with self.connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )"""
            )
        migration_dir = files("agentflow.storage.migrations")
        migrations = sorted(migration_dir.iterdir(), key=lambda item: item.name)
        for migration in migrations:
            if not migration.name.endswith(".sql"):
                continue
            prefix, _, name = migration.name.partition("_")
            version = int(prefix)
            script = migration.read_text(encoding="utf-8")
            with self.transaction() as connection:
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied:
                    continue
                for statement in script.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name.removesuffix(".sql"), _timestamp(_utc_now())),
                )


class SQLiteRepository:
    """CRUD repository for domain records, with transactional state changes."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self._local = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[SQLiteRepository]:
        """Group multiple repository operations into one atomic transaction."""
        if getattr(self._local, "connection", None) is not None:
            raise RuntimeError("nested repository transactions are not supported")
        with self.database.transaction() as connection:
            self._local.connection = connection
            try:
                yield self
            finally:
                del self._local.connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
        else:
            with self.database.connection() as connection:
                yield connection

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
        else:
            with self.database.transaction() as connection:
                yield connection

    # Workflow runs
    def create_run(self, entity: WorkflowRun) -> WorkflowRun:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            connection.execute(
                """INSERT INTO workflow_runs (
                    id, objective, status, planner_kind, plan_json, source_run_id,
                    error_code, error_summary, created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _run_values(entity),
            )
        return entity

    def get_run(self, entity_id: UUID) -> WorkflowRun | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (str(entity_id),)
            ).fetchone()
        return _run_from_row(row) if row else None

    def list_runs(
        self, limit: int = 50, before: tuple[datetime, UUID] | None = None
    ) -> list[WorkflowRun]:
        limit = _bounded_limit(limit, 200)
        with self._read_connection() as connection:
            if before is None:
                rows = connection.execute(
                    "SELECT * FROM workflow_runs ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM workflow_runs
                    WHERE created_at < ? OR (created_at = ? AND id < ?)
                    ORDER BY created_at DESC, id DESC LIMIT ?""",
                    (_timestamp(before[0]), _timestamp(before[0]), str(before[1]), limit),
                ).fetchall()
        return [_run_from_row(row) for row in rows]

    def update_run(self, entity: WorkflowRun) -> WorkflowRun:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            current_row = connection.execute(
                "SELECT status FROM workflow_runs WHERE id = ?", (str(entity.id),)
            ).fetchone()
            if current_row is None:
                raise PersistenceError(f"workflow run {entity.id} does not exist")
            current = RunStatus(current_row["status"])
            if current in TERMINAL_RUN_STATUSES:
                raise PersistenceError(f"terminal workflow run {entity.id} cannot be modified")
            if current != entity.status:
                raise InvalidStateTransition("run", current.value, entity.status.value)
            cursor = connection.execute(
                """UPDATE workflow_runs SET objective=?, status=?, planner_kind=?, plan_json=?,
                source_run_id=?, error_code=?, error_summary=?, created_at=?, updated_at=?,
                started_at=?, finished_at=? WHERE id=? AND status=?""",
                (*_run_values(entity)[1:], str(entity.id), current.value),
            )
            if cursor.rowcount != 1:
                raise ConcurrentStateChange(f"workflow run {entity.id} changed during update")
        return entity

    def delete_run(self, entity_id: UUID) -> bool:
        return self._delete("workflow_runs", entity_id)

    def transition_run(
        self,
        entity_id: UUID,
        target: RunStatus,
        details: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (str(entity_id),)
            ).fetchone()
            if row is None:
                raise PersistenceError(f"workflow run {entity_id} does not exist")
            current = _run_from_row(row)
            ensure_run_transition(current.status, target)
            now = _utc_now()
            changes: dict[str, Any] = {
                **current.model_dump(),
                "status": target,
                "updated_at": now,
            }
            if target == RunStatus.EXECUTING and current.started_at is None:
                changes["started_at"] = now
            if target in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
                changes["finished_at"] = now
            updated = WorkflowRun.model_validate(changes)
            cursor = connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, started_at=?, finished_at=? "
                "WHERE id=? AND status=?",
                (
                    target.value,
                    _timestamp(updated.updated_at),
                    _timestamp(updated.started_at) if updated.started_at else None,
                    _timestamp(updated.finished_at) if updated.finished_at else None,
                    str(entity_id),
                    current.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentStateChange(f"workflow run {entity_id} changed during transition")
            event_details = {**(details or {}), "from": current.status.value, "to": target.value}
            self._insert_event(
                connection,
                RunEvent(
                    run_id=entity_id,
                    event_type=f"run.transition.{target.value}",
                    details=event_details,
                ),
            )
        return updated

    # Workflow steps
    def create_step(self, entity: WorkflowStep) -> WorkflowStep:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            connection.execute(
                """INSERT INTO workflow_steps (
                    id, run_id, step_key, position, tool_name, arguments_json, depends_on_json,
                    required, approval_required, status, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _step_values(entity),
            )
        return entity

    def get_step(self, entity_id: UUID) -> WorkflowStep | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id=?", (str(entity_id),)
            ).fetchone()
        return _step_from_row(row) if row else None

    def list_steps(self, run_id: UUID) -> list[WorkflowStep]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_steps WHERE run_id=? ORDER BY position, id LIMIT 500",
                (str(run_id),),
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def update_step(self, entity: WorkflowStep) -> WorkflowStep:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            current_row = connection.execute(
                "SELECT status FROM workflow_steps WHERE id=?", (str(entity.id),)
            ).fetchone()
            if current_row is None:
                raise PersistenceError(f"workflow step {entity.id} does not exist")
            current = StepStatus(current_row["status"])
            if current in TERMINAL_STEP_STATUSES:
                raise PersistenceError(f"terminal workflow step {entity.id} cannot be modified")
            if current != entity.status:
                raise InvalidStateTransition("step", current.value, entity.status.value)
            values = _step_values(entity)
            cursor = connection.execute(
                """UPDATE workflow_steps SET run_id=?, step_key=?, position=?, tool_name=?,
                arguments_json=?, depends_on_json=?, required=?, approval_required=?, status=?,
                created_at=?, started_at=?, finished_at=? WHERE id=? AND status=?""",
                (*values[1:], str(entity.id), current.value),
            )
            if cursor.rowcount != 1:
                raise ConcurrentStateChange(f"workflow step {entity.id} changed during update")
        return entity

    def delete_step(self, entity_id: UUID) -> bool:
        return self._delete("workflow_steps", entity_id)

    def transition_step(
        self, entity_id: UUID, target: StepStatus, details: dict[str, Any] | None = None
    ) -> WorkflowStep:
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_steps WHERE id=?", (str(entity_id),)
            ).fetchone()
            if row is None:
                raise PersistenceError(f"workflow step {entity_id} does not exist")
            current = _step_from_row(row)
            ensure_step_transition(current.status, target)
            now = _utc_now()
            changes: dict[str, Any] = {**current.model_dump(), "status": target}
            if target == StepStatus.RUNNING and current.started_at is None:
                changes["started_at"] = now
            if target in {
                StepStatus.SUCCEEDED,
                StepStatus.FAILED,
                StepStatus.SKIPPED,
                StepStatus.CANCELLED,
            }:
                changes["finished_at"] = now
            updated = WorkflowStep.model_validate(changes)
            cursor = connection.execute(
                "UPDATE workflow_steps SET status=?, started_at=?, finished_at=? "
                "WHERE id=? AND status=?",
                (
                    target.value,
                    _timestamp(updated.started_at) if updated.started_at else None,
                    _timestamp(updated.finished_at) if updated.finished_at else None,
                    str(entity_id),
                    current.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentStateChange(f"workflow step {entity_id} changed during transition")
            event_details = {
                "step_id": str(entity_id),
                "from": current.status.value,
                "to": target.value,
            }
            event_details = {**(details or {}), **event_details}
            self._insert_event(
                connection,
                RunEvent(
                    run_id=current.run_id,
                    event_type=f"step.transition.{target.value}",
                    details=event_details,
                ),
            )
        return updated

    # Step attempts
    def create_attempt(self, entity: StepAttempt) -> StepAttempt:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            connection.execute(
                """INSERT INTO step_attempts (
                    id, step_id, attempt_number, status, input_hash, output_json, error_code,
                    error_summary, duration_ms, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _attempt_values(entity),
            )
        return entity

    def get_attempt(self, entity_id: UUID) -> StepAttempt | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM step_attempts WHERE id=?", (str(entity_id),)
            ).fetchone()
        return _attempt_from_row(row) if row else None

    def list_attempts(self, step_id: UUID, limit: int = 500) -> list[StepAttempt]:
        limit = _bounded_limit(limit, 500)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM step_attempts WHERE step_id=? ORDER BY attempt_number LIMIT ?",
                (str(step_id), limit),
            ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    def update_attempt(self, entity: StepAttempt) -> StepAttempt:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            values = _attempt_values(entity)
            cursor = connection.execute(
                """UPDATE step_attempts SET step_id=?, attempt_number=?, status=?, input_hash=?,
                output_json=?, error_code=?, error_summary=?, duration_ms=?, started_at=?,
                finished_at=? WHERE id=?""",
                (*values[1:], str(entity.id)),
            )
            _require_updated(cursor.rowcount, "step attempt", entity.id)
        return entity

    def delete_attempt(self, entity_id: UUID) -> bool:
        return self._delete("step_attempts", entity_id)

    # Approval requests
    def create_approval(self, entity: ApprovalRequest) -> ApprovalRequest:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            self._validate_approval_scope(connection, entity)
            connection.execute(
                """INSERT INTO approval_requests (
                    id, run_id, step_id, status, plan_digest, decision_note, requested_at,
                    resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                _approval_values(entity),
            )
        return entity

    def get_approval(self, entity_id: UUID) -> ApprovalRequest | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE id=?", (str(entity_id),)
            ).fetchone()
        return _approval_from_row(row) if row else None

    def list_approvals(self, run_id: UUID, limit: int = 200) -> list[ApprovalRequest]:
        limit = _bounded_limit(limit, 500)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM approval_requests WHERE run_id=? ORDER BY requested_at, id LIMIT ?",
                (str(run_id), limit),
            ).fetchall()
        return [_approval_from_row(row) for row in rows]

    def update_approval(self, entity: ApprovalRequest) -> ApprovalRequest:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            self._validate_approval_scope(connection, entity)
            values = _approval_values(entity)
            cursor = connection.execute(
                """UPDATE approval_requests SET run_id=?, step_id=?, status=?, plan_digest=?,
                decision_note=?, requested_at=?, resolved_at=? WHERE id=?""",
                (*values[1:], str(entity.id)),
            )
            _require_updated(cursor.rowcount, "approval request", entity.id)
        return entity

    def delete_approval(self, entity_id: UUID) -> bool:
        return self._delete("approval_requests", entity_id)

    # Run events
    def create_event(self, entity: RunEvent) -> RunEvent:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            self._insert_event(connection, entity)
        return entity

    def get_event(self, entity_id: UUID) -> RunEvent | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM run_events WHERE id=?", (str(entity_id),)
            ).fetchone()
        return _event_from_row(row) if row else None

    def list_events(self, run_id: UUID, limit: int = 200) -> list[RunEvent]:
        limit = _bounded_limit(limit, 1000)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id=? ORDER BY occurred_at, id LIMIT ?",
                (str(run_id), limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def update_event(self, entity: RunEvent) -> RunEvent:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            cursor = connection.execute(
                "UPDATE run_events SET run_id=?, event_type=?, details_json=?, occurred_at=? "
                "WHERE id=?",
                (
                    str(entity.run_id),
                    entity.event_type,
                    _json(entity.details),
                    _timestamp(entity.occurred_at),
                    str(entity.id),
                ),
            )
            _require_updated(cursor.rowcount, "run event", entity.id)
        return entity

    def delete_event(self, entity_id: UUID) -> bool:
        return self._delete("run_events", entity_id)

    # Workflow templates
    def create_template(self, entity: WorkflowTemplate) -> WorkflowTemplate:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            connection.execute(
                """INSERT INTO workflow_templates (
                    id, name, description, plan_json, version, created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                _template_values(entity),
            )
        return entity

    def get_template(self, entity_id: UUID) -> WorkflowTemplate | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_templates WHERE id=?", (str(entity_id),)
            ).fetchone()
        return _template_from_row(row) if row else None

    def list_templates(self, limit: int = 100) -> list[WorkflowTemplate]:
        limit = _bounded_limit(limit, 500)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_templates ORDER BY name COLLATE NOCASE, id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_template_from_row(row) for row in rows]

    def update_template(self, entity: WorkflowTemplate) -> WorkflowTemplate:
        entity = _validated_snapshot(entity)
        with self._write_connection() as connection:
            values = _template_values(entity)
            cursor = connection.execute(
                """UPDATE workflow_templates SET name=?, description=?, plan_json=?, version=?,
                created_at=?, updated_at=?, archived_at=? WHERE id=?""",
                (*values[1:], str(entity.id)),
            )
            _require_updated(cursor.rowcount, "workflow template", entity.id)
        return entity

    def delete_template(self, entity_id: UUID) -> bool:
        return self._delete("workflow_templates", entity_id)

    def _delete(self, table: str, entity_id: UUID) -> bool:
        allowed_tables = {
            "workflow_runs",
            "workflow_steps",
            "step_attempts",
            "approval_requests",
            "run_events",
            "workflow_templates",
        }
        if table not in allowed_tables:
            raise ValueError("unsupported table")
        with self._write_connection() as connection:
            cursor = connection.execute(f"DELETE FROM {table} WHERE id=?", (str(entity_id),))
            return cursor.rowcount == 1

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, entity: RunEvent) -> None:
        connection.execute(
            """INSERT INTO run_events(id, run_id, event_type, details_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                str(entity.id),
                str(entity.run_id),
                entity.event_type,
                _json(entity.details),
                _timestamp(entity.occurred_at),
            ),
        )

    @staticmethod
    def _validate_approval_scope(connection: sqlite3.Connection, entity: ApprovalRequest) -> None:
        if entity.step_id is None:
            return
        row = connection.execute(
            "SELECT run_id FROM workflow_steps WHERE id=?", (str(entity.step_id),)
        ).fetchone()
        if row is not None and row["run_id"] != str(entity.run_id):
            raise ValueError("approval step must belong to the same workflow run")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("SQLite timestamps must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bounded_limit(limit: int, maximum: int) -> int:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}")
    return limit


def _validated_snapshot[ModelT: BaseModel](entity: ModelT) -> ModelT:
    """Revalidate a JSON snapshot so nested mutable data cannot bypass model checks."""
    return type(entity).model_validate_json(entity.model_dump_json())


def _require_updated(rowcount: int, entity: str, entity_id: UUID) -> None:
    if rowcount != 1:
        raise PersistenceError(f"{entity} {entity_id} does not exist")


def _run_values(entity: WorkflowRun) -> tuple[Any, ...]:
    return (
        str(entity.id),
        entity.objective,
        entity.status.value,
        entity.planner_kind.value,
        _json(entity.plan.model_dump(mode="json")) if entity.plan else None,
        str(entity.source_run_id) if entity.source_run_id else None,
        entity.error_code,
        entity.error_summary,
        _timestamp(entity.created_at),
        _timestamp(entity.updated_at),
        _timestamp(entity.started_at),
        _timestamp(entity.finished_at),
    )


def _run_from_row(row: sqlite3.Row) -> WorkflowRun:
    data = dict(row)
    plan_json = data.pop("plan_json")
    data["plan"] = json.loads(plan_json) if plan_json is not None else None
    return _from_json(WorkflowRun, data)


def _step_values(entity: WorkflowStep) -> tuple[Any, ...]:
    return (
        str(entity.id),
        str(entity.run_id),
        entity.step_key,
        entity.position,
        entity.tool_name,
        _json(entity.arguments),
        _json(entity.depends_on),
        int(entity.required),
        int(entity.approval_required),
        entity.status.value,
        _timestamp(entity.created_at),
        _timestamp(entity.started_at),
        _timestamp(entity.finished_at),
    )


def _step_from_row(row: sqlite3.Row) -> WorkflowStep:
    data = dict(row)
    data["arguments"] = json.loads(data.pop("arguments_json"))
    data["depends_on"] = json.loads(data.pop("depends_on_json"))
    data["required"] = bool(data["required"])
    data["approval_required"] = bool(data["approval_required"])
    return _from_json(WorkflowStep, data)


def _attempt_values(entity: StepAttempt) -> tuple[Any, ...]:
    return (
        str(entity.id),
        str(entity.step_id),
        entity.attempt_number,
        entity.status.value,
        entity.input_hash,
        _json(entity.output.model_dump(mode="json")) if entity.output else None,
        entity.error_code,
        entity.error_summary,
        entity.duration_ms,
        _timestamp(entity.started_at),
        _timestamp(entity.finished_at),
    )


def _attempt_from_row(row: sqlite3.Row) -> StepAttempt:
    data = dict(row)
    output = data.pop("output_json")
    data["output"] = json.loads(output) if output is not None else None
    return _from_json(StepAttempt, data)


def _approval_values(entity: ApprovalRequest) -> tuple[Any, ...]:
    return (
        str(entity.id),
        str(entity.run_id),
        str(entity.step_id) if entity.step_id else None,
        entity.status.value,
        entity.plan_digest,
        entity.decision_note,
        _timestamp(entity.requested_at),
        _timestamp(entity.resolved_at),
    )


def _approval_from_row(row: sqlite3.Row) -> ApprovalRequest:
    return _from_json(ApprovalRequest, dict(row))


def _event_from_row(row: sqlite3.Row) -> RunEvent:
    data = dict(row)
    data["details"] = json.loads(data.pop("details_json"))
    return _from_json(RunEvent, data)


def _template_values(entity: WorkflowTemplate) -> tuple[Any, ...]:
    return (
        str(entity.id),
        entity.name,
        entity.description,
        _json(entity.plan.model_dump(mode="json")),
        entity.version,
        _timestamp(entity.created_at),
        _timestamp(entity.updated_at),
        _timestamp(entity.archived_at),
    )


def _template_from_row(row: sqlite3.Row) -> WorkflowTemplate:
    data = dict(row)
    data["plan"] = json.loads(data.pop("plan_json"))
    return _from_json(WorkflowTemplate, data)


def _from_json[ModelT: BaseModel](model: type[ModelT], data: dict[str, Any]) -> ModelT:
    return model.model_validate_json(_json(data))
