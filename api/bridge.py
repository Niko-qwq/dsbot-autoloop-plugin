import codecs
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Iterator
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, JsonValue, StringConstraints, TypeAdapter, ValidationError

from core.bus import make_inbox
from core.schema import (
    InboxEvent, InboxEventType, NonEmptyText, OutboxEvent, OutboxEventType, PrepareProject, ProjectBinding,
    ProjectStatus, StrictModel, Task, TaskDAG, TaskStatus, UIActionType,
)
from core.state_manager import StateFormatError, read_model, task_escalation_id, task_retry_kind, write_atomic
from runtime.daemon import AutoloopDaemon
from runtime.pm_agent import PMRemoval, PMTask, pm_task_context, validate_acceptance_tasks


ACTION = "/api/autoloop/action"
OUTBOX = "/api/autoloop/outbox"
NOTICES = "/api/autoloop/notices"
CONTEXT = "/api/autoloop/chief-context"
COMMUNICATION = "/api/autoloop/chief-communication"
DETAILS = "/api/autoloop/details"
PANEL = "/api/autoloop/panel"
FILES = "/api/autoloop/files"
PM_PLAN = "/api/autoloop/pm-plan"
RETRY_DRAFT = "/api/autoloop/retry-draft"
PROPOSE_PM_PLAN = "pm-plan-proposal"
PM_STATUS = "pm-status"
MAX_PREVIEW_BYTES = 128 * 1024
PM_STATUS_FAILED_LIMIT = 4
PM_STATUS_EXCERPT_CHARS = 160
CONTEXT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}")
EVENT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,239}")
CONTEXT_TASK_LIMIT = 3
EVIDENCE_TAIL_CHARS = 1600
EVIDENCE_TAIL_LINES = 16


class Action(StrictModel):
    action: UIActionType
    task_id: NonEmptyText | None
    confirm_worker_stopped: bool = False


Sequence = Annotated[int, Field(ge=0)]
DeliveryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class NativeDelivery(StrictModel):
    project_id: NonEmptyText
    event_id: NonEmptyText
    session_id: NonEmptyText
    request_id: NonEmptyText
    start_seq: Sequence
    attempt: Annotated[int, Field(ge=1)] = 1
    started_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    status: Literal["SENDING", "NEEDS_ATTENTION", "DELIVERED"] = "SENDING"
    facts: dict[str, JsonValue]
    error: str | None = None
    retryable: bool = False
    message_seq: Sequence | None = None
    turn: Sequence | None = None
    text: str | None = None
    delivered_at: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class ChiefDeliveries(StrictModel):
    deliveries: dict[str, NativeDelivery] = Field(default_factory=dict)


class ClaimDelivery(StrictModel):
    operation: Literal["deliver_claim"]
    start_seq: Sequence


class DeliveryReference(StrictModel):
    request_id: NonEmptyText


class FinishDelivery(DeliveryReference):
    operation: Literal["deliver_finish"]
    message_seq: Sequence
    turn: Sequence
    text: DeliveryText | None = None


class AttentionDelivery(DeliveryReference):
    operation: Literal["deliver_attention"]
    error: DeliveryText
    retryable: bool


class RetryDelivery(DeliveryReference):
    operation: Literal["deliver_retry"]
    start_seq: Sequence | None = None


DeliveryCommand = ClaimDelivery | FinishDelivery | AttentionDelivery | RetryDelivery
delivery_request = TypeAdapter(DeliveryCommand)


class FeedbackAction(StrictModel):
    action: Literal["USER_FEEDBACK"]
    task_id: None = None
    issue: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    replace_plan: bool = False


class FeedbackInboxAction(StrictModel):
    action: Literal["RETRY_FEEDBACK", "DELETE_FEEDBACK"]
    task_id: None = None


class DraftRetry(StrictModel):
    project_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    attempt: Annotated[int, Field(ge=0)]


class PMPlanChange(StrictModel):
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    tasks: Annotated[list[PMTask], Field(max_length=50)] = Field(default_factory=list)
    removals: Annotated[list[PMRemoval], Field(max_length=50)] = Field(default_factory=list)

    def removal_map(self) -> dict[str, str | None]:
        entries = {entry.task_id: entry.replaced_by for entry in self.removals}
        if len(entries) != len(self.removals):
            raise ActionConflict("A task appears more than once in the removal list")
        return entries


class PMPlanApproval(StrictModel):
    proposal_id: Annotated[str, Field(pattern=r"^pm-[0-9a-f]{32}$")]


class PMPlanProposal(PMPlanApproval):
    project_id: NonEmptyText
    session_id: NonEmptyText
    fingerprint: NonEmptyText
    change: PMPlanChange
    preview: dict[str, JsonValue]


action_request = TypeAdapter(Action | FeedbackAction | FeedbackInboxAction)


class Request(StrictModel):
    method: Literal["GET", "POST"]
    path: NonEmptyText
    body: str | None = None
    session_id: str | None = None
    chief_sessions: tuple[NonEmptyText, ...] | None = None
    event_id: str | None = None
    project_id: str | None = None
    file_path: str | None = None
    task_id: str | None = None
    include_logs: bool = False

    @property
    def chief_session_ids(self) -> tuple[str, ...]:
        if self.session_id is None:
            return ()
        if self.chief_sessions is None:
            return (self.session_id,)
        return self.chief_sessions if self.session_id in self.chief_sessions else ()


class ActionConflict(RuntimeError):
    pass


class ProjectScopeDenied(RuntimeError):
    pass


PROJECT_ACTIONS = {
    UIActionType.START_PROJECT: (ProjectStatus.IDLE,),
    UIActionType.PAUSE_PROJECT: (ProjectStatus.ACTIVE,),
    UIActionType.RESUME_PROJECT: (ProjectStatus.PAUSED, ProjectStatus.ALERT),
}


