from collections.abc import Iterable
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from config.constants import DEFAULT_BUDGET_USD, MAX_RETRY


class ProjectStatus(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ALERT = "ALERT"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    PENDING_VERIFY = "PENDING_VERIFY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class InboxEventType(StrEnum):
    USER_COMMAND = "USER_COMMAND"
    USER_FEEDBACK = "USER_FEEDBACK"
    TASK_EXECUTION_FINISHED = "TASK_EXECUTION_FINISHED"
    TASK_VERIFIED = "TASK_VERIFIED"
    TASK_REJECTED = "TASK_REJECTED"
    TASK_RETRY = "TASK_RETRY"


class OutboxEventType(StrEnum):
    PROJECT_ALERT = "PROJECT_ALERT"
    ALERT_ESCALATED = "ALERT_ESCALATED"
    PROJECT_PREPARED = "PROJECT_PREPARED"
    PROJECT_COMPLETED = "PROJECT_COMPLETED"


class UIActionType(StrEnum):
    START_PROJECT = "START_PROJECT"
    PAUSE_PROJECT = "PAUSE_PROJECT"
    RESUME_PROJECT = "RESUME_PROJECT"
    RETRY = "RETRY"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


def _freeze_list(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


NonEmptyText = Annotated[str, Field(min_length=1)]
PMName = Annotated[str, Field(min_length=1, max_length=24)]
NonnegativeCost = Annotated[float, Field(ge=0, allow_inf_nan=False)]
TaskPaths = Annotated[tuple[NonEmptyText, ...], BeforeValidator(_freeze_list)]
TaskPhase = Literal["RESEARCH", "DESIGN", "IMPLEMENT", "VERIFY", "RELEASE"]
PlanEditSource = Literal["PM_CHAT", "INBOX"]
UnixSeconds = Annotated[int, Field(ge=1, le=253402300799)]
BriefCriterion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
CriterionReferences = Annotated[tuple[Annotated[str, Field(pattern=r"^C[1-9][0-9]*$")], ...],
                                BeforeValidator(_freeze_list), Field(max_length=12)]


class PMTaskCheck(StrictModel):
    criterion_id: NonEmptyText
    status: Literal["PASS", "FAIL", "UNCLEAR"]
    evidence_refs: TaskPaths = ()
    reason: NonEmptyText


class PMTaskVerdict(StrictModel):
    verdict: Literal["VERIFIED", "REJECTED"]
    reason: NonEmptyText
    disposition: Literal["REWORK", "BLOCKED_PERMISSION", "BLOCKED_HUMAN"] | None = None
    checks: Annotated[tuple[PMTaskCheck, ...], BeforeValidator(_freeze_list), Field(min_length=1, max_length=13)]

    @model_validator(mode="after")
    def check_disposition(self) -> "PMTaskVerdict":
        passed = all(check.status == "PASS" for check in self.checks)
        if self.verdict == "VERIFIED" and (self.disposition is not None or not passed):
            raise ValueError("A verified task must pass every check and have no rejection disposition")
        if self.verdict == "VERIFIED" and any(not check.evidence_refs for check in self.checks):
            raise ValueError("A verified check must cite its evidence")
        if self.verdict == "REJECTED" and (self.disposition is None or passed):
            raise ValueError("A rejected task must identify an unmet check and its disposition")
        return self

    def require_criteria(self, criteria: Iterable[str]) -> None:
        expected = set(criteria)
        if len(self.checks) != len(expected) or {check.criterion_id for check in self.checks} != expected:
            raise ValueError("PM verdict must cover each assigned acceptance check exactly once")


class TaskRunRef(StrictModel):
    run_id: NonEmptyText
    task_revision: Annotated[int, Field(ge=1)]


class TaskAcceptanceReceipt(StrictModel):
    event_id: NonEmptyText
    verdict: PMTaskVerdict


class TaskExecutionFailure(StrictModel):
    reason_code: Literal["WORKER_FAILED", "WORKER_TIMEOUT", "WORKER_CLEANUP_FAILED", "WORKSPACE_ROLLBACK_FAILED", "BUDGET_EXHAUSTED"]
    retryable: bool


class TaskEscalation(StrictModel):
    reason_code: Literal[
        "BLOCKED_PERMISSION", "BLOCKED_HUMAN", "RETRY_EXHAUSTED", "WORKER_FAILED", "WORKER_TIMEOUT",
        "WORKER_CLEANUP_FAILED", "WORKSPACE_ROLLBACK_FAILED", "PM_ACCEPTANCE_FAILED", "WORKER_INTERRUPTED",
    ]
    summary: NonEmptyText


class WorkerRun(StrictModel):
    started_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    finished_at: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    status: Literal["RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"] = "RUNNING"
    detail: str = ""
    run_id: NonEmptyText
    execution_event_id: NonEmptyText | None = Field(default=None, exclude_if=lambda value: value is None)
    execution_checks: tuple[PMTaskCheck, ...] = Field(default=(), exclude_if=lambda value: not value)
    verification_attempts: Annotated[int, Field(ge=0, le=MAX_RETRY)] = Field(default=0, exclude_if=lambda value: value == 0)
    acceptance: TaskAcceptanceReceipt | None = Field(default=None, exclude_if=lambda value: value is None)
    escalation: TaskEscalation | None = Field(default=None, exclude_if=lambda value: value is None)


class TaskActionReceipt(StrictModel):
    event_id: NonEmptyText
    target_event_id: NonEmptyText
    action: Literal["RETRY"]


class Task(StrictModel):
    model_config = ConfigDict(frozen=True)

    task_id: NonEmptyText
    title: NonEmptyText
    skill_ref: NonEmptyText
    dependencies: TaskPaths
    status: TaskStatus
    retry_count: Annotated[int, Field(ge=0, le=MAX_RETRY)]
    permission_tier: Annotated[int, Field(ge=1, le=3)]
    output_artifacts: TaskPaths
    error_msg: str | None
    acceptance: NonEmptyText | None = None
    phase: TaskPhase | None = None
    source_issue_ids: TaskPaths = ()
    acceptance_refs: CriterionReferences = ()
    worker_runs: tuple[WorkerRun, ...] = ()
    last_action: TaskActionReceipt | None = None
    task_revision: Annotated[int, Field(ge=1)] = Field(default=1, exclude_if=lambda value: value == 1)

    @property
    def pm_verified(self) -> bool:
        if self.status != TaskStatus.COMPLETED or not self.worker_runs:
            return False
        run = self.worker_runs[-1]
        return bool(run.execution_event_id and run.status == "COMPLETED" and run.acceptance
                    and run.acceptance.verdict.verdict == "VERIFIED")


class PlanEditReceipt(StrictModel):
    id: NonEmptyText
    summary: NonEmptyText
    source: PlanEditSource
    timestamp: Annotated[int, Field(ge=0)]
    added: TaskPaths = ()
    updated: TaskPaths = ()
    removed: TaskPaths = ()
    source_ref: NonEmptyText | None = None
    event_ids: TaskPaths = ()


class ProjectBudgetAlert(StrictModel):
    event_id: NonEmptyText
    summary: NonEmptyText


class BudgetPolicy(StrictModel):
    max_total_cost_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)] = DEFAULT_BUDGET_USD
    current_cost_usd: NonnegativeCost = 0.0
    max_task_retries: Annotated[int, Field(ge=1, le=MAX_RETRY)] = MAX_RETRY


