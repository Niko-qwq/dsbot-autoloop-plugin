import asyncio
import os
import traceback
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from pydantic import Field

from config.constants import GIT_TIMEOUT_SEC
from core.bus import make_inbox
from core.governance import BudgetExceededError, GovernanceGate
from core.schema import (
    InboxEventType, NonnegativeCost, PMTaskCheck, StrictModel, TaskDAG, TaskExecutionFailure, TaskPaths, TaskRunRef, TaskStatus,
)
from core.state_manager import StateFormatError


class WorkerResult(StrictModel):
    cost_usd: NonnegativeCost
    summary: str
    output_artifacts: TaskPaths = ()
    checks: Annotated[tuple[PMTaskCheck, ...], Field(max_length=13)] = ()


class RollbackError(RuntimeError):
    pass


class WorkerCleanupError(RuntimeError):
    pass


class WorkerCancelled(asyncio.CancelledError):
    pass


def block_dependents(dag: TaskDAG, failed_id: str) -> None:
    dependents = defaultdict(list)
    for index, task in enumerate(dag.tasks):
        for prerequisite in task.dependencies:
            dependents[prerequisite].append(index)
    pending = deque(dependents[failed_id])
    blocked = set()
    while pending:
        index = pending.popleft()
        if index in blocked:
            continue
        blocked.add(index)
        task = dag.tasks[index]
        dag.tasks[index] = task.model_copy(update={"status": TaskStatus.BLOCKED})
        pending.extend(dependents[task.task_id])


async def _git(workspace: Path, *args: str, root: Path | None = None, index_file: Path | None = None) -> bytes:
    parent = workspace.parent if root is None else root
    process = await asyncio.create_subprocess_exec(
        "git", f"--git-dir={parent / '.git'}", f"--work-tree={parent}", *args, cwd=parent,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_INDEX_FILE": str(index_file)} if index_file is not None else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SEC)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RollbackError(f"git {' '.join(args)} in {parent} exited {process.returncode}: {stderr.decode(errors='replace').strip()}")
    return stdout


def _error_summary(error: Exception) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    boundary = frames if len(frames) <= 2 else [frames[0], frames[-1]]
    locations = [f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in boundary]
    detail = str(error)
    if isinstance(error, TimeoutError) and isinstance(error.__cause__, WorkerCancelled):
        detail = str(error.__cause__)
    message = f"{type(error).__name__}: {detail}"[:1800]
    return "\n".join((message, " → ".join(locations)))


class Watchdog:
    def __init__(self, gate: GovernanceGate):
        self.gate = gate
        self.manager = gate.manager
        self.bus = gate.bus

    async def run(
        self, project_id: str, task_id: str, execution: TaskRunRef, worker: Callable[[], Awaitable[WorkerResult]],
    ) -> None:
        failure = None
        result = None
        try:
            result = await worker()
            if not isinstance(result, WorkerResult):
                raise TypeError(f"Worker for {task_id} did not return a WorkerResult")
        except Exception as error:
            result = None
            reason = (
                "WORKER_CLEANUP_FAILED" if isinstance(error, WorkerCleanupError)
                else "BUDGET_EXHAUSTED" if isinstance(error, BudgetExceededError)
                else "WORKER_TIMEOUT" if isinstance(error, TimeoutError)
                else "WORKER_FAILED"
            )
            failure = TaskExecutionFailure(
                reason_code=reason, retryable=reason in ("WORKER_FAILED", "WORKER_TIMEOUT"),
            )
            summary = _error_summary(error)
        else:
            summary = result.summary

        async with self.manager.lock:
            dag = self.manager.load()
            if dag.project_id != project_id:
                raise StateFormatError(f"Project changed during execution of {task_id}")
            task = dag.tasks[self.manager.task_index(dag, task_id)]
            if (task.status != TaskStatus.RUNNING or task.task_revision != execution.task_revision
                    or not task.worker_runs or task.worker_runs[-1].run_id != execution.run_id):
                raise StateFormatError(f"Execution receipt for {task_id} does not match its current run")
            self.bus._append_inbox(make_inbox(
                InboxEventType.TASK_EXECUTION_FINISHED, project_id=project_id, task_id=task_id,
                event_id=f"execution-finished-{execution.run_id}", execution=execution, summary=summary,
                cost_usd=result.cost_usd if result else 0.0, output_artifacts=result.output_artifacts if result else (),
                checks=result.checks if result else (), execution_failure=failure,
            ))
