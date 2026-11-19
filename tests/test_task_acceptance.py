import asyncio
import tempfile
import unittest
from pathlib import Path

from core.bus import EventBus, make_inbox
from core.governance import BudgetExceededError, GovernanceGate
from core.schema import (
    InboxEventType, OutboxEventType, PMTaskCheck, PMTaskVerdict,
    ProjectStatus, Task, TaskDAG, TaskRunRef, TaskStatus, WorkerRun,
)
from core.state_manager import DAGManager, StateFormatError, UneditableTaskError
from runtime.daemon import AutoloopDaemon
from runtime.router import Router
from runtime.watchdog import Watchdog, WorkerResult
from test_phase3 import unused


def node(task_id, dependencies=()):
    return Task(task_id=task_id, title=f"Deliver {task_id}", skill_ref="test", dependencies=dependencies,
                status=TaskStatus.PENDING if dependencies else TaskStatus.READY, retry_count=0,
                permission_tier=1, output_artifacts=(), error_msg=None)


def passed(evidence):
    return PMTaskVerdict(verdict="VERIFIED", reason="Checked project evidence", checks=tuple(
        PMTaskCheck(criterion_id=key, status="PASS", evidence_refs=("worker summary",), reason="Checked the cited result")
        for key in evidence["criteria"]
    ))


class TaskAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        self.skills = root / "skills"
        self.skills.mkdir()
        (self.skills / "test.md").write_text("Test skill", encoding="utf-8")
        workspace = root / "workspace"
        workspace.mkdir()
        self.manager = DAGManager(workspace)
        self.bus = EventBus(workspace, self.manager.lock)
        await self.bus.initialize()
        self.gate = GovernanceGate(self.manager, self.bus)
        await self.manager.save_atomic(TaskDAG(project_id="p1", project_status=ProjectStatus.ACTIVE,
                                               tasks=[node("T1"), node("T2", ("T1",))]))
        self.runs = []

    async def worker(self, task, *_):
        self.runs.append(task.worker_runs[-1].run_id)
        return WorkerResult(cost_usd=0, summary="Worker submitted text-only evidence")

    async def review(self, _project, _task, evidence):
        return passed(evidence)

    def daemon(self, review=None, worker=None):
        return AutoloopDaemon(Router(Watchdog(self.gate), self.skills, worker or self.worker),
                              feedback=unused, task_acceptance=review or self.review)

    def notices(self, kind):
        return [event for event in self.bus.outbox("p1") if event.type == kind]

    async def test_worker_submission_does_not_unlock_until_pm_accepts(self):
        release = asyncio.Event()

        async def review(_project, _task, evidence):
            await release.wait()
            return passed(evidence)

        daemon = self.daemon(review)
        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        self.assertEqual([task.status for task in self.manager.load().tasks], [TaskStatus.PENDING_VERIFY, TaskStatus.PENDING])
        self.assertFalse(self.notices(OutboxEventType.PROJECT_COMPLETED))
        release.set()
        await daemon._acceptance_job
        await daemon.step()
        self.assertEqual(self.manager.load().tasks[1].status, TaskStatus.RUNNING)
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        await daemon._acceptance_job
        await daemon.step()
        await daemon.step()
        complete, = self.notices(OutboxEventType.PROJECT_COMPLETED)
        self.assertTrue(complete.report.delivery_verified)
        self.assertEqual(complete.report.artifacts, [])
        self.assertEqual(self.manager.load().project_status, ProjectStatus.COMPLETED)
        self.assertEqual(len(self.runs), 2)

        # Simulate the disk boundary after the complete DAG commit and before Outbox append.
        self.bus.outbox_path.write_text("", encoding="utf-8")
        restart = self.daemon(review)
        await restart.recover()
        await restart.step()
        rebuilt, = self.notices(OutboxEventType.PROJECT_COMPLETED)
        self.assertEqual((rebuilt.event_id, rebuilt.report), (complete.event_id, complete.report))
        self.assertEqual(len(self.runs), 2)

    async def test_permission_block_escalates_once_and_cannot_be_retried(self):
        async def review(_project, _task, _evidence):
            return PMTaskVerdict(verdict="REJECTED", reason="Read-only Worker cannot write docs/report.md",
                                 disposition="BLOCKED_PERMISSION", checks=(
                                     PMTaskCheck(criterion_id="TASK", status="FAIL", reason="Report is missing"),
                                 ))

        daemon = self.daemon(review)
        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        await daemon._acceptance_job
        verdict, = await self.bus.pending_inbox()
        self.assertEqual(verdict.type, InboxEventType.TASK_REJECTED)
        await self.bus.append_inbox(verdict)
        with self.assertRaises(StateFormatError):
            await self.bus.append_inbox(verdict.model_copy(update={"task_id": "T2"}))
        await daemon.step()
        restarted = self.daemon(review)
        await restarted.recover()
        await restarted.step()
        alert, = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertEqual(alert.reason_code, "BLOCKED_PERMISSION")
        await self.bus.append_inbox(make_inbox(InboxEventType.TASK_RETRY, project_id="p1", task_id="T1",
                                              action_ref=alert.event_id))
        await restarted.step()
        self.assertEqual([task.status for task in self.manager.load().tasks], [TaskStatus.FAILED, TaskStatus.BLOCKED])
        self.assertEqual(len(self.runs), 1)

    async def test_execution_receipt_survives_restart_before_dag_update(self):
        daemon = self.daemon()
        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        receipt, = await self.bus.pending_inbox()
        self.assertEqual(receipt.type, InboxEventType.TASK_EXECUTION_FINISHED)
        restarted = self.daemon()
        await restarted.recover()
        await restarted.step()
        await restarted._acceptance_job
        await restarted.step()
        self.assertTrue(self.manager.load().tasks[0].pm_verified)
        await asyncio.gather(*restarted.router._jobs.values())
        self.assertEqual(len(self.runs), 2)

    async def test_only_confirmed_pm_plan_can_resolve_settled_permission_block(self):
        async def review(_project, task_id, evidence):
            dag = self.manager.load()
            task = dag.tasks[self.manager.task_index(dag, task_id)]
            if task.permission_tier == 1:
                return PMTaskVerdict(verdict="REJECTED", reason="Write access is required", disposition="BLOCKED_PERMISSION",
                                     checks=(PMTaskCheck(criterion_id="TASK", status="FAIL", reason="Missing file"),))
            return passed(evidence)

        daemon = self.daemon(review)
        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        await daemon._acceptance_job
        await daemon.step()
        blocked = self.manager.load()
        task = blocked.tasks[0]
        self.assertTrue(self.manager.task_is_amendable(task))
        amended = node("T1").model_copy(update={"permission_tier": 2})
        fingerprint = self.manager.plan_fingerprint(blocked)
        async with self.manager.lock:
            with self.assertRaises(UneditableTaskError):
                self.manager.commit_task_edit("p1", "feedback", "Changed permissions", "INBOX", [amended], {},
                                              expected_fingerprint=fingerprint, source_ref="issue")
            with self.assertRaises(UneditableTaskError):
                self.manager.commit_task_edit("p1", "no-confirmation", "Changed permissions", "PM_CHAT", [amended], {})
            with self.assertRaises(UneditableTaskError):
                self.manager.edit_tasks(blocked, [amended.model_copy(update={"permission_tier": 3})], {}, allow_pm_amendments=True)
            approved = self.manager.commit_task_edit("p1", "pm-confirmed", "Approved local writes", "PM_CHAT", [amended], {},
                                                       expected_fingerprint=fingerprint, source_ref="project-pm-session")
        self.assertEqual(approved.tasks[0].worker_runs, task.worker_runs)
        self.assertEqual(approved.tasks[0].task_revision, task.task_revision + 1)
        self.assertEqual(approved.tasks[1].status, TaskStatus.BLOCKED)

        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        await daemon._acceptance_job
        await daemon.step()
        self.assertTrue(self.manager.load().tasks[0].pm_verified)
        self.assertEqual(self.manager.load().tasks[1].status, TaskStatus.RUNNING)
        await asyncio.gather(*daemon.router._jobs.values())

    async def test_worker_execution_failure_retries_and_preserves_final_alert_after_restart(self):
        async def failed_worker(task, *_):
            self.runs.append(task.worker_runs[-1].run_id)
            raise RuntimeError("worker failed")

        async def unused_review(*_):
            self.fail("An execution failure cannot request PM acceptance")

        daemon = self.daemon(review=unused_review, worker=failed_worker)
        await daemon.step()
        for _ in range(2):
            await asyncio.gather(*daemon.router._jobs.values())
            failure, = await self.bus.pending_inbox()
            self.assertTrue(failure.execution_failure.retryable)
            await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        final, = await self.bus.pending_inbox()
        self.assertEqual(final.execution_failure.reason_code, "WORKER_FAILED")
        restarted = self.daemon(review=unused_review, worker=failed_worker)
        await restarted.recover()
        await restarted.step()
        alert, = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertEqual(alert.reason_code, "WORKER_FAILED")
        self.assertEqual(len(set(self.runs)), 3)
        self.assertEqual([task.status for task in self.manager.load().tasks], [TaskStatus.FAILED, TaskStatus.BLOCKED])

    async def test_budget_alert_is_rebuilt_from_project_state_after_restart(self):
        async with self.manager.lock:
            self.gate._record_cost(10)
        daemon = self.daemon()
        await daemon.step()
        paused = self.manager.load()
        self.assertEqual(paused.project_status, ProjectStatus.ALERT)
        self.bus.outbox_path.write_text("", encoding="utf-8")
        restarted = self.daemon()
        await restarted.recover()
        await restarted.step()
        await restarted.step()
        alert, = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertEqual((alert.event_id, alert.task_id, alert.reason_code),
                         (paused.budget_alert.event_id, None, "BUDGET_EXHAUSTED"))
        self.assertFalse(self.runs)

    async def test_worker_budget_failure_waits_for_explicit_project_resume(self):
        async def worker(task, *_):
            self.runs.append(task.worker_runs[-1].run_id)
            async with self.manager.lock:
                self.gate._record_cost(10)
            raise BudgetExceededError("The active Worker exhausted the project budget")

        daemon = self.daemon(worker=worker)
        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        await daemon.step()
        alert, = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertEqual((alert.task_id, alert.reason_code), (None, "BUDGET_EXHAUSTED"))
        paused = self.manager.load()
        self.assertEqual((paused.project_status, paused.tasks[0].status, paused.tasks[0].retry_count),
                         (ProjectStatus.ALERT, TaskStatus.READY, 0))
        self.assertEqual(len(self.runs), 1)

    async def test_failed_pm_provider_retry_verifies_the_same_submission(self):
        available = False

        async def review(_project, _task, evidence):
            if not available:
                raise RuntimeError("PM input exceeded the model budget")
            return passed(evidence)

        daemon = self.daemon(review)
        await daemon.step()
        await asyncio.gather(*daemon.router._jobs.values())
        await daemon.step()
        for _ in range(self.gate.load_budget().max_task_retries):
            await daemon._acceptance_job
            await daemon.step()
        first_alert, = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertEqual(first_alert.reason_code, "PM_ACCEPTANCE_FAILED")
        first_retry = make_inbox(InboxEventType.TASK_RETRY, project_id="p1", task_id="T1", action_ref=first_alert.event_id)
        await self.bus.append_inbox(first_retry)
        await daemon.step()
        for _ in range(self.gate.load_budget().max_task_retries):
            await daemon._acceptance_job
            await daemon.step()
        old, current = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertNotEqual(old.event_id, current.event_id)
        self.assertEqual(self.runs, [self.manager.load().tasks[0].worker_runs[-1].run_id])

        available = True
        await self.bus.append_inbox(make_inbox(InboxEventType.TASK_RETRY, project_id="p1", task_id="T1",
                                              action_ref=current.event_id))
        await daemon.step()
        await daemon._acceptance_job
        await daemon.step()
        self.assertTrue(self.manager.load().tasks[0].pm_verified)
        self.assertEqual(len(self.manager.load().tasks[0].worker_runs), 1)
        await asyncio.gather(*daemon.router._jobs.values())

    async def test_stale_verdict_and_unconfirmed_execution_do_not_start_another_worker(self):
        started = asyncio.Event()
        dag = self.manager.load()
        dag.tasks[0] = dag.tasks[0].model_copy(update={"permission_tier": 2})
        await self.manager.save_atomic(dag)

        async def paused_worker(task, *_):
            self.runs.append(task.worker_runs[-1].run_id)
            started.set()
            await asyncio.Event().wait()

        daemon = self.daemon(worker=paused_worker)
        await daemon.step()
        await started.wait()
        await daemon.router.stop()
        run_id, = self.runs
        dag = self.manager.load()
        dag.tasks.extend((node("T3"), node("T4").model_copy(update={
            "status": TaskStatus.PENDING_VERIFY,
            "worker_runs": (WorkerRun(run_id="finished-run", started_at=1, finished_at=2,
                                      status="COMPLETED", execution_event_id="finished-receipt"),),
        })))
        await self.manager.save_atomic(dag)
        await self.bus.append_inbox(make_inbox(
            InboxEventType.TASK_VERIFIED, project_id="p1", task_id="T1", event_id="stale",
            execution=TaskRunRef(run_id=run_id, task_revision=2), verdict=passed({"criteria": {"TASK": "old"}}),
        ))
        restarted = self.daemon(worker=paused_worker)
        await restarted.recover()
        await restarted.step()
        interrupted = [TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.PENDING_VERIFY]
        self.assertEqual([task.status for task in self.manager.load().tasks], interrupted)
        self.assertIsNone(restarted._acceptance_job)
        alert, = self.notices(OutboxEventType.ALERT_ESCALATED)
        self.assertEqual(alert.reason_code, "WORKER_INTERRUPTED")
        self.assertEqual(self.runs, [run_id])
        await self.bus.append_inbox(make_inbox(InboxEventType.TASK_RETRY, project_id="p1", task_id="T1",
                                              action_ref=alert.event_id))
        await restarted.step()
        self.assertEqual([task.status for task in self.manager.load().tasks], interrupted)
        self.assertIsNone(restarted._acceptance_job)
        await self.bus.append_inbox(make_inbox(InboxEventType.TASK_RETRY, project_id="p1", task_id="T1",
                                              action_ref=alert.event_id, confirm_worker_stopped=True))
        await restarted.step()
        tasks = self.manager.load().tasks
        self.assertEqual((tasks[0].status, tasks[2].status), (TaskStatus.RUNNING, TaskStatus.RUNNING))
        self.assertIsNotNone(restarted._acceptance_job)
        await restarted._acceptance_job
        await restarted.router.stop()
