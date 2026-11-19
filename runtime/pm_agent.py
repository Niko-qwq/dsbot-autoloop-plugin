import json
from collections.abc import Awaitable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue, StringConstraints, ValidationError

from core.bus import make_outbox
from core.governance import GovernanceGate
from core.schema import (
    CriterionReferences, InboxEventType, ModelRoute, NonEmptyText, OutboxEventType, PlanPrepared, PrepareProject,
    PMTaskVerdict, ProjectBrief, ProjectStatus, StrictModel, Task, TaskDAG, TaskPaths, TaskPhase, TaskStatus,
)
from core.state_manager import DAGManager, StalePlanEditError, StateFormatError, UneditableTaskError
from runtime.daemon import AgentReply, FeedbackBatch, ModelCallError


PMTaskId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")]
PM_PROMPT_MAX_BYTES = 40 * 1024
PM_TASK_EVIDENCE_MAX_BYTES = 8 * 1024
PM_PROJECT_CONSTRAINTS_MAX_BYTES = 2 * 1024
PM_ACCEPTANCE_SUMMARY_MAX_BYTES = 4 * 1024
PM_ACCEPTANCE_ARTIFACT_LIMIT = 16
PM_ACCEPTANCE_CHECKS_MAX_BYTES = 4 * 1024
PM_TASK_FIELDS = {
    "task_id", "title", "acceptance", "acceptance_refs", "dependencies", "skill_ref", "permission_tier", "phase", "status",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _excerpt(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit - len("…".encode("utf-8"))].decode("utf-8", errors="ignore") + "…"


def _evidence_paths(task: Task, directory: Path) -> list[str]:
    paths: dict[str, None] = {}
    for value in task.output_artifacts[:12]:
        try:
            path = directory / value
            relative = path.relative_to(directory).as_posix()
            if len(relative.encode("utf-8")) > 240 or path.resolve(strict=True) != path or not path.is_file():
                continue
        except (ValueError, OSError, RuntimeError):
            continue
        paths[relative] = None
        if len(paths) == 3:
            break
    return list(paths)


def pm_task_context(dag: TaskDAG, directory: Path, *, revision: bool = False) -> list[dict[str, JsonValue]]:
    """Keep editable task specs intact and show bounded evidence for work already attempted."""
    rows = {task.task_id: task.model_dump(mode="json", include=PM_TASK_FIELDS) for task in dag.tasks}
    if revision:
        return list(rows.values())
    attempted = [task for task in dag.tasks if not DAGManager.task_is_editable(task)]
    for task in attempted:
        if task.acceptance:
            rows[task.task_id]["acceptance"] = _excerpt(task.acceptance, 480)
    ordered = sorted(attempted, key=lambda task: (
        task.phase == "VERIFY" or bool(task.acceptance_refs),
        task.worker_runs[-1].finished_at or 0 if task.worker_runs else 0,
    ), reverse=True)
    remaining = PM_TASK_EVIDENCE_MAX_BYTES
    for task in ordered:
        run = task.worker_runs[-1] if task.worker_runs else None
        finished = run is not None and run.finished_at is not None
        paths = _evidence_paths(task, directory) if task.status == TaskStatus.COMPLETED else []
        if not finished and not paths:
            continue
        evidence = {
            "run_status": run.status if finished else "UNVERIFIED",
            "conclusion": _excerpt(run.detail.strip(), 420) if finished and run.detail.strip() else None,
            "paths": paths,
        }
        if run and run.acceptance:
            verdict = run.acceptance.verdict
            evidence["pm_verdict"] = verdict.verdict
            evidence["pm_reason"] = _excerpt(verdict.reason, 420)
        size = len(_json({"evidence": evidence}).encode("utf-8"))
        if size <= remaining:
            rows[task.task_id]["evidence"] = evidence
            remaining -= size
    return list(rows.values())


class PMTask(StrictModel):
    task_id: PMTaskId
    title: NonEmptyText
    acceptance: NonEmptyText
    dependencies: TaskPaths
    permission_tier: Annotated[int, Field(ge=1, le=3)]
    phase: TaskPhase | None = None
    source_issue_ids: TaskPaths = ()
    acceptance_refs: CriterionReferences = ()

    def to_task(self, source_issue_ids: tuple[str, ...] = ()) -> Task:
        return Task(
            **self.model_dump(exclude={"source_issue_ids"}), skill_ref="task-worker", status=TaskStatus.PENDING, retry_count=0,
            output_artifacts=(), error_msg=None, source_issue_ids=source_issue_ids,
        )


class PMRemoval(StrictModel):
    task_id: PMTaskId
    replaced_by: PMTaskId | None = None


class PMDelta(StrictModel):
    tasks: Annotated[list[PMTask], Field(max_length=50)]
    removals: Annotated[list[PMRemoval], Field(max_length=50)] = Field(default_factory=list)
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)] | None = None