class AutoloopBridge:
    def __init__(
        self, daemon: AutoloopDaemon,
        prepare: Callable[[PrepareProject, tuple[str, ...] | None], Awaitable[dict[str, JsonValue]]],
        read_draft: Callable[[tuple[str, ...], str | None], dict[str, JsonValue] | None],
        retry_draft: Callable[[str, int, tuple[str, ...] | None], Awaitable[dict[str, JsonValue]]],
    ):
        self.daemon = daemon
        self.manager = daemon.manager
        self.bus = daemon.bus
        self.pm_proposal_path = self.manager.workspace / "pm-plan-proposal.json"
        self.deliveries_path = self.manager.workspace / "chief-communications.json"
        self.history = self.manager.workspace / "project_history"
        self.prepare = prepare
        self.read_draft = read_draft
        self.retry_draft = retry_draft

    def initialize_deliveries(self) -> None:
        if self.deliveries_path.exists():
            self._deliveries()
        else:
            self._save_deliveries(ChiefDeliveries())

    def _save_deliveries(self, state: ChiefDeliveries) -> None:
        write_atomic(self.deliveries_path, state.model_dump_json() + "\n")

    def _deliveries(self) -> ChiefDeliveries:
        return read_model(self.deliveries_path, ChiefDeliveries)

    def _snapshot(self, dag: TaskDAG, *, live: bool = True) -> dict[str, JsonValue]:
        task_actions = {}
        if live and dag.tasks:
            failures = {event.task_id: event for event in self.bus.outbox(dag.project_id)
                        if event.type == OutboxEventType.ALERT_ESCALATED and event.task_id is not None}
            for task in dag.tasks:
                alert = failures.get(task.task_id)
                if alert is not None and self._can_retry(task, alert):
                    task_actions[task.task_id] = {
                        "actions": ["RETRY"], "event_id": alert.event_id,
                        "confirm_worker_stopped": alert.reason_code == "WORKER_INTERRUPTED",
                    }
        return {
            "dag": dag.model_dump(mode="json", exclude={"binding", "budget"}),
            "budget": dag.budget.model_dump(mode="json"),
            "active_workers": self.daemon.router.active_workers if live else 0,
            "task_actions": task_actions,
        }

    @staticmethod
    def _can_retry(task: Task, alert: OutboxEvent) -> bool:
        return task_retry_kind(task) is not None and task_escalation_id(task) == alert.event_id

    @staticmethod
    def _binding(dag: TaskDAG) -> ProjectBinding:
        if dag.binding is None or dag.binding.project_id != dag.project_id:
            raise StateFormatError(f"Project {dag.project_id} has no matching Chief binding")
        return dag.binding

    def _has_static_preview(self, dag: TaskDAG) -> bool:
        if not any(task.status == TaskStatus.COMPLETED for task in dag.tasks):
            return False
        entry = self.manager.project_directory(dag.project_id) / "index.html"
        try:
            return entry.is_file() and entry.resolve() == entry
        except (OSError, RuntimeError):
            return False

    async def handle(self, request: Request) -> tuple[int, dict[str, JsonValue]]:
        try:
            if request.method == "POST" and request.path == "prepare":
                draft = PrepareProject.model_validate_json(request.body or "")
                if request.chief_sessions is not None and (
                    draft.session_id != request.session_id or draft.session_id not in request.chief_session_ids
                ):
                    raise ProjectScopeDenied("Prepare a project from Chief")
                return 202, await self.prepare(draft, request.chief_sessions)
            if request.method == "POST" and request.path == RETRY_DRAFT:
                retry = DraftRetry.model_validate_json(request.body or "")
                if request.session_id is None:
                    raise ProjectScopeDenied("Retry from the draft's conversation")
                if request.project_id is not None:
                    if request.project_id != retry.project_id:
                        raise ProjectScopeDenied("The PM is assigned to a different project")
                    chief_sessions = None
                else:
                    chief_sessions = request.chief_session_ids
                    if not chief_sessions:
                        raise ProjectScopeDenied("Retry from the draft's Chief or PM conversation")
                return 202, await self.retry_draft(retry.project_id, retry.attempt, chief_sessions)
            async with self.manager.lock:
                match request.method, request.path:
                    case "GET", path if path == PANEL:
                        return 200, self._panel(request)
                    case "GET", path if path == PM_STATUS:
                        return self._pm_status(request)
                    case "GET", path if path == FILES:
                        return self._files(request)
                    case "POST", path if path == PROPOSE_PM_PLAN:
                        return 200, self._propose_pm_plan(request)
                    case "POST", path if path == PM_PLAN:
                        return 200, self._apply_pm_plan(request)
                    case "POST", path if path == ACTION:
                        action = action_request.validate_json(request.body or "")
                        dag = self.manager.load()
                        if request.project_id is not None and request.project_id != dag.project_id:
                            raise ActionConflict("This action belongs to a different Autoloop project")
                        if isinstance(action, FeedbackInboxAction):
                            if request.session_id is not None:
                                self._check_active_chief(dag, request)
                            if request.project_id != dag.project_id or not request.event_id:
                                raise ActionConflict("This action requires the exact project and feedback event")
                            if action.action == "DELETE_FEEDBACK":
                                return 200, self._delete_feedback(dag, request.event_id)
                            return 200, self._retry_feedback(dag, request.event_id)
                        if isinstance(action, FeedbackAction):
                            draft = self.read_draft(request.chief_session_ids, None) if request.project_id is None else None
                            if draft:
                                if draft["status"] == "FAILED":
                                    raise ActionConflict("Retry the project draft from its card, or submit an updated brief")
                                raise ActionConflict("The new project's initial plan is still being prepared")
                            if request.session_id is not None:
                                self._check_active_chief(dag, request)
                            return 200, {
                                "ok": True, "project_id": dag.project_id,
                                "mode": ("replacement" if action.replace_plan else
                                         "revision" if dag.project_status == ProjectStatus.IDLE else "feedback"),
                                "event_id": self._feedback(dag, action.issue, request.event_id, action.replace_plan),
                            }
                        if request.session_id is not None:
                            self._check_active_chief(dag, request)
                            if action.action == UIActionType.RETRY and (request.project_id is None or request.event_id is None):
                                raise ActionConflict("Task action requires the exact project and event reference")
                        return 200, self._enqueue(dag, action, request.event_id)
                    case "GET", path if path == OUTBOX:
                        return 200, self._outbox(request)
                    case "GET", path if path == NOTICES:
                        return 200, self._notices()
                    case "GET", path if path == CONTEXT:
                        return 200, self._chief_context(request)
                    case "POST", path if path == COMMUNICATION:
                        return 200, self._write_delivery(request)
                    case "GET", path if path == DETAILS:
                        return self._details(request)
                    case _:
                        return 404, {"error": "Unknown Autoloop route"}
        except ValidationError as error:
            return 400, {"error": str(error)}
        except ActionConflict as error:
            return 409, {"error": str(error)}
        except ProjectScopeDenied as error:
            return 403, {"error": str(error)}

    def _check_active_chief(self, dag: TaskDAG, request: Request) -> None:
        if not request.chief_session_ids or not dag.tasks:
            raise ProjectScopeDenied("No project is bound to this Chief session")
        if self._binding(dag).session_id not in request.chief_session_ids:
            raise ProjectScopeDenied("The project belongs to another Chief session")

    def _pm_plan_dag(self, request: Request) -> TaskDAG:
        dag = self.manager.load()
        if not request.session_id or request.project_id != dag.project_id or not dag.tasks:
            raise ProjectScopeDenied("Plan changes require the live project PM conversation")
        return dag

    def _read_pm_proposal(self, dag: TaskDAG, session_id: str | None) -> PMPlanProposal | None:
        try:
            proposal = read_model(self.pm_proposal_path, PMPlanProposal)
        except FileNotFoundError:
            return None
        return proposal if (proposal.project_id, proposal.session_id) == (dag.project_id, session_id) else None

    def _pm_proposal_is_current(self, dag: TaskDAG, proposal: PMPlanProposal) -> bool:
        if proposal.fingerprint != self.manager.plan_fingerprint(dag):
            return False
        # The plan fingerprint excludes execution state, so affected tasks can become uneditable before it changes.
        change = proposal.change
        try:
            self.manager.edit_tasks(dag, [task.to_task() for task in change.tasks], change.removal_map(),
                                    allow_pm_amendments=True)
        except StateFormatError:
            return False
        return True

    def _propose_pm_plan(self, request: Request) -> dict[str, JsonValue]:
        dag = self._pm_plan_dag(request)
        change = PMPlanChange.model_validate_json(request.body or "")
        removals = change.removal_map()
        try:
            brief = self._binding(dag).brief
            validate_acceptance_tasks(change.tasks, brief, complete_plan=False)
            revised = self.manager.edit_tasks(dag, [task.to_task() for task in change.tasks], removals,
                                              allow_pm_amendments=True)
            validate_acceptance_tasks(revised.tasks, brief, complete_plan=True)
        except StateFormatError as error:
            raise ActionConflict(str(error)) from error
        before = {task.task_id: task for task in dag.tasks}
        fields = {"task_id", "title", "acceptance", "dependencies"}
        added = [task.model_dump(mode="json", include=fields) for task in revised.tasks if task.task_id not in before]
        updated = []
        for task in revised.tasks:
            previous = before.get(task.task_id)
            if previous is None or task == previous:
                continue
            view = task.model_dump(mode="json", include=fields)
            if previous.permission_tier != task.permission_tier:
                view.update(previous_permission_tier=previous.permission_tier, permission_tier=task.permission_tier)
            updated.append(view)
        removed = [{"task_id": task_id, "title": before[task_id].title, "replaced_by": replacement}
                   for task_id, replacement in removals.items()]
        if not added and not updated and not removed:
            raise ActionConflict("The proposal needs a task change")
        identifier = f"pm-{uuid4().hex}"
        preview = {"id": identifier, "summary": change.summary, "added": added, "updated": updated, "removed": removed}
        proposal = PMPlanProposal(
            proposal_id=identifier, project_id=dag.project_id, session_id=request.session_id,
            fingerprint=self.manager.plan_fingerprint(dag), change=change, preview=preview,
        )
        write_atomic(self.pm_proposal_path, proposal.model_dump_json() + "\n")
        return preview

    def _apply_pm_plan(self, request: Request) -> dict[str, JsonValue]:
        dag = self._pm_plan_dag(request)
        approval = PMPlanApproval.model_validate_json(request.body or "")
        for receipt in dag.plan_edits:
            if (receipt.id, receipt.source, receipt.source_ref) == (approval.proposal_id, "PM_CHAT", request.session_id):
                return {"ok": True, "receipt": receipt.model_dump(mode="json")}
        proposal = self._read_pm_proposal(dag, request.session_id)
        if proposal is None or proposal.proposal_id != approval.proposal_id:
            raise ActionConflict("This proposal has changed. Review the newest proposal in the PM conversation")
        try:
            brief = self._binding(dag).brief
            updated = self.manager.commit_task_edit(
                dag.project_id, proposal.proposal_id, proposal.change.summary, "PM_CHAT",
                [task.to_task() for task in proposal.change.tasks], proposal.change.removal_map(),
                expected_fingerprint=proposal.fingerprint, source_ref=proposal.session_id,
                required_criteria=tuple(brief.criteria()) if brief else (),
            )
        except StateFormatError as error:
            raise ActionConflict(f"The project moved ahead. Ask the PM to update this proposal. {error}") from error
        receipt = next(receipt for receipt in updated.plan_edits if receipt.id == proposal.proposal_id)
        return {"ok": True, "receipt": receipt.model_dump(mode="json")}

    @staticmethod
    def _event_ref(event_id: str) -> str:
        return sha256(event_id.encode()).hexdigest()[:12]

    @staticmethod
    def _event_anchor(event: OutboxEvent) -> dict[str, JsonValue] | None:
        if not EVENT_ID.fullmatch(event.event_id) or (event.task_id and not CONTEXT_ID.fullmatch(event.task_id)):
            return None
        reasons = {OutboxEventType.PROJECT_ALERT: "FEEDBACK_FAILED", OutboxEventType.PROJECT_PREPARED: "PLAN_READY"}
        return {"event_id": event.event_id, "event_ref": AutoloopBridge._event_ref(event.event_id),
                "kind": event.type.value, "task_id": event.task_id,
                "reason_code": event.reason_code or reasons.get(event.type)}

    def _chief_context(self, request: Request) -> dict[str, JsonValue]:
        dag = self.manager.load()
        if not dag.tasks or not CONTEXT_ID.fullmatch(dag.project_id):
            return {"project": None, "recent_event": None, "recent_issue": None, "pending": []}
        self._check_active_chief(dag, request)
        tasks = {task.task_id: task.status for task in dag.tasks if CONTEXT_ID.fullmatch(task.task_id)}
        cards = self._recent_card_anchors(dag)
        _, issue = self._preferred_issue(cards)
        pending = []
        pending_tasks = set()
        for event, anchor in cards:
            if event.type == OutboxEventType.PROJECT_COMPLETED or event.task_id in pending_tasks:
                continue
            pending.append(anchor)
            pending_tasks.add(event.task_id)
            if len(pending) >= CONTEXT_TASK_LIMIT:
                break
        return {
            "project": {
                "id": dag.project_id, "status": dag.project_status.value,
                "done": sum(task.status == TaskStatus.COMPLETED for task in dag.tasks), "total": len(dag.tasks),
                "running": [task_id for task_id, status in tasks.items() if status == TaskStatus.RUNNING][:CONTEXT_TASK_LIMIT],
                "blocked": [task_id for task_id, status in tasks.items()
                            if status in (TaskStatus.BLOCKED, TaskStatus.FAILED)][:CONTEXT_TASK_LIMIT],
            },
            "recent_event": cards[0][1] if cards else None,
            "recent_issue": issue,
            "pending": pending,
        }

    def _recent_anchors(self, events: list[OutboxEvent]) -> Iterator[tuple[OutboxEvent, dict[str, JsonValue]]]:
        for event in reversed(events):
            if (anchor := self._event_anchor(event)) is not None:
                yield event, anchor

    def _recent_card_anchors(self, dag: TaskDAG) -> list[tuple[OutboxEvent, dict[str, JsonValue]]]:
        events = self.bus.outbox(dag.project_id)
        visible = self._current_notices(dag, events)
        token = self.bus.start_token(dag) if dag.project_status == ProjectStatus.IDLE else None
        candidates = [event for event in events if event.event_id in visible or (
            token is not None and event.type == OutboxEventType.PROJECT_PREPARED
            and event.plan is not None and event.plan.approval_token == token
        )]
        return list(self._recent_anchors(candidates))

    @staticmethod
    def _preferred_issue(cards: list[tuple[OutboxEvent, dict[str, JsonValue]]]) -> tuple[OutboxEvent | None, dict[str, JsonValue] | None]:
        return next(((event, anchor) for event, anchor in cards
                     if event.type in (OutboxEventType.ALERT_ESCALATED, OutboxEventType.PROJECT_ALERT)), (None, None))

    @staticmethod
    def _delivery_key(project_id: str, event_id: str) -> str:
        return f"{project_id}/{event_id}"

    def _check_archived_chief(self, project_id: str, request: Request) -> TaskDAG:
        try:
            project = read_model(self._history_file(project_id), TaskDAG)
        except FileNotFoundError:
            raise ProjectScopeDenied("This Chief conversation does not own the project") from None
        if project.project_id != project_id or self._binding(project).session_id not in request.chief_session_ids:
            raise ProjectScopeDenied("This Chief conversation does not own the project")
        return project

    @staticmethod
    def _current_completion(dag: TaskDAG, event: OutboxEvent) -> bool:
        return (event.type == OutboxEventType.PROJECT_COMPLETED and event.event_id == dag.completion_event_id
                and dag.project_status == ProjectStatus.COMPLETED and event.report is not None
                and event.report.delivery_verified is True)

    def _current_notices(self, dag: TaskDAG, events: list[OutboxEvent]) -> dict[str, OutboxEvent]:
        inbox = self.bus.inbox(dag.project_id)
        feedback = {item.event_id: item for item in inbox
                    if item.type == InboxEventType.USER_FEEDBACK and item.superseded_by is None and item.deleted_at is None}
        pending_feedback = any(not item.consumed for item in feedback.values())
        pending_actions = {item.action_ref for item in inbox if not item.consumed}
        tasks = {task.task_id: task for task in dag.tasks}
        feedback_failure = next((event for event in reversed(events) if event.type == OutboxEventType.PROJECT_ALERT
                                 and (issue := feedback.get(event.event_id.removeprefix("feedback-error-"))) is not None
                                 and issue.consumed and issue.event_id not in dag.applied_feedback), None)
        result = {}
        for event in events:
            if event.event_id in pending_actions:
                continue
            if event.type == OutboxEventType.PROJECT_ALERT:
                current = event is feedback_failure
            elif event.type == OutboxEventType.ALERT_ESCALATED:
                task = tasks.get(event.task_id)
                if event.task_id is not None:
                    current = task is not None and task_escalation_id(task) == event.event_id
                elif event.reason_code == "ACCEPTANCE_GAP":
                    current = (dag.project_status != ProjectStatus.COMPLETED
                               and event.event_id == f"acceptance-gap-{dag.completion_event_id}")
                else:
                    current = (dag.project_status == ProjectStatus.ALERT and dag.budget_alert is not None
                               and dag.budget_alert.event_id == event.event_id)
            else:
                current = not pending_feedback and self._current_completion(dag, event)
            if current:
                result[event.event_id] = event
        return result

    def _available_artifacts(self, dag: TaskDAG, event: OutboxEvent) -> list[dict[str, JsonValue]]:
        if event.report is None:
            return []
        directory = self.manager.project_directory(dag.project_id)
        artifacts = []
        for artifact in event.report.artifacts[:8]:
            relative = PurePosixPath(artifact.path)
            if relative.is_absolute() or not relative.parts or any(part.startswith(".") for part in relative.parts):
                continue
            path = directory / relative
            try:
                if path.resolve(strict=True) == path and path.is_file():
                    artifacts.append(artifact.model_dump(mode="json"))
            except (OSError, RuntimeError):
                continue
        return artifacts

    def _delivery_facts(self, dag: TaskDAG, event: OutboxEvent) -> dict[str, JsonValue]:
        binding = self._binding(dag)
        report = event.report
        task = next((task for task in dag.tasks if task.task_id == event.task_id), None)
        return {
            "kind": event.type.value, "project_title": (dag.project_name or binding.name or dag.project_id)[:140],
            "goal": binding.brief.user_instruction[:280] if binding.brief else None,
            "summary": " ".join(event.payload.summary.split())[:500],
            "reason_code": event.reason_code,
            "task_title": task.title[:140] if task else None, "task_id": event.task_id,
            "report": {
                "done": report.done, "total": report.total, "artifacts": self._available_artifacts(dag, event),
            } if report else None,
        }

    @staticmethod
    def _delivery_request_id(key: str, attempt: int) -> str:
        return f"autoloop-notify:{sha256(key.encode()).hexdigest()[:32]}:{attempt}"

    def _write_native_delivery(
        self, request: Request, command: DeliveryCommand, dag: TaskDAG, candidate: OutboxEvent | None,
        state: ChiefDeliveries, key: str,
    ) -> dict[str, JsonValue]:
        prior = state.deliveries.get(key)
        if prior is not None and prior.session_id not in request.chief_session_ids:
            raise ProjectScopeDenied("This notification is pinned to another Chief conversation")

        if isinstance(command, ClaimDelivery):
            if prior is not None:
                return {"delivery": prior.model_dump(mode="json"), "claimed": False}
            if candidate is None:
                return {"delivery": None, "claimed": False}
            delivery = NativeDelivery(
                project_id=dag.project_id, event_id=candidate.event_id, session_id=request.session_id,
                request_id=self._delivery_request_id(key, 1), start_seq=command.start_seq, started_at=time.time(),
                facts=self._delivery_facts(dag, candidate),
            )
            state.deliveries[key] = delivery
            self._save_deliveries(state)
            return {"delivery": delivery.model_dump(mode="json"), "claimed": True}

        if prior is None or prior.request_id != command.request_id:
            raise ActionConflict("The Chief notification attempt changed")
        if isinstance(command, RetryDelivery):
            if prior.status == "SENDING":
                return {"delivery": prior.model_dump(mode="json")}
            if prior.status != "NEEDS_ATTENTION":
                raise ActionConflict("This Chief notification does not need a retry")
            if prior.retryable:
                prior.attempt += 1
                prior.request_id = self._delivery_request_id(key, prior.attempt)
                if command.start_seq is not None:
                    prior.session_id = request.session_id
                    prior.start_seq = command.start_seq
            prior.status = "SENDING"
            prior.started_at = time.time()
            prior.error = None
            prior.retryable = False
        elif isinstance(command, FinishDelivery):
            if command.message_seq < prior.start_seq:
                raise ActionConflict("The Chief message precedes this notification request")
            if prior.status == "DELIVERED":
                if (prior.message_seq, prior.turn) != (command.message_seq, command.turn):
                    raise ActionConflict("The Chief notification already has a different delivery receipt")
                return {"delivery": prior.model_dump(mode="json")}
            if prior.status not in ("SENDING", "NEEDS_ATTENTION"):
                raise ActionConflict("This Chief notification is no longer pending")
            prior.status = "DELIVERED"
            prior.message_seq, prior.turn, prior.text = command.message_seq, command.turn, command.text
            prior.delivered_at, prior.error, prior.retryable = time.time(), None, False
        elif prior.status == "SENDING":
            prior.status, prior.error, prior.retryable = "NEEDS_ATTENTION", command.error, command.retryable
        else:
            return {"delivery": prior.model_dump(mode="json")}

        self._save_deliveries(state)
        return {"delivery": prior.model_dump(mode="json")}

    def _write_delivery(self, request: Request) -> dict[str, JsonValue]:
        command = delivery_request.validate_json(request.body or "")
        if request.project_id is None or request.event_id is None:
            raise ActionConflict("This notification requires the exact project and event")
        dag = self.manager.load()
        candidate = None
        if request.project_id == dag.project_id:
            self._check_active_chief(dag, request)
            if isinstance(command, ClaimDelivery):
                candidate = self._current_notices(dag, self.bus.outbox(dag.project_id)).get(request.event_id)
        else:
            dag = self._check_archived_chief(request.project_id, request)
            if isinstance(command, ClaimDelivery):
                candidate = next((event for event in self.bus.outbox(dag.project_id)
                                  if event.event_id == request.event_id and self._current_completion(dag, event)), None)
        key = self._delivery_key(request.project_id, request.event_id)
        return self._write_native_delivery(request, command, dag, candidate, self._deliveries(), key)

    def _details(self, request: Request) -> tuple[int, dict[str, JsonValue]]:
        dag = self.manager.load()
        project_id = request.project_id or dag.project_id
        current_project = project_id == dag.project_id
        if current_project:
            if not dag.tasks:
                return 404, {"error": "No project to inspect"}
            self._check_active_chief(dag, request)
        else:
            try:
                dag = read_model(self._history_file(project_id), TaskDAG)
            except FileNotFoundError:
                return 404, {"error": "Project not found"}
            if dag.project_id != project_id or self._binding(dag).session_id not in request.chief_session_ids:
                raise ProjectScopeDenied("The project belongs to another Chief session")
        event, anchor = None, None
        if request.event_id is not None:
            matches = [event for event in self.bus.outbox(project_id)
                       if request.event_id in (event.event_id, self._event_ref(event.event_id))]
            if len(matches) != 1 or (anchor := self._event_anchor(matches[0])) is None:
                return 404, {"error": "Project event not found"}
            event = matches[0]
        elif request.task_id is None:
            if current_project:
                cards = self._recent_card_anchors(dag)
                event, anchor = self._preferred_issue(cards)
                if event is None and cards:
                    event, anchor = cards[0]
            else:
                event, anchor = next(self._recent_anchors(self.bus.outbox(project_id)), (None, None))
        task_id = request.task_id or (event.task_id if event is not None else None)
        if event is not None and event.task_id is not None and event.task_id != task_id:
            return 404, {"error": "The event does not belong to the selected task"}
        task = next((node for node in dag.tasks if node.task_id == task_id), None) if task_id is not None else None
        if task_id is not None and task is None and event is None:
            return 404, {"error": "Project task not found"}
        result: dict[str, JsonValue] = {
            "project_id": project_id, "project_status": dag.project_status.value,
            "done": sum(task.status == TaskStatus.COMPLETED for task in dag.tasks), "total": len(dag.tasks),
        }
        if event is not None:
            result["event"] = {**anchor, "timestamp": event.timestamp}
            if event.report is not None:
                result["report"] = {
                    "done": event.report.done, "total": event.report.total, "unverified": event.report.unverified,
                    "outcomes": [{"title": outcome.title[:120], "summary": outcome.summary[:360]}
                                 for outcome in event.report.outcomes[:3]],
                    "artifacts": [file.path[:240] for file in event.report.artifacts[:8]],
                    "delivery_verified": event.report.delivery_verified,
                    "acceptance_checks": [check.model_dump(mode="json") for check in event.report.acceptance_checks[:8]],
                }
            if event.plan is not None:
                result["plan"] = {"mode": event.plan.mode, "task_count": event.plan.task_count}
        if task is not None:
            result["task"] = self._task_details(project_id, task)
        elif task_id is not None:
            result["task"] = None
            result["task_unavailable"] = True
        if request.include_logs:
            error = event.payload.summary if event is not None else (task.error_msg if task is not None else None)
            public = next((run.detail for run in reversed(task.worker_runs) if run.detail), None) if task is not None else None
            error_tail, error_cut = self._evidence_tail(error)
            public_tail, public_cut = self._evidence_tail(public if public != error else None)
            result["logs"] = {"error": error_tail, "latest_public": public_tail, "tail_truncated": error_cut or public_cut}
        return 200, result

    def _task_details(self, project_id: str, task: Task) -> dict[str, JsonValue]:
        root = self.manager.project_directory(project_id)
        artifacts = []
        for raw in task.output_artifacts:
            path = root / raw
            try:
                relative = path.relative_to(root)
                if not relative.parts or any(part.startswith(".") for part in relative.parts):
                    continue
                if path.resolve(strict=True) == path and path.is_file():
                    artifacts.append(relative.as_posix())
                    if len(artifacts) >= 8:
                        break
            except (ValueError, OSError, RuntimeError):
                continue
        return {"task_id": task.task_id, "title": task.title[:120], "status": task.status.value,
                "retry_count": task.retry_count, "artifacts": artifacts,
                "last_action": task.last_action.model_dump(mode="json") if task.last_action is not None else None}

    @staticmethod
    def _evidence_tail(value: str | None) -> tuple[str | None, bool]:
        if not value:
            return None, False
        tail = "\n".join(value[-EVIDENCE_TAIL_CHARS:].splitlines()[-EVIDENCE_TAIL_LINES:])
        return tail, tail != value.strip()

    def _feedback(self, dag: TaskDAG, issue: str, request_id: str | None, replace_plan: bool = False) -> str:
        if not dag.tasks:
            raise ActionConflict("There is no project plan to revise yet")
        if replace_plan and dag.project_status != ProjectStatus.IDLE:
            raise ActionConflict("Full plan replacement is available while the project is awaiting confirmation")
        events = self.bus.inbox()
        if dag.project_status == ProjectStatus.IDLE and any(
            event.type == InboxEventType.USER_COMMAND and not event.consumed
            and event.project_id == dag.project_id for event in events
        ):
            raise ActionConflict("A project action is already queued")
        key = sha256(f"{dag.project_id}:{request_id}".encode()).hexdigest() if request_id else None
        event = make_inbox(
            InboxEventType.USER_FEEDBACK, summary=issue, project_id=dag.project_id,
            event_id=f"issue-{key}" if key else None,
        )
        if not replace_plan:
            self.bus._append_inbox(event)
            return event.event_id

        event = event.model_copy(update={"replace_plan": True})
        if existing := next((item for item in events if item.event_id == event.event_id), None):
            if existing.replace_plan and existing.project_id == dag.project_id and existing.payload.summary == issue:
                return existing.event_id
            raise ActionConflict("This feedback request ID is already in use")

        for prior in events:
            if (prior.type == InboxEventType.USER_FEEDBACK and prior.project_id == dag.project_id
                    and prior.superseded_by is None and prior.deleted_at is None):
                prior.superseded_by = event.event_id
                prior.consumed = True
        self.bus.replace_inbox([*events, event])
        return event.event_id

    def _history_file(self, project_id: str) -> Path:
        return self.history / f"{sha256(project_id.encode()).hexdigest()}.json"

    def archive_project(self, dag: TaskDAG) -> None:
        self.history.mkdir(mode=0o700, exist_ok=True)
        write_atomic(self._history_file(dag.project_id), dag.model_dump_json() + "\n")

    def _feedback_failures(self, project_id: str) -> dict[str, OutboxEvent]:
        return {event.event_id.removeprefix("feedback-error-"): event for event in self.bus.outbox(project_id)
                if event.type == OutboxEventType.PROJECT_ALERT and event.event_id.startswith("feedback-error-")}

    def _delete_feedback(self, dag: TaskDAG, issue_id: str) -> dict[str, JsonValue]:
        rows = self.bus.inbox()
        issue = next((event for event in rows if event.event_id == issue_id and event.project_id == dag.project_id
                      and event.type == InboxEventType.USER_FEEDBACK), None)
        if issue is None:
            raise ActionConflict("This Issue is no longer in the project inbox")
        if issue.deleted_at is None:
            if issue.event_id in dag.applied_feedback:
                raise ActionConflict("PM already applied this Issue to the plan")
            if issue.superseded_by is not None:
                raise ActionConflict("This Issue has been replaced. Refresh the inbox and use the current card")
            issue.consumed = True
            issue.deleted_at = int(time.time())
            self.bus.replace_inbox(rows)
        return {"ok": True, "event_id": issue_id, "project_id": dag.project_id}

    def _retry_feedback(self, dag: TaskDAG, failure_id: str) -> dict[str, JsonValue]:
        issue_id = failure_id.removeprefix("feedback-error-")
        failure = self._feedback_failures(dag.project_id).get(issue_id)
        rows = self.bus.inbox()
        prior = next((event for event in rows if event.event_id == issue_id and event.project_id == dag.project_id
                      and event.type == InboxEventType.USER_FEEDBACK), None)
        if (not failure or failure.event_id != failure_id or not prior or not prior.consumed
                or prior.deleted_at is not None or issue_id in dag.applied_feedback):
            raise ActionConflict("The feedback is no longer waiting for recovery")
        if prior.superseded_by:
            retry = next((event for event in rows if event.event_id == prior.superseded_by
                          and event.action_ref == issue_id and event.deleted_at is None), None)
            if retry is None:
                raise ActionConflict("The feedback has been replaced")
            return {"ok": True, "event_id": retry.event_id, "project_id": dag.project_id}
        if prior.replace_plan and dag.project_status != ProjectStatus.IDLE:
            raise ActionConflict("The project has already started. Discuss changes with the project PM")
        retry = make_inbox(
            InboxEventType.USER_FEEDBACK, project_id=dag.project_id, summary=prior.payload.summary,
            replace_plan=prior.replace_plan, action_ref=issue_id,
        )
        prior.superseded_by = retry.event_id
        self.bus.replace_inbox([*rows, retry])
        return {"ok": True, "event_id": retry.event_id, "project_id": dag.project_id}

    def _feedback_view(self, dag: TaskDAG, event: InboxEvent, failures: dict[str, OutboxEvent]) -> dict[str, JsonValue]:
        if event.deleted_at is not None:
            return {"status": "DELETED", "error": None}
        failure = failures.get(event.event_id)
        error = failure.payload.summary if failure else None
        if event.superseded_by is not None:
            return {"status": "SUPERSEDED", "error": error}
        if event.event_id in dag.applied_feedback:
            return {"status": "APPLIED", "error": None}
        if event.consumed:
            retry_event_id = failure.event_id if failure and (
                not event.replace_plan or dag.project_status == ProjectStatus.IDLE
            ) else None
            return {"status": "FAILED", "error": error, "retry_event_id": retry_event_id}
        return {"status": "PROCESSING" if event.event_id in self.daemon.feedback_event_ids else "QUEUED", "error": None}

    def _review(self, dag: TaskDAG) -> tuple[str | None, dict[str, JsonValue] | None]:
        if dag.project_status != ProjectStatus.IDLE or not dag.tasks:
            return None, None
        feedback = self.bus.feedback(dag.project_id)
        token = self.bus.start_token(dag, feedback)
        if not feedback:
            return token, None
        pending = [event for event in feedback if not event.consumed]
        if pending:
            running = [event for event in pending if event.event_id in self.daemon.feedback_event_ids]
            batch = running or pending
            return None, {
                "status": "UPDATING" if running else "QUEUED", "issue": batch[-1].payload.summary,
                "count": len(batch), "error": None,
            }
        latest = feedback[-1]
        result = self._feedback_view(dag, latest, self._feedback_failures(dag.project_id))
        return token, {**result, "issue": latest.payload.summary}

    def _plan(self, name: str, dag: TaskDAG, *, live: bool) -> dict[str, JsonValue]:
        token, revision = self._review(dag) if live else (None, None)
        source_ids = {source for task in dag.tasks for source in task.source_issue_ids}
        sources = [
            {"id": event.event_id, "summary": event.payload.summary}
            for event in self.bus.inbox()
            if event.type == InboxEventType.USER_FEEDBACK and event.project_id == dag.project_id and event.event_id in source_ids
        ] if source_ids else []
        return {
            "id": dag.project_id, "name": name, "goal_tree": dag.goal_tree,
            "tasks": [task.model_dump(mode="json", include={
                "task_id", "title", "status", "dependencies", "acceptance", "permission_tier", "phase", "source_issue_ids",
            }) for task in dag.tasks],
            "approval_token": token, "revision": revision, "sources": sources,
            "editable_task_ids": [task.task_id for task in dag.tasks if live and self.manager.task_is_editable(task)],
            "edits": [edit.model_dump(mode="json", exclude={"source_ref"}) for edit in dag.plan_edits[-10:]],
        }

    def _describe(self, name: str, dag: TaskDAG, pm_name: str | None, *, live: bool = False) -> dict[str, JsonValue]:
        done = sum(task.status == TaskStatus.COMPLETED for task in dag.tasks)
        blocked = sum(task.status in (TaskStatus.BLOCKED, TaskStatus.FAILED) for task in dag.tasks)
        return {
            "id": dag.project_id, "name": name, "pm_name": pm_name, "status": dag.project_status.value,
            "total": len(dag.tasks), "done": done, "blocked": blocked,
            "pending_verify": sum(task.status == TaskStatus.PENDING_VERIFY for task in dag.tasks),
            "static_preview": self._has_static_preview(dag),
            "work_dir": str(self.manager.project_directory(dag.project_id)),
            "plan": self._plan(name, dag, live=live),
        }

    def _pm_status(self, request: Request) -> tuple[int, dict[str, JsonValue]]:
        project_id = request.project_id
        if not request.session_id or not project_id:
            raise ProjectScopeDenied("Read status from an assigned project PM conversation")

        dag = self.manager.load()
        live = bool(dag.tasks) and dag.project_id == project_id
        if live:
            name = dag.project_name or self._binding(dag).name or project_id
        else:
            draft = self.read_draft(request.chief_session_ids, project_id)
            if draft is not None:
                return 200, {"project": None, "plan": None, "inbox": [], "draft": {
                    "project_id": project_id, "title": draft["title"], "status": draft["status"],
                    "error": str(draft["error"])[:PM_STATUS_EXCERPT_CHARS] if draft["error"] else None,
                }}
            history_path = self._history_file(project_id)
            if not history_path.is_file():
                return 404, {"error": "This PM's assigned project is unavailable"}
            dag = read_model(history_path, TaskDAG)
            name = dag.project_name or self._binding(dag).name or project_id

        directory = self.manager.project_directory(project_id)
        project = {"id": project_id, "name": name, "status": dag.project_status.value,
                   "total": len(dag.tasks), "done": sum(task.status == TaskStatus.COMPLETED for task in dag.tasks),
                   "work_dir": str(directory), "static_preview": self._has_static_preview(dag)}
        plan = {"goal_tree": dag.goal_tree, "tasks": pm_task_context(dag, directory),
                "editable_task_ids": [task.task_id for task in dag.tasks if live and self.manager.task_is_editable(task)],
                "amendable_task_ids": [task.task_id for task in dag.tasks
                                       if live and self.manager.task_is_amendable(task)]}
        if live and (brief := self._binding(dag).brief) is not None:
            plan["brief"] = brief.planning_data()
        return 200, {"project": project, "plan": plan, "draft": None,
                     "inbox": self._pm_status_inbox(dag) if live else []}

    def _pm_status_inbox(self, dag: TaskDAG) -> list[dict[str, JsonValue]]:
        feedback = self.bus.feedback(dag.project_id)
        failures = self._feedback_failures(dag.project_id) if feedback else {}
        entries = []
        failed_count = 0
        for event in sorted(feedback, key=lambda item: item.timestamp, reverse=True):
            view = self._feedback_view(dag, event, failures)
            status = view["status"]
            if status in ("QUEUED", "PROCESSING"):
                entries.append({"event_id": event.event_id, "status": status, "issue": event.payload.summary,
                                "replace_plan": event.replace_plan})
            elif status == "FAILED" and failed_count < PM_STATUS_FAILED_LIMIT:
                error = view["error"]
                entries.append({"event_id": event.event_id, "status": status,
                                "issue_excerpt": event.payload.summary[:PM_STATUS_EXCERPT_CHARS],
                                "error": error[:PM_STATUS_EXCERPT_CHARS] if isinstance(error, str) else None})
                failed_count += 1
        return entries

    def _panel(self, request: Request) -> dict[str, JsonValue]:
        dag = self.manager.load()
        project_id = request.project_id
        draft = self.read_draft(request.chief_session_ids, project_id)
        projects = []
        live = bool(dag.tasks) and (project_id is None or project_id == dag.project_id)
        if live:
            binding = self._binding(dag)
            projects.append(self._describe(dag.project_name or binding.name or dag.project_id, dag, binding.pm_name, live=True))

        if project_id is None:
            for record in self._archived_projects():
                if record.project_id != dag.project_id:
                    binding = self._binding(record)
                    projects.append(self._describe(record.project_name or binding.name or record.project_id, record, binding.pm_name))
        elif not live:
            file = self._history_file(project_id)
            if file.is_file():
                dag = read_model(file, TaskDAG)
                binding = self._binding(dag)
                projects.append(self._describe(dag.project_name or binding.name or dag.project_id, dag, binding.pm_name))
            else:
                dag = TaskDAG(project_id=project_id, project_status=ProjectStatus.IDLE, tasks=[])

        snapshot = self._snapshot(dag, live=live)
        inbox = []
        if dag.tasks:
            events = self.bus.inbox(dag.project_id)
            failures = self._feedback_failures(dag.project_id)
            for event in sorted(events, key=lambda event: event.timestamp, reverse=True):
                feedback = self._feedback_view(dag, event, failures) if event.type == InboxEventType.USER_FEEDBACK else None
                if event.consumed and (feedback is None or feedback["status"] != "FAILED"):
                    continue
                inbox.append({**event.model_dump(mode="json"), "feedback": feedback})
        attention = []
        if project_id is None and request.chief_session_ids:
            titles = {project["id"]: project["name"] for project in projects}
            attention = [{"project_id": delivery.project_id, "event_id": delivery.event_id, "request_id": delivery.request_id,
                          "project": titles[delivery.project_id], "error": delivery.error}
                         for delivery in self._deliveries().deliveries.values()
                         if delivery.status == "NEEDS_ATTENTION" and delivery.session_id in request.chief_session_ids
                         and delivery.project_id in titles]
        return {"snapshot": snapshot, "projects": projects, "inbox": inbox, "draft": draft, "live": live,
                "notification_attention": attention}

    def _archived_projects(self) -> Iterator[TaskDAG]:
        for path in sorted(self.history.glob("*.json"), key=lambda file: file.stat().st_mtime, reverse=True):
            yield read_model(path, TaskDAG)

    def _files(self, request: Request) -> tuple[int, dict[str, JsonValue]]:
        dag = self.manager.load()
        project_id = request.project_id or dag.project_id
        if not dag.tasks or (project_id != dag.project_id and not self._history_file(project_id).is_file()):
            return 404, {"error": "Project folder not available"}
        relative = PurePosixPath(request.file_path or ".")
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts or "\x00" in str(relative):
            return 400, {"error": "Invalid project path"}
        root = self.manager.project_directory(project_id)
        path = root.joinpath(*relative.parts)
        if path.resolve() != path:
            return 400, {"error": "Project links cannot be opened"}
        if path.is_dir():
            entries = []
            for item in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.casefold())):
                if item.name in (".git", ".DS_Store") or item.is_symlink():
                    continue
                if item.is_dir() or item.is_file():
                    entries.append({"name": item.name, "path": item.relative_to(root).as_posix(), "directory": item.is_dir()})
            return 200, {"kind": "directory", "entries": entries[:250], "truncated": len(entries) > 250, "path": str(relative)}
        if not path.is_file():
            return 404, {"error": "Project file not found"}
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return 415, {"error": "File preview unavailable"}
            contents = stream.read(MAX_PREVIEW_BYTES + 1)
        try:
            text = codecs.getincrementaldecoder("utf-8")().decode(contents[:MAX_PREVIEW_BYTES], final=len(contents) <= MAX_PREVIEW_BYTES)
        except UnicodeDecodeError:
            return 415, {"error": "File preview is available for UTF-8 text"}
        if "\x00" in text:
            return 415, {"error": "File preview is available for text"}
        return 200, {"kind": "file", "text": text, "truncated": len(contents) > MAX_PREVIEW_BYTES, "path": str(relative)}

    def _notices(self) -> dict[str, JsonValue]:
        dag = self.manager.load()
        state = self._deliveries()
        by_project: dict[str, list[OutboxEvent]] = {}
        for event in self.bus.outbox():
            by_project.setdefault(event.project_id, []).append(event)
        history = self._recent_achievements(dag.project_id, by_project, state)
        if not dag.tasks:
            return {"notices": [], "history": history}
        events = by_project.get(dag.project_id, [])
        current = self._current_notices(dag, events)
        tasks = {task.task_id: task for task in dag.tasks}
        entries = []
        for event in events:
            active = event.event_id in current
            key = self._delivery_key(dag.project_id, event.event_id)
            delivery = state.deliveries.get(key)
            task = tasks.get(event.task_id)
            applied = task is not None and task.last_action is not None and task.last_action.target_event_id == event.event_id
            if active or delivery is not None or applied:
                entries.append(self._notice(dag, event, task, active, delivery))
        return {"notices": entries, "project": self._notice_project(dag), "history": history}

    def _notice(
        self, dag: TaskDAG, event: OutboxEvent, task: Task | None, active: bool, delivery: NativeDelivery | None,
        *, unclaimed: bool = False,
    ) -> dict[str, JsonValue]:
        receipt = task.last_action if task is not None else None
        actions = []
        if active and task is not None and self._can_retry(task, event):
            actions = ["RETRY"]
        elif active and event.type == OutboxEventType.PROJECT_ALERT:
            actions = ["RETRY_FEEDBACK"]
        return {
            "id": event.event_id, "event_ref": self._event_ref(event.event_id),
            "type": event.type.value, "summary": event.payload.summary, "project_id": dag.project_id, "task_id": event.task_id,
            "timestamp": event.timestamp, "task": task.title if task else None, "active": active,
            "reason_code": event.reason_code,
            "native_delivery_pending": (active or unclaimed) if delivery is None else delivery.status == "SENDING",
            "actions": actions, "confirm_worker_stopped": event.reason_code == "WORKER_INTERRUPTED",
            **({"delivery": delivery.model_dump(mode="json", exclude={"project_id", "event_id", "text"})} if delivery is not None else {}),
            **({"receipt": receipt.model_dump(mode="json")} if receipt is not None and receipt.target_event_id == event.event_id else {}),
            **({"report": event.report.model_dump(mode="json")} if event.report is not None else {}),
        }

    def _notice_project(self, dag: TaskDAG) -> dict[str, JsonValue]:
        binding = self._binding(dag)
        return {
            "id": dag.project_id, "name": dag.project_name or binding.name or dag.project_id, "chief_session": binding.session_id,
            "total": len(dag.tasks), "done": sum(task.status == TaskStatus.COMPLETED for task in dag.tasks),
            "static_preview": self._has_static_preview(dag),
        }

    def _recent_achievements(
        self, active_project: str, events: dict[str, list[OutboxEvent]], state: ChiefDeliveries,
    ) -> list[JsonValue]:
        history = []
        recent_count = 0
        for dag in self._archived_projects():
            if dag.project_id == active_project:
                continue
            entries = []
            for event in events.get(dag.project_id, []):
                delivery = state.deliveries.get(self._delivery_key(dag.project_id, event.event_id))
                complete = self._current_completion(dag, event)
                pending = delivery is not None and delivery.status != "DELIVERED"
                if not pending and not (complete and (delivery is None or recent_count < 3)):
                    continue
                if complete:
                    recent_count += 1
                entries.append(self._notice(dag, event, None, False, delivery, unclaimed=complete))
            if entries:
                history.append({"project": self._notice_project(dag), "notices": entries})
        return history

    def _outbox(self, request: Request) -> dict[str, JsonValue]:
        if request.project_id is not None:
            return {"active": False, "pm": self._pm_chat(request)}
        draft = self.read_draft(request.chief_session_ids, None)
        if draft is not None:
            return {"active": False, "draft": draft}
        dag = self.manager.load()
        if not dag.tasks:
            return {"active": False}
        binding = self._binding(dag)
        if binding.session_id not in request.chief_session_ids:
            return {"active": False}
        pending = [event.task_id for event in self.bus.inbox(dag.project_id)
                   if not event.consumed and event.type in (InboxEventType.TASK_RETRY, InboxEventType.USER_COMMAND)]
        return {
            "active": True, "snapshot": self._snapshot(dag), "queued": pending,
            "plan": self._plan(dag.project_name or binding.name or dag.project_id, dag, live=True),
        }

    def _pm_chat(self, request: Request) -> dict[str, JsonValue] | None:
        draft = self.read_draft(request.chief_session_ids, request.project_id)
        if draft is not None:
            status = {"QUEUED": "QUEUED", "PLANNING": "RUNNING", "FAILED": "FAILED"}[draft["status"]]
            return {"name": draft["title"], "draft": draft, "plan": None, "proposal": None,
                    "activity": self._pm_activity(status, draft.get("progress"), draft["error"])}

        panel = self._panel(request)
        if not panel["projects"]:
            return None
        project = panel["projects"][0]
        plan = project["plan"]
        progress = self.daemon.planning_progress(project["id"]) if panel["live"] else None
        visible = progress.model_dump(mode="json") if progress else None
        status, error = "READY", None
        if revision := plan["revision"]:
            status = {"QUEUED": "QUEUED", "UPDATING": "RUNNING", "APPLIED": "READY", "FAILED": "FAILED"}[revision["status"]]
            error = revision["error"]
        else:
            next_issue = next((entry["feedback"]["status"] for entry in panel["inbox"]
                               if entry["feedback"] and entry["feedback"]["status"] in ("QUEUED", "PROCESSING")), None)
            if next_issue:
                status = "RUNNING" if next_issue == "PROCESSING" else "QUEUED"
        if status == "FAILED":
            failed = next((entry for entry in panel["inbox"] if entry["feedback"] and entry["feedback"]["status"] == "FAILED"), None)
            failure = self._feedback_failures(project["id"]).get(failed["event_id"]) if failed else None
            if failure and failure.progress:
                visible = failure.progress.model_dump(mode="json")
        proposal = None
        if panel["live"]:
            dag = self.manager.load()
            pending = self._read_pm_proposal(dag, request.session_id)
            if (pending and all(receipt.id != pending.proposal_id for receipt in dag.plan_edits)
                    and self._pm_proposal_is_current(dag, pending)):
                proposal = pending.preview
        chat_plan = None if project["status"] == ProjectStatus.COMPLETED.value else plan
        return {"name": project["name"], "draft": None, "plan": chat_plan, "proposal": proposal,
                "activity": self._pm_activity(status, visible, error)}

    @staticmethod
    def _pm_activity(status: str, progress: JsonValue, error: JsonValue) -> dict[str, JsonValue]:
        started_at = progress.get("started_at") if isinstance(progress, dict) else None
        return {"status": status, "started_at": started_at,
                "public_text": progress.get("preview") if isinstance(progress, dict) else None, "error": error}

    def _enqueue(self, dag: TaskDAG, command: Action, expected_event: str | None) -> dict[str, JsonValue]:
        action, task_id = command.action, command.task_id
        if command.confirm_worker_stopped and (action != UIActionType.RETRY or not expected_event):
            raise ActionConflict("Confirmation must refer to the interrupted worker alert")
        action_ref = None
        if action in PROJECT_ACTIONS:
            if task_id is not None or not dag.tasks or dag.project_status not in PROJECT_ACTIONS[action]:
                raise ActionConflict(f"{action} is unavailable in project state {dag.project_status}")
            if action == UIActionType.START_PROJECT:
                token, revision = self._review(dag)
                if token is None:
                    if revision and revision["status"] == "FAILED":
                        raise ActionConflict("A replacement plan must be ready before confirming this project")
                    raise ActionConflict("PM feedback is queued or in progress. Review the next plan before starting")
                if expected_event != token:
                    raise ActionConflict("The project plan has changed. Review the latest version before starting")
                action_ref = token
            kind, summary = InboxEventType.USER_COMMAND, action.value
        else:
            task = next((node for node in dag.tasks if node.task_id == task_id), None)
            if task is None:
                raise ActionConflict(f"Unknown task {task_id!r}")
            if expected_event is None:
                raise ActionConflict("Retry requires the exact failure event")
            if receipt := self._retry_receipt(dag, task, expected_event):
                return receipt
            alert = self.bus._latest_failure(dag.project_id, task.task_id)
            if alert is None or expected_event not in (alert.event_id, self._event_ref(alert.event_id)):
                raise ActionConflict("The task failure has changed")
            if not self._can_retry(task, alert):
                raise ActionConflict("Resolve the blocking condition before restarting this task")
            interrupted = task_retry_kind(task) == "INTERRUPTED"
            if interrupted and not command.confirm_worker_stopped:
                raise ActionConflict("Confirm the interrupted worker has stopped before retrying")
            if command.confirm_worker_stopped and not interrupted:
                raise ActionConflict("The interrupted worker alert has changed")
            action_ref = alert.event_id
            kind, summary = InboxEventType.TASK_RETRY, ""

        event = make_inbox(kind, task_id=task_id, summary=summary, project_id=dag.project_id,
                           action_ref=action_ref, confirm_worker_stopped=command.confirm_worker_stopped)
        for queued in self.bus.inbox(dag.project_id):
            if not queued.consumed and queued.task_id == task_id and queued.type in (
                InboxEventType.TASK_RETRY, InboxEventType.USER_COMMAND,
            ):
                if queued.type == kind and queued.payload.summary == summary and queued.action_ref == action_ref:
                    return self._queued_action(queued, action)
                raise ActionConflict("An action is already queued for this target")
        self.bus._append_inbox(event)
        return self._queued_action(event, action)

    def _retry_receipt(self, dag: TaskDAG, task: Task, expected_event: str) -> dict[str, JsonValue] | None:
        def matches(target: str | None) -> bool:
            return target is not None and expected_event in (target, self._event_ref(target))

        receipt = task.last_action
        if receipt is not None and receipt.action == "RETRY" and matches(receipt.target_event_id):
            return {
                "ok": True, "status": "APPLIED", "event_id": receipt.event_id,
                "project_id": dag.project_id, "task_id": task.task_id, "action": receipt.action,
                "target_event_id": receipt.target_event_id,
            }
        existing = next((event for event in self.bus.inbox(dag.project_id)
                         if not event.consumed and event.task_id == task.task_id
                         and event.type == InboxEventType.TASK_RETRY and matches(event.action_ref)), None)
        return self._queued_action(existing, UIActionType.RETRY) if existing else None

    @staticmethod
    def _queued_action(event: InboxEvent, action: UIActionType) -> dict[str, JsonValue]:
        return {"ok": True, "status": "QUEUED", "event_id": event.event_id, "project_id": event.project_id,
                "task_id": event.task_id, "action": action.value, "target_event_id": event.action_ref}
