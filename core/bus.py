import asyncio
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from pydantic import ValidationError

from core.schema import (
    EventPayload, InboxEvent, InboxEventType, NotificationReason, OutboxEvent, OutboxEventType, PMTaskCheck, PMTaskVerdict,
    PlanPrepared, PlanningProgress, ProjectReport, ProjectStatus, TaskDAG, TaskExecutionFailure, TaskRunRef,
)
from core.state_manager import StateFormatError, write_atomic


Event = TypeVar("Event", InboxEvent, OutboxEvent)


def make_inbox(
    event_type: InboxEventType, *, project_id: str, task_id: str | None = None,
    summary: str = "", cost_usd: float = 0.0,
    event_id: str | None = None, output_artifacts: tuple[str, ...] = (), replace_plan: bool = False,
    action_ref: str | None = None, execution: TaskRunRef | None = None, verdict: PMTaskVerdict | None = None,
    checks: tuple[PMTaskCheck, ...] = (), execution_failure: TaskExecutionFailure | None = None,
    confirm_worker_stopped: bool = False,
) -> InboxEvent:
    return InboxEvent(
        event_id=event_id or f"evt_{uuid4().hex}", timestamp=int(time.time()), type=event_type, task_id=task_id,
        project_id=project_id, replace_plan=replace_plan, action_ref=action_ref,
        payload=EventPayload(cost_usd=cost_usd, summary=summary, output_artifacts=output_artifacts),
        execution=execution, verdict=verdict, checks=checks, execution_failure=execution_failure,
        confirm_worker_stopped=confirm_worker_stopped,
    )


def make_outbox(
    event_type: OutboxEventType, *, project_id: str, task_id: str | None = None,
    summary: str = "", event_id: str | None = None,
    progress: PlanningProgress | None = None, plan: PlanPrepared | None = None,
    reason_code: NotificationReason | None = None, report: ProjectReport | None = None,
) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id or f"evt_{uuid4().hex}", timestamp=int(time.time()), type=event_type, task_id=task_id,
        project_id=project_id, payload=EventPayload(cost_usd=0.0, summary=summary), progress=progress,
        plan=plan, reason_code=reason_code, report=report,
    )


class EventBus:
    """Both queues are written by the Python runtime under its shared workspace lock."""

    def __init__(self, workspace: str | Path, lock: asyncio.Lock | None = None):
        self.workspace = Path(workspace).resolve()
        self.inbox_path = self.workspace / "inbox.jsonl"
        self.outbox_path = self.workspace / "outbox.jsonl"
        self.lock = lock if lock is not None else asyncio.Lock()

    async def initialize(self) -> None:
        async with self.lock:
            for path in (self.inbox_path, self.outbox_path):
                if not path.exists():
                    write_atomic(path, "")

    def _read(self, path: Path, model: type[Event]) -> list[Event]:
        events = []
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    events.append(model.model_validate_json(line))
                except ValidationError as error:
                    raise StateFormatError(f"Invalid {path}:{number}: {error}") from error
        return events

    def _write(self, path: Path, events: Iterable[Event]) -> None:
        write_atomic(path, "".join(event.model_dump_json() + "\n" for event in events))

    def inbox(self, project_id: str | None = None) -> list[InboxEvent]:
        events = self._read(self.inbox_path, InboxEvent)
        return events if project_id is None else [event for event in events if event.project_id == project_id]

    def outbox(self, project_id: str | None = None) -> list[OutboxEvent]:
        events = self._read(self.outbox_path, OutboxEvent)
        return events if project_id is None else [event for event in events if event.project_id == project_id]

    def replace_inbox(self, events: Iterable[InboxEvent]) -> None:
        """Persist a mutation of the complete inbox. The caller holds the workspace lock."""
        self._write(self.inbox_path, events)

    async def append_inbox(self, event: InboxEvent) -> None:
        async with self.lock:
            self._append_inbox(event)

    def _append_inbox(self, event: InboxEvent) -> None:
        self._append(self.inbox_path, (event,), self.inbox())

    def _append_outbox(self, event: OutboxEvent) -> None:
        self.append_outbox_batch((event,))

    def append_outbox_batch(self, events: Iterable[OutboxEvent]) -> None:
        """Publish events atomically. The caller holds the workspace lock."""
        self._append(self.outbox_path, events, self.outbox())

    def _append(self, path: Path, additions: Iterable[Event], events: list[Event]) -> None:
        known = {event.event_id: event for event in events}
        size = len(events)
        transient = {"timestamp", "consumed", "superseded_by", "deleted_at"}
        for event in additions:
            prior = known.get(event.event_id)
            if prior is not None:
                if prior.model_dump(exclude=transient) != event.model_dump(exclude=transient):
                    raise StateFormatError(f"Conflicting event {event.event_id!r} in {path}")
            else:
                events.append(event)
                known[event.event_id] = event
        if len(events) != size:
            self._write(path, events)

    async def pending_inbox(self) -> list[InboxEvent]:
        async with self.lock:
            return [event for event in self.inbox() if not event.consumed]

    def feedback(self, project_id: str) -> list[InboxEvent]:
        return [event for event in self.inbox(project_id) if event.type == InboxEventType.USER_FEEDBACK
                and event.superseded_by is None and event.deleted_at is None]

    def start_token(self, dag: TaskDAG, feedback: list[InboxEvent] | None = None) -> str | None:
        if dag.project_status != ProjectStatus.IDLE or not dag.tasks:
            return None
        feedback = self.feedback(dag.project_id) if feedback is None else feedback
        token = dag.start_approval_base
        if not feedback:
            return token
        latest = feedback[-1]
        if any(not event.consumed for event in feedback) or (latest.replace_plan and latest.event_id not in dag.applied_feedback):
            return None
        return f"{token}-{latest.event_id}"

    def _feedback_is_current(self, batch: tuple[InboxEvent, ...], *, complete: bool = False) -> bool:
        current = tuple(event for event in self.feedback(batch[0].project_id) if not event.consumed)
        return current == batch if complete else all(event in current for event in batch)

    def _ack_feedback(self, batch: tuple[InboxEvent, ...]) -> None:
        events = self.inbox()
        matched = [event for event in events if not event.consumed and event in batch]
        if matched:
            for event in matched:
                event.consumed = True
            self.replace_inbox(events)

    def _ack_inbox(self, event_id: str) -> None:
        events = self.inbox()
        for event in events:
            if event.event_id == event_id:
                if not event.consumed:
                    event.consumed = True
                    self.replace_inbox(events)
                return
        raise StateFormatError(f"Unknown Inbox event {event_id!r}")

    def _latest_failure(self, project_id: str, task_id: str) -> OutboxEvent | None:
        return next((event for event in reversed(self.outbox(project_id))
                     if event.type == OutboxEventType.ALERT_ESCALATED and event.task_id == task_id), None)
