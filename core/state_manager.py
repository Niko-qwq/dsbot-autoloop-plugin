import asyncio
import json
import os
import time
from collections import defaultdict, deque
from hashlib import sha256
from itertools import chain
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from core.schema import PlanEditReceipt, PlanEditSource, ProjectStatus, Task, TaskDAG, TaskStatus


class StateFormatError(ValueError):
    pass


class CyclicDAGError(StateFormatError):
    pass


class StalePlanEditError(StateFormatError):
    pass


class UneditableTaskError(StateFormatError):
    pass


Model = TypeVar("Model", bound=BaseModel)
TASK_RUNTIME_FIELDS = {"status", "retry_count", "output_artifacts", "error_msg", "worker_runs", "last_action",
                       "task_revision"}
EDITABLE_TASK_STATUSES = (TaskStatus.PENDING, TaskStatus.READY)
STARTED_TASK_STATUSES = (TaskStatus.RUNNING, TaskStatus.PENDING_VERIFY, TaskStatus.COMPLETED,
                        TaskStatus.FAILED)


def read_model(path: Path, model: type[Model]) -> Model:
    try:
        return model.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise StateFormatError(f"Invalid {path}: {error}") from error


def write_atomic(path: Path, content: str) -> None:
    """Replace one complete file. The caller holds the workspace lock."""
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _check_acyclic(dependencies: dict[str, set[str]], path: Path) -> None:
    dependents = defaultdict(list)
    indegree = {task_id: len(required) for task_id, required in dependencies.items()}
    for task_id, required in dependencies.items():
        for prerequisite in required:
            dependents[prerequisite].append(task_id)
    ready = deque(task_id for task_id, degree in indegree.items() if degree == 0)
    resolved = 0
    while ready:
        resolved += 1
        for task_id in dependents[ready.popleft()]:
            indegree[task_id] -= 1
            if indegree[task_id] == 0:
                ready.append(task_id)
    if resolved != len(dependencies):
        raise CyclicDAGError(f"Cycle in {path}: {', '.join(k for k, v in indegree.items() if v)}")


def _task_definition(task: Task) -> dict[str, object]:
    return task.model_dump(mode="json", exclude=TASK_RUNTIME_FIELDS)


def _has_attempt(task: Task) -> bool:
    return bool(task.status in STARTED_TASK_STATUSES or task.worker_runs or task.retry_count
                or task.output_artifacts or task.error_msg or task.last_action)


def _require_unstarted(task: Task) -> None:
    if not DAGManager.task_is_editable(task):
        raise UneditableTaskError(f"Task {task.task_id!r} cannot be edited or removed in {task.status} or after an attempt")


def _check_affected_tasks(current: dict[str, Task], prospective: dict[str, Task], changed: set[str]) -> None:
    dependents: dict[str, set[str]] = defaultdict(set)
    for task in chain(current.values(), prospective.values()):
        for prerequisite in task.dependencies:
            dependents[prerequisite].add(task.task_id)
    pending = deque(sorted(changed))
    visited = set(changed)
    while pending:
        for task_id in sorted(dependents[pending.popleft()] - visited):
            task = current.get(task_id)
            if task is not None and _has_attempt(task):
                raise UneditableTaskError(f"Plan edit affects downstream task {task_id!r}, which has already started")
            visited.add(task_id)
            pending.append(task_id)


def task_retry_kind(task: Task) -> Literal["EXECUTION", "ACCEPTANCE", "INTERRUPTED"] | None:
    if task.status != TaskStatus.FAILED or not task.worker_runs:
        return None
    run = task.worker_runs[-1]
    reason = run.escalation.reason_code if run.escalation is not None else None
    if reason == "WORKER_INTERRUPTED":
        return "INTERRUPTED"
    if reason == "PM_ACCEPTANCE_FAILED" and run.status == "COMPLETED" and run.execution_event_id and run.acceptance is None:
        return "ACCEPTANCE"
    if reason in ("RETRY_EXHAUSTED", "WORKER_FAILED", "WORKER_TIMEOUT"):
        return "EXECUTION"
    return None


