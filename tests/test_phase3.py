import asyncio
import pytest
from pydantic import ValidationError

from core.schema import InboxEventType, OutboxEventType, ProjectStatus, TaskStatus
from runtime.daemon import AutoloopDaemon
from runtime.router import Router
from runtime.watchdog import Watchdog, WorkerCleanupError, WorkerResult
from test_phase1 import task
from test_phase2 import workspace_gate


async def worker_skills(tmp_path, gate):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "test.md").write_text("Test skill", encoding="utf-8")
    dag = gate.manager.load()
    dag.goal_tree = "Known project plan\n"
    await gate.manager.save_atomic(dag)
    return skills


async def unused(*_args):
    raise AssertionError("No PM call was expected")


def test_workers_use_three_slots_and_receive_only_read_only_context(tmp_path):
    async def scenario():
        gate = await workspace_gate(tmp_path / "workspace", tasks=[
            task(f"T{index}", status=TaskStatus.READY) for index in range(5)
        ])
        skills = await worker_skills(tmp_path, gate)
        started = []
        release = asyncio.Event()

        async def worker(node, skill, project_state):
            assert skill == "Test skill" and "Known project plan" in project_state
            with pytest.raises(ValidationError, match="frozen_instance"):
                node.title = "change"
            started.append(node.task_id)
            await release.wait()
            return WorkerResult(cost_usd=0.15, summary="done", output_artifacts=(f"{node.task_id}.md",))

        router = Router(Watchdog(gate), skills, worker)
        first, duplicate = await asyncio.gather(router.dispatch_ready_tasks(), router.dispatch_ready_tasks())
        assert len(first) == 3 and not duplicate and router.active_workers == 3
        assert [node.status for node in gate.manager.load().tasks].count(TaskStatus.RUNNING) == 3
        release.set()
        await asyncio.gather(*first)
        await asyncio.gather(*await router.dispatch_ready_tasks())
        assert len(set(started)) == 5
        events = await gate.bus.pending_inbox()
        assert len(events) == 5 and all(event.type == InboxEventType.TASK_EXECUTION_FINISHED for event in events)
        assert {event.task_id: event.payload.output_artifacts for event in events}["T0"] == ("T0.md",)
        daemon = AutoloopDaemon(router, feedback=unused, task_acceptance=unused)
        for event in events:
            await daemon._apply(event)
        assert gate.load_budget().current_cost_usd == 0.75

    asyncio.run(scenario())


def test_unconfirmed_native_stop_alerts_and_prevents_retry(tmp_path):
    async def scenario():
        gate = await workspace_gate(tmp_path / "workspace", tasks=[task("T1", status=TaskStatus.READY)])
        skills = await worker_skills(tmp_path, gate)

        async def worker(*_args):
            raise WorkerCleanupError("Native stop was not confirmed")

        daemon = AutoloopDaemon(Router(Watchdog(gate), skills, worker), feedback=unused, task_acceptance=unused)
        await asyncio.gather(*await daemon.router.dispatch_ready_tasks())
        await daemon.step()
        dag = gate.manager.load()
        assert dag.project_status == ProjectStatus.ALERT
        assert dag.tasks[0].status == TaskStatus.FAILED
        assert dag.tasks[0].retry_count == 1
        alert, = gate.bus.outbox(dag.project_id)
        assert alert.type == OutboxEventType.ALERT_ESCALATED
        assert alert.reason_code == "WORKER_CLEANUP_FAILED"
        assert not await daemon.router.dispatch_ready_tasks()

    asyncio.run(scenario())
