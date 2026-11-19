import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import JsonValue

from config.constants import MAX_RETRY, POLL_INTERVAL_SEC
from core.bus import make_inbox, make_outbox
from core.governance import BudgetExceededError
from core.schema import (
    AcceptanceCheck, InboxEvent, InboxEventType, NonnegativeCost, OutboxEventType, PlanningProgress, PMTaskVerdict,
    ProjectArtifact, ProjectOutcome, ProjectReport, ProjectStatus, StrictModel, Task, TaskAcceptanceReceipt,
    TaskActionReceipt, TaskDAG, TaskEscalation, TaskRunRef, TaskStatus, UIActionType,
)
from core.state_manager import StateFormatError, task_escalation_id, task_retry_kind, workspace_has_unconfirmed_writer
from runtime.router import Router
from runtime.watchdog import block_dependents


class AgentReply(StrictModel):
    output: str
    cost_usd: NonnegativeCost


class ModelCallError(RuntimeError):
    def __init__(self, message: str, cost_usd: float | None, progress: PlanningProgress | None = None):
        super().__init__(message)
        self.cost_usd = cost_usd
        self.progress = progress


FeedbackBatch = tuple[InboxEvent, ...]
Feedback = Callable[[FeedbackBatch], Awaitable[None]]
TaskAcceptance = Callable[[str, str, dict[str, JsonValue]], Awaitable[PMTaskVerdict]]
ACCEPTANCE_EVENTS = (InboxEventType.TASK_EXECUTION_FINISHED, InboxEventType.TASK_VERIFIED, InboxEventType.TASK_REJECTED)
REPORT_OUTCOME_LIMIT = 5
REPORT_ARTIFACT_LIMIT = 8
logger = logging.getLogger(__name__)