def workspace_has_unconfirmed_writer(dag: TaskDAG) -> bool:
    return any(task.permission_tier >= 2 and task_retry_kind(task) == "INTERRUPTED" for task in dag.tasks)


def task_escalation_id(task: Task) -> str | None:
    if task.status != TaskStatus.FAILED or not task.worker_runs:
        return None
    run = task.worker_runs[-1]
    if run.escalation is None:
        return None
    event_id = f"escalation-{run.run_id}"
    if run.escalation.reason_code == "PM_ACCEPTANCE_FAILED" and task.last_action is not None:
        generation = sha256(task.last_action.event_id.encode()).hexdigest()[:16]
        return f"{event_id}-{generation}"
    return event_id


class DAGManager:
    def __init__(self, workspace: str | Path, lock: asyncio.Lock | None = None):
        self.workspace = Path(workspace).resolve()
        self.dag_path = self.workspace / "TASK_DAG.json"
        self.lock = lock if lock is not None else asyncio.Lock()

    def load(self) -> TaskDAG:
        dag = read_model(self.dag_path, TaskDAG)
        self.validate_graph(dag)
        return dag

    def task_index(self, dag: TaskDAG, task_id: str) -> int:
        for index, task in enumerate(dag.tasks):
            if task.task_id == task_id:
                return index
        raise StateFormatError(f"Unknown task {task_id!r} in {self.dag_path}")

    def validate_graph(self, dag: TaskDAG) -> None:
        if dag.binding is not None and dag.binding.project_id != dag.project_id:
            raise StateFormatError(f"Project binding does not match the DAG in {self.dag_path}")
        dependencies = {task.task_id: set(task.dependencies) for task in dag.tasks}
        if len(dependencies) != len(dag.tasks):
            raise StateFormatError(f"Duplicate task IDs in {self.dag_path}")
        retired = {task_id for receipt in dag.plan_edits for task_id in receipt.removed}
        reused = retired.intersection(dependencies)
        if reused:
            raise StateFormatError(f"Retired task IDs cannot be reused: {', '.join(sorted(reused))}")
        missing = set().union(*dependencies.values()).difference(dependencies)
        if missing:
            raise StateFormatError(f"Unknown dependencies in {self.dag_path}: {', '.join(sorted(missing))}")
        _check_acyclic(dependencies, self.dag_path)

    def _save(self, dag: TaskDAG) -> None:
        self.validate_graph(dag)
        write_atomic(self.dag_path, dag.model_dump_json(indent=2) + "\n")

    async def save_atomic(self, dag: TaskDAG) -> None:
        async with self.lock:
            self._save(dag)

    def plan_fingerprint(self, dag: TaskDAG) -> str:
        """Hash the plan and provenance shared by both planning entry points."""
        definition = {
            **dag.model_dump(mode="json", include={"project_id", "goal_tree", "project_name", "plan_edits", "applied_feedback"}),
            "tasks": [_task_definition(task) for task in dag.tasks],
        }
        canonical = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def task_is_editable(task: Task) -> bool:
        return task.status in EDITABLE_TASK_STATUSES and not _has_attempt(task)

    @staticmethod
    def task_is_amendable(task: Task) -> bool:
        if task.status != TaskStatus.FAILED or not task.worker_runs:
            return False
        run = task.worker_runs[-1]
        return bool(run.status == "COMPLETED" and run.execution_event_id and run.acceptance
                    and run.escalation and run.acceptance.verdict.disposition == run.escalation.reason_code
                    and run.escalation.reason_code in ("BLOCKED_PERMISSION", "BLOCKED_HUMAN"))

    def edit_tasks(self, dag: TaskDAG, tasks: list[Task], removals: dict[str, str | None], *,
                   allow_pm_amendments: bool = False) -> TaskDAG:
        """Validate a prospective edit without changing the live DAG or task attempts."""
        self.validate_graph(dag)
        current = {task.task_id: task for task in dag.tasks}
        upserts = {task.task_id: task for task in tasks}
        if len(upserts) != len(tasks):
            raise StateFormatError("Duplicate task IDs in the plan edit")
        if upserts.keys() & removals.keys():
            raise StateFormatError("A plan edit cannot both update and remove the same task")

        prospective = current.copy()
        for task_id, task in upserts.items():
            _require_unstarted(task)
            previous = current.get(task_id)
            amended_task = bool(allow_pm_amendments and previous is not None
                                and self.task_is_amendable(previous))
            if previous is not None:
                if amended_task:
                    if (task.dependencies != previous.dependencies or task.acceptance_refs != previous.acceptance_refs
                            or task.permission_tier > max(2, previous.permission_tier)):
                        raise UneditableTaskError(f"Task {task_id!r} amendment must keep dependencies, acceptance references and scope")
                    if all(getattr(task, field) == getattr(previous, field)
                           for field in ("title", "acceptance", "permission_tier", "phase", "skill_ref")):
                        raise UneditableTaskError(f"Task {task_id!r} amendment must specify what will change")
                else:
                    _require_unstarted(previous)
            sources = previous.source_issue_ids if previous is not None else ()
            prospective[task_id] = task.model_copy(update={
                "status": previous.status if previous is not None else TaskStatus.PENDING,
                "dependencies": tuple(dict.fromkeys(task.dependencies)),
                "source_issue_ids": tuple(dict.fromkeys((*sources, *task.source_issue_ids))),
                "task_revision": previous.task_revision if previous is not None else 1,
                **({"worker_runs": previous.worker_runs, "error_msg": previous.error_msg} if amended_task else {}),
            })

        for task_id in removals:
            removed = prospective.pop(task_id, None)
            if removed is None:
                raise StateFormatError(f"Unknown task {task_id!r} in the removal list")
            _require_unstarted(removed)
        if current and not prospective:
            raise StateFormatError("A plan edit must leave at least one task")
        merged_targets = set()
        for task_id, target_id in removals.items():
            if target_id is None:
                continue
            target = prospective.get(target_id)
            if target is None:
                raise StateFormatError(f"Merge target {target_id!r} for {task_id!r} must survive the edit")
            _require_unstarted(target)
            merged_targets.add(target_id)
            removed = current[task_id]
            inherited = tuple(dependency for dependency in removed.dependencies
                              if dependency != target_id and removals.get(dependency, dependency) is not None)
            prospective[target_id] = target.model_copy(update={
                "dependencies": (*target.dependencies, *inherited),
                "source_issue_ids": tuple(dict.fromkeys((*target.source_issue_ids, *removed.source_issue_ids))),
            })

        for task_id, task in prospective.items():
            if task_id not in merged_targets and not any(dependency in removals for dependency in task.dependencies):
                continue
            dependencies = []
            for dependency in task.dependencies:
                replacement = removals.get(dependency, dependency)
                if replacement is None:
                    raise StateFormatError(f"Task {task_id!r} still depends on removed task {dependency!r}; update its dependencies")
                # A link to merged work becomes internal to the surviving task.
                if replacement == task_id and dependency in removals:
                    continue
                dependencies.append(replacement)
            required = tuple(dict.fromkeys(dependencies))
            if required != task.dependencies:
                if task_id in current:
                    _require_unstarted(current[task_id])
                prospective[task_id] = task.model_copy(update={"dependencies": required})

        candidate = dag.model_copy(update={"tasks": list(prospective.values())})
        self.validate_graph(candidate)
        changed = set(removals) | {
            task_id for task_id, task in prospective.items()
            if task_id not in current or _task_definition(task) != _task_definition(current[task_id])
        }
        _check_affected_tasks(current, prospective, changed)
        completed = {task_id for task_id, task in prospective.items()
                     if task.pm_verified}
        for index, task in enumerate(candidate.tasks):
            if task.task_id in changed:
                status = TaskStatus.READY if completed.issuperset(task.dependencies) else TaskStatus.PENDING
                previous = current.get(task.task_id)
                candidate.tasks[index] = task.model_copy(update={
                    "status": status, "task_revision": previous.task_revision + 1 if previous is not None else 1,
                })
        return candidate

    def commit_task_edit(
        self, project_id: str, edit_id: str, summary: str, source: PlanEditSource, tasks: list[Task],
        removals: dict[str, str | None], *, expected_fingerprint: str | None = None, source_ref: str | None = None,
        event_ids: tuple[str, ...] = (), required_criteria: tuple[str, ...] = (),
    ) -> TaskDAG:
        """Atomically save a validated plan edit and its receipt. Caller holds the workspace lock."""
        dag = self.load()
        if dag.project_id != project_id:
            raise StateFormatError(f"Plan edit targets a different project: {project_id!r}")
        if any(receipt.id == edit_id for receipt in dag.plan_edits):
            return dag
        if expected_fingerprint is not None and expected_fingerprint != self.plan_fingerprint(dag):
            raise StalePlanEditError("The project plan changed while this edit was being proposed")
        if set(event_ids).intersection(dag.applied_feedback):
            raise StateFormatError("This edit includes feedback already applied to the plan")
        edited = self.edit_tasks(dag, tasks, removals, allow_pm_amendments=(
            source == "PM_CHAT" and expected_fingerprint is not None and source_ref is not None
        ))
        missing = set(required_criteria) - {ref for task in edited.tasks for ref in task.acceptance_refs}
        if missing:
            raise StateFormatError(f"The revised plan has no task covering acceptance criteria: {', '.join(sorted(missing))}")
        before = {task.task_id: _task_definition(task) for task in dag.tasks}
        after = {task.task_id: _task_definition(task) for task in edited.tasks}
        events = tuple(dict.fromkeys(event_ids))
        receipt = PlanEditReceipt(
            id=edit_id, summary=summary, source=source, timestamp=int(time.time()), source_ref=source_ref, event_ids=events,
            added=tuple(task_id for task_id in after if task_id not in before),
            updated=tuple(task_id for task_id in after if task_id in before and before[task_id] != after[task_id]),
            removed=tuple(task_id for task_id in before if task_id not in after),
        )
        edited.plan_edits += (receipt,)
        edited.applied_feedback += events
        if (dag.project_status == ProjectStatus.COMPLETED
                and any(task.task_id in receipt.added and task.status in EDITABLE_TASK_STATUSES for task in edited.tasks)):
            edited.project_status = ProjectStatus.ACTIVE
            edited.completion_generation += 1
        self._save(edited)
        return edited

    def replace_plan(
        self, project_id: str, event_ids: tuple[str, ...], goal_tree: str, tasks: list[Task], *, project_name: str | None = None,
    ) -> None:
        """Store reviewed plan and task graph together. Caller holds the workspace lock."""
        dag = self.load()
        if dag.project_id != project_id or dag.project_status != ProjectStatus.IDLE:
            raise StateFormatError("The project is no longer awaiting confirmation")
        receipts = tuple(event_id for event_id in event_ids if event_id not in dag.applied_feedback)
        if not receipts:
            return
        replacement = dag.model_copy(update={
            "tasks": tasks, "goal_tree": goal_tree, "applied_feedback": (*dag.applied_feedback, *receipts),
            "project_name": project_name or dag.project_name,
        })
        self._save(replacement)

    def project_directory(self, project_id: str) -> Path:
        key = sha256(project_id.encode()).hexdigest()[:24]
        return self.workspace / "projects" / key

    @staticmethod
    def promote_ready(dag: TaskDAG, completed: set[str]) -> list[Task]:
        promoted = []
        for index, task in enumerate(dag.tasks):
            if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED) and completed.issuperset(task.dependencies):
                ready = task.model_copy(update={"status": TaskStatus.READY})
                dag.tasks[index] = ready
                promoted.append(ready)
        return promoted