class EventPayload(StrictModel):
    cost_usd: NonnegativeCost
    summary: str
    output_artifacts: TaskPaths = ()


class EventEnvelope(StrictModel):
    event_id: NonEmptyText
    timestamp: Annotated[int, Field(ge=0)]
    task_id: NonEmptyText | None
    payload: EventPayload
    project_id: NonEmptyText


class InboxEvent(EventEnvelope):
    type: InboxEventType
    consumed: bool = False
    replace_plan: bool = False
    superseded_by: NonEmptyText | None = None
    deleted_at: UnixSeconds | None = Field(default=None, exclude_if=lambda value: value is None)
    action_ref: NonEmptyText | None = None
    execution: TaskRunRef | None = Field(default=None, exclude_if=lambda value: value is None)
    execution_failure: TaskExecutionFailure | None = Field(default=None, exclude_if=lambda value: value is None)
    confirm_worker_stopped: bool = Field(default=False, exclude_if=lambda value: not value)
    verdict: PMTaskVerdict | None = Field(default=None, exclude_if=lambda value: value is None)
    checks: Annotated[tuple[PMTaskCheck, ...], BeforeValidator(_freeze_list), Field(max_length=13)] = Field(
        default=(), exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def check_acceptance_event(self) -> "InboxEvent":
        if self.type in (InboxEventType.TASK_EXECUTION_FINISHED, InboxEventType.TASK_VERIFIED, InboxEventType.TASK_REJECTED):
            if self.task_id is None or self.execution is None:
                raise ValueError("A task acceptance event requires its project, task and execution identity")
            if self.type == InboxEventType.TASK_EXECUTION_FINISHED:
                if self.verdict is not None:
                    raise ValueError("A Worker execution receipt cannot include a PM verdict")
            else:
                expected = "VERIFIED" if self.type == InboxEventType.TASK_VERIFIED else "REJECTED"
                if (self.verdict is None or self.verdict.verdict != expected or self.checks
                        or self.execution_failure is not None):
                    raise ValueError("Only a scoped PM verdict can settle task acceptance")
        elif (self.execution is not None or self.verdict is not None or self.checks
              or self.execution_failure is not None):
            raise ValueError("Only execution and verdict receipts can carry task acceptance fields")
        if self.confirm_worker_stopped and (self.type != InboxEventType.TASK_RETRY or self.action_ref is None):
            raise ValueError("Worker stop confirmation requires an explicit retry of the matching alert")
        return self


class PlanningProgress(StrictModel):
    started_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    preview: str | None = None


class ProjectOutcome(StrictModel):
    title: NonEmptyText
    summary: NonEmptyText


class ProjectArtifact(StrictModel):
    path: NonEmptyText
    label: NonEmptyText


class AcceptanceCheck(StrictModel):
    criterion_id: NonEmptyText
    description: NonEmptyText
    verified: bool
    task_ids: TaskPaths


class ProjectReport(StrictModel):
    done: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(ge=1)]
    unverified: Annotated[int, Field(ge=0)] = 0
    outcomes: list[ProjectOutcome]
    artifacts: list[ProjectArtifact]
    acceptance_checks: list[AcceptanceCheck] = Field(default_factory=list)
    delivery_verified: bool