class AutoloopDaemon:
    def __init__(
        self, router: Router, *, feedback: Feedback, task_acceptance: TaskAcceptance,
        read_planning_progress: Callable[[str], PlanningProgress | None] | None = None,
    ):
        self.router = router
        self.gate = router.watchdog.gate
        self.manager = router.manager
        self.bus = self.gate.bus
        self.feedback = feedback
        self.task_acceptance = task_acceptance
        self._read_planning_progress = read_planning_progress
        self._feedback_job: asyncio.Task[None] | None = None
        self._feedback_events: FeedbackBatch = ()
        self._acceptance_job: asyncio.Task[None] | None = None

    @property
    def feedback_event_ids(self) -> tuple[str, ...]:
        return tuple(event.event_id for event in self._feedback_events)

    def planning_progress(self, project_id: str) -> PlanningProgress | None:
        return self._read_planning_progress(project_id) if self._read_planning_progress is not None else None

    async def recover(self) -> None:
        async with self.manager.lock:
            dag = self.manager.load()
            pending = tuple(event for event in self.bus.inbox(dag.project_id) if not event.consumed)
            submitted = {(event.task_id, event.execution.run_id, event.execution.task_revision)
                         for event in pending if event.type == InboxEventType.TASK_EXECUTION_FINISHED
                         and event.execution is not None}
            notices = {event.event_id for event in self.bus.outbox(dag.project_id)}
            settled = tuple(event for event in pending if event.type == InboxEventType.USER_FEEDBACK
                            and (event.event_id in dag.applied_feedback or f"feedback-error-{event.event_id}" in notices))
            if settled:
                self.bus._ack_feedback(settled)
            changed = False
            for index, task in enumerate(dag.tasks):
                if task.status != TaskStatus.RUNNING:
                    continue
                if not task.worker_runs:
                    raise StateFormatError(f"Missing execution identity for active task {task.task_id}")
                run = task.worker_runs[-1]
                if (task.task_id, run.run_id, task.task_revision) in submitted:
                    continue
                reason = "服务重启后无法确认上次 Worker 的结果。请检查原运行和工作区，再决定是否继续。"
                interrupted = run.model_copy(update={"status": "INTERRUPTED", "finished_at": time.time(), "detail": reason})
                dag.tasks[index] = task.model_copy(update={"worker_runs": (*task.worker_runs[:-1], interrupted)})
                self._block_acceptance(dag, index, TaskEscalation(reason_code="WORKER_INTERRUPTED", summary=reason))
                changed = True
            if changed:
                self.manager._save(dag)
        await self._publish_escalations()
        await self._publish_project_report()

    async def run_forever(self) -> None:
        try:
            while True:
                await self.step()
                await asyncio.sleep(POLL_INTERVAL_SEC)
        finally:
            jobs = tuple(job for job in (self._acceptance_job, self._feedback_job) if job is not None)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await self.router.stop()

    async def step(self) -> None:
        if self._acceptance_job is not None and self._acceptance_job.done():
            self._acceptance_job.result()
            self._acceptance_job = None
        if self._feedback_job is not None and self._feedback_job.done():
            self._feedback_job.result()
            self._feedback_job = None
            self._feedback_events = ()
        pending = await self.bus.pending_inbox()
        dag = self.manager.load()
        feedback = tuple(event for event in pending if event.type == InboxEventType.USER_FEEDBACK)
        batch = tuple(event for event in feedback if event.project_id == dag.project_id and event.superseded_by is None
                      and event.deleted_at is None and event.event_id not in dag.applied_feedback)
        stale = tuple(event for event in feedback if event not in batch)
        if stale:
            async with self.manager.lock:
                self.bus._ack_feedback(stale)
        if self._feedback_job is not None and (
            (dag.project_status == ProjectStatus.IDLE and self._feedback_events != batch)
            or any(event not in batch for event in self._feedback_events)
        ):
            self._feedback_job.cancel()
            await asyncio.gather(self._feedback_job, return_exceptions=True)
            self._feedback_job = None
            self._feedback_events = ()
        for event in pending:
            if event.type != InboxEventType.USER_FEEDBACK:
                await self._apply(event)

        if self._feedback_job is None and batch:
            self._feedback_events = batch
            self._feedback_job = asyncio.create_task(self._apply_feedback(batch))

        await self._publish_project_report()
        await self.router.dispatch_ready_tasks()
        await self._schedule_acceptance()
        await self._publish_escalations()

    @staticmethod
    def _task_criteria(dag: TaskDAG, task: Task) -> dict[str, str]:
        brief = dag.binding.brief if dag.binding is not None else None
        criteria = brief.criteria() if brief else {}
        return {"TASK": task.acceptance or task.title, **{ref: criteria[ref] for ref in task.acceptance_refs}}

    async def _schedule_acceptance(self) -> None:
        if self._acceptance_job is not None:
            return
        async with self.manager.lock:
            dag = self.manager.load()
            if dag.project_status != ProjectStatus.ACTIVE or workspace_has_unconfirmed_writer(dag):
                return
            for index, task in enumerate(dag.tasks):
                if task.status != TaskStatus.PENDING_VERIFY:
                    continue
                run = task.worker_runs[-1]
                try:
                    self.gate._enforce_budget(dag)
                except BudgetExceededError:
                    return
                if run.verification_attempts >= MAX_RETRY:
                    self._block_acceptance(dag, index, TaskEscalation(reason_code="PM_ACCEPTANCE_FAILED", summary=(
                        "PM 验收暂不可用或已用完重试次数。Worker 提交已保留，排查后可重新验收。"
                    )))
                    self.manager._save(dag)
                    return
                try:
                    criteria = self._task_criteria(dag, task)
                except KeyError as error:
                    self._block_acceptance(dag, index, TaskEscalation(reason_code="BLOCKED_HUMAN", summary=(
                        f"计划引用的用户验收项 {error} 不可读取。Worker 提交已保留，请核对项目需求。"
                    )))
                    self.manager._save(dag)
                    return
                run_ref = TaskRunRef(run_id=run.run_id, task_revision=task.task_revision)
                evidence = {"run_id": run_ref.run_id, "task_revision": run_ref.task_revision, "criteria": criteria}
                # Reserve the review before any model call so interruption consumes a bounded attempt.
                claimed = run.model_copy(update={"verification_attempts": run.verification_attempts + 1})
                dag.tasks[index] = task.model_copy(update={"worker_runs": (*task.worker_runs[:-1], claimed)})
                self.manager._save(dag)
                self._acceptance_job = asyncio.create_task(self._accept_task(self.task_acceptance, dag.project_id,
                                                                            task.task_id, run_ref, evidence, criteria))
                return

    async def _accept_task(self, review: TaskAcceptance, project_id: str, task_id: str, run_ref: TaskRunRef,
                           evidence: dict[str, JsonValue], criteria: dict[str, str]) -> None:
        try:
            verdict = await review(project_id, task_id, evidence)
            if not isinstance(verdict, PMTaskVerdict):
                raise ValueError("PM did not return a valid task acceptance verdict")
            verdict.require_criteria(criteria)
        except (ValueError, RuntimeError, OSError) as error:
            logger.warning("PM task acceptance failed for %s/%s/%s: %s", project_id, task_id, run_ref.run_id, error)
            async with self.manager.lock:
                dag = self.manager.load()
                index = self._acceptance_target(dag, project_id, task_id, run_ref)
                if index is not None:
                    run = dag.tasks[index].worker_runs[-1]
                    try:
                        self.gate._enforce_budget(dag)
                    except BudgetExceededError:
                        return
                    if run.verification_attempts >= MAX_RETRY:
                        self._block_acceptance(dag, index, TaskEscalation(reason_code="PM_ACCEPTANCE_FAILED", summary=(
                            f"PM 无法完成验收，Worker 提交已保留。{str(error)[:300]}"
                        )))
                        self.manager._save(dag)
            return
        async with self.manager.lock:
            dag = self.manager.load()
            index = self._acceptance_target(dag, project_id, task_id, run_ref)
            if index is None:
                return
            kind = InboxEventType.TASK_VERIFIED if verdict.verdict == "VERIFIED" else InboxEventType.TASK_REJECTED
            self.bus._append_inbox(make_inbox(kind, project_id=project_id, task_id=task_id, execution=run_ref,
                                              verdict=verdict, event_id=f"task-verdict-{run_ref.run_id}", summary=verdict.reason,
                                              output_artifacts=dag.tasks[index].output_artifacts))

    @staticmethod
    def _acceptance_target(dag: TaskDAG, project_id: str, task_id: str, run_ref: TaskRunRef) -> int | None:
        if dag.project_id == project_id:
            for index, task in enumerate(dag.tasks):
                if (task.task_id == task_id and task.task_revision == run_ref.task_revision
                        and task.status == TaskStatus.PENDING_VERIFY and task.worker_runs
                        and task.worker_runs[-1].run_id == run_ref.run_id):
                    return index
        return None

    @staticmethod
    def _block_acceptance(dag: TaskDAG, index: int, escalation: TaskEscalation) -> None:
        task = dag.tasks[index]
        run = task.worker_runs[-1].model_copy(update={"escalation": escalation})
        dag.tasks[index] = task.model_copy(update={
            "status": TaskStatus.FAILED, "error_msg": escalation.summary, "worker_runs": (*task.worker_runs[:-1], run),
        })
        block_dependents(dag, task.task_id)

    async def _publish_escalations(self) -> None:
        async with self.manager.lock:
            dag = self.manager.load()
            alerts = [(task, event_id) for task in dag.tasks if (event_id := task_escalation_id(task)) is not None]
            budget_alert = dag.budget_alert if dag.project_status == ProjectStatus.ALERT else None
            if not alerts and budget_alert is None:
                return
            known = {event.event_id for event in self.bus.outbox(dag.project_id)}
            additions = []
            if budget_alert is not None and budget_alert.event_id not in known:
                additions.append(make_outbox(
                    OutboxEventType.ALERT_ESCALATED, project_id=dag.project_id, event_id=budget_alert.event_id,
                    reason_code="BUDGET_EXHAUSTED", summary=budget_alert.summary,
                ))
            for task, event_id in alerts:
                if event_id not in known:
                    escalation = task.worker_runs[-1].escalation
                    additions.append(make_outbox(
                        OutboxEventType.ALERT_ESCALATED, project_id=dag.project_id, task_id=task.task_id, event_id=event_id,
                        reason_code=escalation.reason_code, summary=escalation.summary,
                    ))
            if additions:
                self.bus.append_outbox_batch(additions)

    async def _apply_feedback(self, batch: FeedbackBatch) -> None:
        revision = self.manager.load().project_status == ProjectStatus.IDLE
        try:
            await self.feedback(batch)
        except (ValueError, RuntimeError, OSError, TimeoutError) as error:
            async with self.manager.lock:
                if not self.bus._feedback_is_current(batch, complete=revision):
                    return
                dag = self.manager.load()
                if dag.project_id == batch[0].project_id:
                    operation = "replace the plan" if any(event.replace_plan for event in batch) else (
                        "revise the plan" if revision else "update tasks"
                    )
                    known = {event.event_id for event in self.bus.outbox(dag.project_id)}
                    self.bus.append_outbox_batch(make_outbox(
                        OutboxEventType.PROJECT_ALERT, project_id=dag.project_id, event_id=f"feedback-error-{event.event_id}",
                        summary=f"PM could not {operation}: {error}"[:1000],
                        progress=error.progress if isinstance(error, ModelCallError) else None,
                    ) for event in batch if f"feedback-error-{event.event_id}" not in known)
                self.bus._ack_feedback(batch)
            return
        async with self.manager.lock:
            if self.bus._feedback_is_current(batch, complete=revision):
                self.bus._ack_feedback(batch)

    async def _apply(self, event: InboxEvent) -> None:
        async with self.manager.lock:
            dag = self.manager.load()
            if event.project_id == dag.project_id:
                if event.type == InboxEventType.USER_COMMAND:
                    self._project_action(dag, UIActionType(event.payload.summary), event.action_ref)
                elif event.type == InboxEventType.TASK_RETRY:
                    self._retry_task(dag, event)
                elif event.type in ACCEPTANCE_EVENTS:
                    self._apply_acceptance_event(dag, event)
                self.manager._save(dag)
            self.bus._ack_inbox(event.event_id)

    def _apply_acceptance_event(self, dag: TaskDAG, event: InboxEvent) -> None:
        run_ref = event.execution
        if event.project_id != dag.project_id or run_ref is None:
            return
        if event.type == InboxEventType.TASK_EXECUTION_FINISHED:
            failure = event.execution_failure
            for index, task in enumerate(dag.tasks):
                if (task.task_id != event.task_id or task.status != TaskStatus.RUNNING or not task.worker_runs
                        or task.task_revision != run_ref.task_revision or task.worker_runs[-1].run_id != run_ref.run_id):
                    continue
                latest = task.worker_runs[-1]
                if latest.execution_event_id is not None:
                    return
                run = latest.model_copy(update={
                    "status": "FAILED" if failure else "COMPLETED", "finished_at": max(float(event.timestamp), latest.started_at),
                    "detail": event.payload.summary, "execution_event_id": event.event_id, "execution_checks": event.checks,
                })
                self.gate.charge(dag, event.payload.cost_usd)
                if failure is not None:
                    if failure.reason_code == "BUDGET_EXHAUSTED":
                        self.gate.mark_budget_alert(dag, event.payload.summary)
                        dag.tasks[index] = task.model_copy(update={
                            "status": TaskStatus.READY, "error_msg": event.payload.summary,
                            "worker_runs": (*task.worker_runs[:-1], run),
                        })
                        return
                    failures = task.retry_count + 1
                    exhausted = not failure.retryable or failures >= dag.budget.max_task_retries
                    dag.tasks[index] = task.model_copy(update={
                        "status": TaskStatus.FAILED if exhausted else TaskStatus.READY, "retry_count": failures,
                        "error_msg": event.payload.summary,
                        "worker_runs": (*task.worker_runs[:-1], run),
                    })
                    if exhausted:
                        self._block_acceptance(dag, index, TaskEscalation(reason_code=failure.reason_code, summary=event.payload.summary))
                    if failure.reason_code == "WORKER_CLEANUP_FAILED":
                        dag.project_status = ProjectStatus.ALERT
                    return
                dag.tasks[index] = task.model_copy(update={
                    "status": TaskStatus.PENDING_VERIFY, "output_artifacts": event.payload.output_artifacts,
                    "error_msg": None, "worker_runs": (*task.worker_runs[:-1], run),
                })
                return
            return

        index = self._acceptance_target(dag, event.project_id, event.task_id, run_ref)
        verdict = event.verdict
        if index is None or verdict is None:
            return
        task = dag.tasks[index]
        latest = task.worker_runs[-1]
        if latest.execution_event_id is None or latest.verification_attempts == 0 or latest.acceptance is not None:
            return
        verdict.require_criteria(self._task_criteria(dag, task))
        run = latest.model_copy(update={"acceptance": TaskAcceptanceReceipt(event_id=event.event_id, verdict=verdict)})
        task = task.model_copy(update={"worker_runs": (*task.worker_runs[:-1], run)})
        if verdict.verdict == "VERIFIED":
            dag.tasks[index] = task.model_copy(update={"status": TaskStatus.COMPLETED, "error_msg": None})
            return
        dag.tasks[index] = task
        if verdict.disposition == "REWORK":
            failures = task.retry_count + 1
            if failures < dag.budget.max_task_retries:
                dag.tasks[index] = task.model_copy(update={
                    "status": TaskStatus.READY, "retry_count": failures, "error_msg": verdict.reason,
                })
                return
            dag.tasks[index] = task.model_copy(update={"retry_count": failures})
            self._block_acceptance(dag, index, TaskEscalation(reason_code="RETRY_EXHAUSTED", summary=verdict.reason))
        else:
            self._block_acceptance(dag, index, TaskEscalation(reason_code=verdict.disposition, summary=verdict.reason))

    def _project_action(self, dag: TaskDAG, action: UIActionType, action_ref: str | None) -> None:
        if not dag.tasks:
            return
        current = dag.project_status
        if (action == UIActionType.START_PROJECT and current == ProjectStatus.IDLE
                and action_ref is not None and action_ref == self.bus.start_token(dag)):
            dag.project_status = ProjectStatus.ACTIVE
            dag.started_at = int(time.time())
        elif action == UIActionType.PAUSE_PROJECT and current == ProjectStatus.ACTIVE:
            dag.project_status = ProjectStatus.PAUSED
        elif action == UIActionType.RESUME_PROJECT and current in (ProjectStatus.PAUSED, ProjectStatus.ALERT):
            if any(task.status == TaskStatus.FAILED and task.worker_runs and task.worker_runs[-1].escalation
                   and task.worker_runs[-1].escalation.reason_code == "WORKER_CLEANUP_FAILED" for task in dag.tasks):
                return
            try:
                self.gate._enforce_budget(dag)
            except BudgetExceededError:
                return
            dag.project_status = ProjectStatus.ACTIVE
            dag.budget_alert = None

    @staticmethod
    def _retry_task(dag: TaskDAG, event: InboxEvent) -> None:
        for index, task in enumerate(dag.tasks):
            if task.task_id != event.task_id:
                continue
            if task.last_action is not None and task.last_action.event_id == event.event_id:
                return
            kind = task_retry_kind(task)
            event_id = task_escalation_id(task)
            if kind is None or event_id is None or event.action_ref != event_id:
                return
            if (kind == "INTERRUPTED") != event.confirm_worker_stopped:
                return
            receipt = TaskActionReceipt(event_id=event.event_id, target_event_id=event_id, action="RETRY")
            if kind == "ACCEPTANCE":
                run = task.worker_runs[-1].model_copy(update={"verification_attempts": 0, "escalation": None})
                dag.tasks[index] = task.model_copy(update={
                    "status": TaskStatus.PENDING_VERIFY, "error_msg": None,
                    "worker_runs": (*task.worker_runs[:-1], run), "last_action": receipt,
                })
            else:
                dag.tasks[index] = task.model_copy(update={
                    "status": TaskStatus.READY, "retry_count": 0, "error_msg": None, "last_action": receipt,
                })
            return

    async def _publish_project_report(self) -> None:
        async with self.manager.lock:
            dag = self.manager.load()
            if dag.project_status not in (ProjectStatus.ACTIVE, ProjectStatus.COMPLETED) or not dag.tasks:
                return
            if any(task.status != TaskStatus.COMPLETED for task in dag.tasks):
                return
            event_id = dag.completion_event_id
            known = {event.event_id for event in self.bus.outbox(dag.project_id)}
            if event_id in known and dag.project_status == ProjectStatus.COMPLETED:
                return
            report = self._project_report(dag)
            if not report.delivery_verified:
                gap_id = f"acceptance-gap-{event_id}"
                if gap_id not in known:
                    self.bus._append_outbox(make_outbox(
                        OutboxEventType.ALERT_ESCALATED, project_id=dag.project_id, event_id=gap_id,
                        reason_code="ACCEPTANCE_GAP", report=report, summary="任务流程已结束，仍有验收项需要核验。",
                    ))
                return
            if dag.project_status != ProjectStatus.COMPLETED:
                dag.project_status = ProjectStatus.COMPLETED
                self.manager._save(dag)
            if event_id not in known:
                self.bus._append_outbox(make_outbox(
                    OutboxEventType.PROJECT_COMPLETED, project_id=dag.project_id, event_id=event_id,
                    report=report, summary="项目已完成 PM 验收。",
                ))

    def _project_report(self, dag: TaskDAG) -> ProjectReport:
        priority = ("VERIFY", "RELEASE", "IMPLEMENT", "DESIGN", "RESEARCH")
        ordered = sorted(reversed(dag.tasks), key=lambda task: priority.index(task.phase) if task.phase in priority else len(priority))
        outcomes = []
        for task in ordered:
            if not task.pm_verified:
                continue
            summary = " ".join(task.worker_runs[-1].acceptance.verdict.reason.split())
            if len(summary) > 720:
                summary = summary[:555].rstrip() + " … " + summary[-145:].lstrip()
            if summary:
                outcomes.append(ProjectOutcome(title=task.title, summary=summary))
            if len(outcomes) == REPORT_OUTCOME_LIMIT:
                break
        brief = dag.binding.brief if dag.binding is not None else None
        acceptance_checks = []
        for criterion_id, description in (brief.criteria() if brief else {}).items():
            tasks = [task for task in dag.tasks if criterion_id in task.acceptance_refs]
            acceptance_checks.append(AcceptanceCheck(
                criterion_id=criterion_id, description=description,
                verified=bool(tasks) and all(task.pm_verified and any(
                    check.criterion_id == criterion_id and check.status == "PASS"
                    for check in task.worker_runs[-1].acceptance.verdict.checks
                ) for task in tasks),
                task_ids=tuple(task.task_id for task in tasks),
            ))
        unverified = sum(not task.pm_verified for task in dag.tasks)
        return ProjectReport(
            done=len(dag.tasks) - unverified, total=len(dag.tasks), unverified=unverified, outcomes=outcomes,
            artifacts=self._report_artifacts(self.manager.project_directory(dag.project_id), ordered),
            acceptance_checks=acceptance_checks, delivery_verified=not unverified and all(check.verified for check in acceptance_checks),
        )

    @staticmethod
    def _report_artifacts(root: Path, tasks: list[Task]) -> list[ProjectArtifact]:
        candidates = dict.fromkeys(("index.html", *(value for task in tasks for value in task.output_artifacts)))
        artifacts: dict[str, ProjectArtifact] = {}
        for value in candidates:
            path = root / value
            try:
                relative = path.relative_to(root)
                if not relative.parts or any(part.startswith(".") for part in relative.parts):
                    continue
                if path.resolve(strict=True) != path or not path.is_file():
                    continue
            except (ValueError, OSError, RuntimeError):
                continue
            name = relative.as_posix()
            if name not in artifacts:
                artifacts[name] = ProjectArtifact(path=name, label=path.name)
            if len(artifacts) == REPORT_ARTIFACT_LIMIT:
                break
        return list(artifacts.values())
