import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from uuid import uuid4

from config.constants import MAX_CONCURRENT_WORKERS, MAX_CONCURRENT_WRITERS
from core.governance import BudgetExceededError
from core.schema import ProjectStatus, Task, TaskRunRef, TaskStatus, WorkerRun
from core.state_manager import StateFormatError, workspace_has_unconfirmed_writer
from runtime.watchdog import Watchdog, WorkerResult


Worker = Callable[[Task, str, str], Awaitable[WorkerResult]]


class Router:
    def __init__(self, watchdog: Watchdog, skills: str | Path, worker: Worker):
        self.watchdog = watchdog
        self.manager = watchdog.manager
        self.skills = Path(skills).resolve()
        self.worker = worker
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._writers: set[str] = set()

    @property
    def active_workers(self) -> int:
        return sum(not job.done() for job in self._jobs.values())

    def _reap(self) -> None:
        for task_id, job in list(self._jobs.items()):
            if job.done():
                del self._jobs[task_id]
                self._writers.discard(task_id)
                job.result()

    async def dispatch_ready_tasks(self) -> list[asyncio.Task[None]]:
        async with self.manager.lock:
            self._reap()
            dag = self.manager.load()
            if dag.project_status != ProjectStatus.ACTIVE or workspace_has_unconfirmed_writer(dag):
                return []
            slots = MAX_CONCURRENT_WORKERS - len(self._jobs)
            writers = MAX_CONCURRENT_WRITERS - len(self._writers)
            verified = {task.task_id for task in dag.tasks if task.pm_verified}
            promoted = self.manager.promote_ready(dag, verified)
            selected = []
            for index, task in enumerate(dag.tasks):
                if slots <= 0:
                    break
                if (task.status != TaskStatus.READY or task.task_id in self._jobs
                        or not verified.issuperset(task.dependencies)):
                    continue
                if task.permission_tier >= 2:
                    if writers <= 0:
                        continue
                    writers -= 1
                selected.append(index)
                slots -= 1
            if not selected:
                if promoted:
                    self.manager._save(dag)
                return []
            try:
                self.watchdog.gate._enforce_budget(dag)
            except BudgetExceededError:
                return []
            for index in selected:
                task = dag.tasks[index]
                run = WorkerRun(started_at=time.time(), run_id=uuid4().hex)
                dag.tasks[index] = task.model_copy(update={"status": TaskStatus.RUNNING, "worker_runs": (*task.worker_runs, run)})
            self.manager._save(dag)
            jobs = []
            for index in selected:
                task = dag.tasks[index]
                execution = TaskRunRef(run_id=task.worker_runs[-1].run_id, task_revision=task.task_revision)
                job = asyncio.create_task(
                    self.watchdog.run(dag.project_id, task.task_id, execution, partial(self._invoke, task, dag.goal_tree or "")),
                    name=f"autoloop:{task.task_id}",
                )
                self._jobs[task.task_id] = job
                if task.permission_tier >= 2:
                    self._writers.add(task.task_id)
                jobs.append(job)
            return jobs

    async def _invoke(self, task: Task, goal: str) -> WorkerResult:
        template = (self.skills / f"{task.skill_ref}.md").resolve()
        if template.parent != self.skills:
            raise StateFormatError(f"Skill is outside {self.skills}: {task.skill_ref!r}")
        return await self.worker(task, template.read_text(encoding="utf-8"), goal)

    async def stop(self) -> None:
        for job in self._jobs.values():
            job.cancel()
        try:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
            for job in self._jobs.values():
                if not job.cancelled():
                    job.result()
        finally:
            self._jobs.clear()
            self._writers.clear()