class PlanPrepared(StrictModel):
    mode: Literal["INITIAL", "REVISION", "UPDATE"]
    approval_token: NonEmptyText | None
    task_count: Annotated[int, Field(ge=1)]


NotificationReason = Literal[
    "WORKER_FAILED", "WORKER_TIMEOUT", "WORKER_CLEANUP_FAILED", "WORKSPACE_ROLLBACK_FAILED",
    "BUDGET_EXHAUSTED", "ACCEPTANCE_GAP", "BLOCKED_PERMISSION", "BLOCKED_HUMAN", "RETRY_EXHAUSTED",
    "PM_ACCEPTANCE_FAILED", "WORKER_INTERRUPTED",
]


class OutboxEvent(EventEnvelope):
    type: OutboxEventType
    progress: PlanningProgress | None = None
    report: ProjectReport | None = None
    plan: PlanPrepared | None = None
    reason_code: NotificationReason | None = None


class ModelRoute(StrictModel):
    provider: NonEmptyText
    model: NonEmptyText
    reasoning_effort: str | None
    input_rate: NonnegativeCost
    output_rate: NonnegativeCost
    cache_read_rate: NonnegativeCost
    cache_write_rate: NonnegativeCost


class ProjectBrief(StrictModel):
    source_session_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    source_message_id: Annotated[str, Field(min_length=1, max_length=256)]
    user_instruction: Annotated[str, Field(min_length=1, max_length=8000)]
    acceptance: Annotated[tuple[BriefCriterion, ...], BeforeValidator(_freeze_list), Field(max_length=12)] = ()
    authorization: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    def criteria(self) -> dict[str, str]:
        return {f"C{number}": text for number, text in enumerate(self.acceptance, 1)}

    def planning_data(self) -> dict[str, object]:
        return {
            "acceptance": self.criteria(), "authorization": self.authorization,
        }


class ProjectBinding(StrictModel):
    project_id: NonEmptyText
    session_id: NonEmptyText
    route: ModelRoute
    name: NonEmptyText | None = None
    pm_name: PMName | None = None
    brief: ProjectBrief | None = None


class PrepareProject(StrictModel):
    session_id: NonEmptyText
    route: ModelRoute
    goal: Annotated[str, Field(min_length=1, max_length=8000)]
    max_budget_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    pm_name: PMName | None = None
    brief: ProjectBrief | None = None


class TaskDAG(StrictModel):
    project_id: NonEmptyText
    project_status: ProjectStatus
    tasks: list[Task]
    applied_feedback: tuple[NonEmptyText, ...] = ()
    goal_tree: NonEmptyText | None = None
    project_name: NonEmptyText | None = None
    plan_edits: tuple[PlanEditReceipt, ...] = ()
    started_at: UnixSeconds | None = None
    completion_generation: Annotated[int, Field(ge=0)] = 0
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    binding: ProjectBinding | None = None
    budget_alert: ProjectBudgetAlert | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def start_approval_base(self) -> str:
        base = f"start-{self.project_id}"
        latest = next((edit for edit in reversed(self.plan_edits) if edit.source == "PM_CHAT"), None)
        if latest is None:
            return base
        digest = sha256(latest.id.encode()).hexdigest()[:16]
        return f"{base}-pm-{digest}"

    @property
    def completion_event_id(self) -> str:
        base = f"completed-{self.project_id}"
        return f"{base}-{self.completion_generation}" if self.completion_generation else base