class PMDraft(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    goal_tree: NonEmptyText
    tasks: Annotated[list[PMTask], Field(min_length=1, max_length=50)]


class NativePM(Protocol):
    async def __call__(
        self, project_id: str, directory: Path, route: ModelRoute, prompt: str, budget: float, draft: bool,
        *, revision: bool = False, acceptance: bool = False,
    ) -> AgentReply: ...


def parse_draft(output: str) -> PMDraft:
    try:
        return PMDraft.model_validate_json(output)
    except ValidationError as error:
        raise StateFormatError(f"PM did not return a valid project plan: {error}") from error


def validate_acceptance_tasks(
    tasks: Sequence[PMTask | Task], brief: ProjectBrief | None, *, complete_plan: bool,
) -> None:
    criteria = set(brief.criteria()) if brief is not None else set()
    covered = set()
    for task in tasks:
        unknown = set(task.acceptance_refs) - criteria
        if unknown:
            raise StateFormatError(f"Task {task.task_id} references unknown acceptance criteria: {', '.join(sorted(unknown))}")
        covered.update(task.acceptance_refs)
    missing = criteria - covered
    if complete_plan and missing:
        raise StateFormatError(f"The plan has no task covering acceptance criteria: {', '.join(sorted(missing))}")


class PMAgent:
    def __init__(self, gate: GovernanceGate, skill: Path, call: NativePM):
        self.gate = gate
        self.manager = gate.manager
        self.skill = skill
        self.call = call

    def _prompt(self, mode: str, *, optional: dict[str, object] | None = None, **context: object) -> str:
        prefix = self.skill.read_text(encoding="utf-8") + "\n\n"
        data = {"mode": mode, **context}
        prompt = prefix + _json(data)
        size = len(prompt.encode("utf-8"))
        if size > PM_PROMPT_MAX_BYTES:
            raise StateFormatError(
                f"Autoloop PM {mode} input is {size:,} UTF-8 bytes. The input budget is {PM_PROMPT_MAX_BYTES:,} bytes. "
                "Reduce the request or its project context, or split the feedback batch before retrying. No model request was sent."
            )
        for key, value in (optional or {}).items():
            if value:
                candidate = prefix + _json({**data, key: value})
                if len(candidate.encode("utf-8")) <= PM_PROMPT_MAX_BYTES:
                    data[key] = value
                    prompt = candidate
        return prompt

    async def draft(self, project_id: str, request: PrepareProject, directory: Path) -> tuple[PMDraft, TaskDAG, float]:
        brief = {}
        if request.brief is not None:
            brief["brief"] = request.brief.planning_data()
            if request.brief.user_instruction != request.goal:
                brief["original_instruction"] = request.brief.user_instruction
        prompt = self._prompt("draft", project_id=project_id, goal=request.goal, **brief)
        reply = await self.call(project_id, directory, request.route, prompt, request.max_budget_usd, True)
        draft = parse_draft(reply.output)
        validate_acceptance_tasks(draft.tasks, request.brief, complete_plan=True)
        dag = TaskDAG(project_id=project_id, project_status=ProjectStatus.IDLE,
                      tasks=[task.to_task() for task in draft.tasks], goal_tree=draft.goal_tree)
        self.manager.validate_graph(dag)
        return draft, dag, reply.cost_usd

    async def _call_charged(
        self, project_id: str, remaining: float, kind: str, pending: Awaitable[AgentReply],
    ) -> AgentReply:
        cost = None
        try:
            reply = await pending
            cost = reply.cost_usd
        except ModelCallError as error:
            cost = error.cost_usd
            raise
        finally:
            if cost is not None:
                async with self.manager.lock:
                    dag = self.manager.load()
                    if dag.project_id == project_id:
                        self.gate.charge(dag, cost)
                        self.manager._save(dag)
        if reply.cost_usd > remaining:
            raise StateFormatError(f"PM exceeded the remaining {kind} budget")
        return reply

    async def accept_task(self, project_id: str, task_id: str, evidence: dict[str, JsonValue]) -> PMTaskVerdict:
        async with self.manager.lock:
            dag = self.manager.load()
            if dag.project_id != project_id:
                raise StateFormatError(f"Task acceptance targets an inactive project: {project_id}")
            task = dag.tasks[self.manager.task_index(dag, task_id)]
            run = task.worker_runs[-1] if task.worker_runs else None
            if (task.status != TaskStatus.PENDING_VERIFY or run is None
                    or run.run_id != evidence.get("run_id") or task.task_revision != evidence.get("task_revision")):
                raise StateFormatError(f"Task attempt changed before PM acceptance of {task_id}")
            binding = dag.binding
            if binding is None or binding.project_id != project_id:
                raise StateFormatError("The project binding does not match the DAG")
            budget = dag.budget
            remaining = budget.max_total_cost_usd - budget.current_cost_usd
            if remaining <= 0:
                raise StateFormatError("No budget remains for task acceptance")
            directory = self.manager.project_directory(project_id)
            if not directory.is_dir() or directory.resolve() != directory:
                raise StateFormatError(f"Invalid project directory for task acceptance: {directory}")
            criteria = evidence.get("criteria")
            if not isinstance(criteria, dict) or not criteria:
                raise StateFormatError(f"Task {task_id} has no acceptance criteria to review")
            artifacts = []
            for value in task.output_artifacts:
                path = Path(value)
                if path.is_absolute():
                    if not path.is_relative_to(directory):
                        continue
                    path = path.relative_to(directory)
                if ".." not in path.parts and len(str(path).encode("utf-8")) <= 240:
                    artifacts.append(path.as_posix())
                if len(artifacts) >= PM_ACCEPTANCE_ARTIFACT_LIMIT:
                    break
            summary = run.detail
            worker_checks = []
            available = PM_ACCEPTANCE_CHECKS_MAX_BYTES
            for check in run.execution_checks:
                if check.criterion_id not in criteria:
                    continue
                record = {"criterion_id": check.criterion_id, "status": check.status,
                          "reason": _excerpt(check.reason, 450),
                          "evidence_refs": [ref for ref in check.evidence_refs[:3] if len(ref.encode("utf-8")) <= 240]}
                size = len(_json(record).encode("utf-8"))
                if size <= available:
                    worker_checks.append(record)
                    available -= size
            optional = {"worker_checks": worker_checks}
            if binding.brief:
                optional["authorization"] = _excerpt(binding.brief.authorization, PM_PROJECT_CONSTRAINTS_MAX_BYTES)
            prompt = self._prompt(
                "task_acceptance", project_id=project_id, task_id=task_id, title=task.title,
                permission_tier=task.permission_tier, criteria=criteria,
                evidence={"summary": _excerpt(summary, PM_ACCEPTANCE_SUMMARY_MAX_BYTES),
                          "output_artifacts": artifacts,
                          "output_artifacts_omitted": len(task.output_artifacts) - len(artifacts)},
                optional=optional,
            )

        reply = await self._call_charged(
            project_id, remaining, "task acceptance",
            self.call(project_id, directory, binding.route, prompt, remaining, False, acceptance=True),
        )
        try:
            verdict = PMTaskVerdict.model_validate_json(reply.output)
        except ValidationError as error:
            raise StateFormatError(f"PM did not return a valid task acceptance verdict: {error}") from error
        verdict.require_criteria(criteria)
        return verdict

    def _feedback_context(self, batch: FeedbackBatch) -> tuple[dict[str, object], list[dict[str, str]]]:
        last = batch[-1]
        context: dict[str, object] = {"event_id": last.event_id, "issue": last.payload.summary}
        if len(batch) > 1:
            context = {"issues": [{"event_id": event.event_id, "issue": event.payload.summary, "status": "PENDING"}
                                  for event in batch]}
        active_texts = {event.payload.summary for event in batch}
        included = {event.event_id for event in batch}
        superseded = []
        for event in reversed(self.gate.bus.inbox(last.project_id)):
            if (event.type != InboxEventType.USER_FEEDBACK or event.project_id != last.project_id
                    or event.superseded_by not in included):
                continue
            included.add(event.event_id)
            if event.payload.summary in active_texts:
                continue
            superseded.append({"event_id": event.event_id, "issue": _excerpt(event.payload.summary, 450),
                               "status": "SUPERSEDED"})
            if len(superseded) == 4:
                break
        return context, list(reversed(superseded))

    def _feedback_tasks(
        self, planned: list[PMTask], dag: TaskDAG, issue_ids: tuple[str, ...], *, revision: bool, replacement: bool,
    ) -> list[Task]:
        if len(issue_ids) == 1 and (replacement or not revision):
            return [task.to_task(issue_ids) for task in planned]

        inherited: dict[str, tuple[str, ...]] = {}
        if revision and not replacement and any(task.source_issue_ids for task in dag.tasks):
            active_history = {event.event_id for event in self.gate.bus.inbox(dag.project_id)
                              if event.project_id == dag.project_id and event.type == InboxEventType.USER_FEEDBACK
                              and event.superseded_by is None and event.event_id in dag.applied_feedback}
            inherited = {task.task_id: tuple(source for source in task.source_issue_ids if source in active_history)
                         for task in dag.tasks}
        actionable = set(issue_ids)
        tasks = []
        for node in planned:
            approved = tuple(source for source in node.source_issue_ids if source in actionable)
            linked = tuple(dict.fromkeys(inherited.get(node.task_id, ()) + approved))
            tasks.append(node.to_task(linked))
        return tasks

    def _announce_plan(
        self, dag: TaskDAG, issue_ids: tuple[str, ...], task_count: int, *, revision: bool, summary: str | None = None,
    ) -> None:
        digest = sha256("\0".join(issue_ids).encode()).hexdigest()[:20]
        if revision:
            summary = f"方案已更新，共 {task_count} 项任务，等待确认启动。"
        self.gate.bus._append_outbox(make_outbox(
            OutboxEventType.PROJECT_PREPARED, project_id=dag.project_id,
            event_id=f"prepared-{dag.project_id}-{digest}", summary=summary or "方案已更新。",
            plan=PlanPrepared(
                mode="REVISION" if revision else "UPDATE",
                approval_token=f"{dag.start_approval_base}-{issue_ids[-1]}" if revision else None,
                task_count=task_count,
            ),
        ))

    async def feedback(self, batch: FeedbackBatch) -> None:
        try:
            await self._apply_feedback(batch)
        except (StalePlanEditError, UneditableTaskError):
            await self._apply_feedback(batch)

    async def _apply_feedback(self, batch: FeedbackBatch) -> None:
        event_ids = tuple(event.event_id for event in batch)
        replacement = any(event.replace_plan for event in batch)
        async with self.manager.lock:
            dag = self.manager.load()
            revision = dag.project_status == ProjectStatus.IDLE
            if (batch[0].project_id != dag.project_id or any(event_id in dag.applied_feedback for event_id in event_ids)
                    or not self.gate.bus._feedback_is_current(batch, complete=revision)):
                return
            if replacement and not revision:
                raise StateFormatError("Only an unconfirmed project plan can be replaced")
            binding = dag.binding
            if binding is None or binding.project_id != dag.project_id:
                raise StateFormatError("The project binding does not match the DAG")
            budget = dag.budget
            remaining = budget.max_total_cost_usd - budget.current_cost_usd
            if revision:
                if remaining <= 0:
                    raise StateFormatError("No budget remains for revising the plan")
            else:
                self.gate._enforce_budget(dag)
            fingerprint = self.manager.plan_fingerprint(dag)
            directory = self.manager.project_directory(dag.project_id)
            feedback_context, superseded = self._feedback_context(batch)
            removed_task_ids = sorted({task_id for edit in dag.plan_edits for task_id in edit.removed})
            optional = {"superseded_issues": superseded}
            if binding.brief is not None:
                feedback_context["brief"] = binding.brief.planning_data()
            if replacement:
                prompt = self._prompt("replacement", removed_task_ids=removed_task_ids, optional=optional,
                                      **feedback_context)
            else:
                plan_context: dict[str, object] = {"name": dag.project_name or binding.name or dag.project_id}
                if not revision:
                    plan_context["editable_task_ids"] = [task.task_id for task in dag.tasks
                                                         if self.manager.task_is_editable(task)]
                tasks = pm_task_context(dag, directory, revision=revision)
                evidence = {str(task["task_id"]): task.pop("evidence") for task in tasks if "evidence" in task}
                prompt = self._prompt(
                    "revision" if revision else "feedback", goal_tree=dag.goal_tree,
                    dag={"project_id": dag.project_id, "project_status": dag.project_status.value, "tasks": tasks},
                    removed_task_ids=removed_task_ids, optional={"task_evidence": evidence, **optional},
                    **plan_context, **feedback_context,
                )
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.resolve() != directory:
                raise StateFormatError(f"Unexpected link in the project directory: {directory}")
        reply = await self._call_charged(
            dag.project_id, remaining, "plan revision" if revision else "plan changes",
            self.call(dag.project_id, directory, binding.route, prompt, remaining, revision, revision=revision),
        )
        if revision:
            plan = parse_draft(reply.output)
            validate_acceptance_tasks(plan.tasks, binding.brief, complete_plan=True)
            async with self.manager.lock:
                if self.gate.bus._feedback_is_current(batch, complete=True):
                    current = self.manager.load()
                    if current.project_id != dag.project_id or self.manager.plan_fingerprint(current) != fingerprint:
                        raise StalePlanEditError("The project plan changed while the revision was being proposed")
                    name = " ".join(plan.name.split()) if replacement else None
                    if replacement and not name:
                        raise StateFormatError("The replacement plan needs a project name")
                    tasks = self._feedback_tasks(plan.tasks, dag, event_ids, revision=True, replacement=replacement)
                    self.manager.replace_plan(
                        dag.project_id, event_ids, plan.goal_tree, tasks, project_name=name,
                    )
                    self._announce_plan(self.manager.load(), event_ids, len(tasks), revision=True)
                    self.gate.bus._ack_feedback(batch)
            return
        try:
            planned = PMDelta.model_validate_json(reply.output)
        except ValidationError as error:
            raise StateFormatError(f"PM did not return valid task edits: {error}") from error
        validate_acceptance_tasks(planned.tasks, binding.brief, complete_plan=False)
        removals = {removal.task_id: removal.replaced_by for removal in planned.removals}
        if len(removals) != len(planned.removals):
            raise StateFormatError("PM returned duplicate task removals")
        edit_id = "feedback-" + sha256(json.dumps(event_ids).encode()).hexdigest()[:24]
        summary = planned.summary or ("已根据本批反馈更新计划。" if planned.tasks or removals else "现有计划已涵盖本批反馈。")
        async with self.manager.lock:
            if self.gate.bus._feedback_is_current(batch):
                tasks = self._feedback_tasks(planned.tasks, dag, event_ids, revision=False, replacement=False)
                updated = self.manager.commit_task_edit(
                    dag.project_id, edit_id, summary, "INBOX", tasks, removals,
                    expected_fingerprint=fingerprint, source_ref=event_ids[-1], event_ids=event_ids,
                    required_criteria=tuple(binding.brief.criteria()) if binding.brief else (),
                )
                receipt = updated.plan_edits[-1]
                task_count = len(receipt.added) + len(receipt.updated) + len(receipt.removed)
                if task_count:
                    self._announce_plan(updated, event_ids, task_count, revision=False, summary=summary)
                self.gate.bus._ack_feedback(batch)
