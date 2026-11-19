import asyncio
import json

import pytest
from pydantic import ValidationError

from core.bus import EventBus, make_inbox
from core.schema import (
    InboxEvent, InboxEventType, PMTaskCheck, PMTaskVerdict, ProjectStatus,
    Task, TaskAcceptanceReceipt, TaskDAG, TaskStatus, WorkerRun,
)
from core.state_manager import CyclicDAGError, DAGManager, StateFormatError


async def publish(bus, event):
    async with bus.lock:
        bus._append_outbox(event)


def task(task_id, dependencies=(), status=TaskStatus.PENDING):
    runs = ()
    if status == TaskStatus.COMPLETED:
        verdict = PMTaskVerdict(verdict="VERIFIED", reason="Evidence checked", checks=(
            PMTaskCheck(criterion_id="TASK", status="PASS", reason="Verified the result", evidence_refs=("worker result",)),
        ))
        runs = (WorkerRun(started_at=1.0, finished_at=2.0, status="COMPLETED", run_id=f"run-{task_id}",
                          execution_event_id=f"execution-{task_id}", detail="Verified the result",
                          acceptance=TaskAcceptanceReceipt(event_id=f"verified-{task_id}", verdict=verdict)),)
    return Task(
        task_id=task_id, title=f"Task {task_id}", skill_ref="test", dependencies=dependencies,
        status=status, retry_count=0, permission_tier=1, output_artifacts=(), error_msg=None, worker_runs=runs,
    )


def test_four_node_dag_unlocks_only_ready_dependencies(tmp_path):
    async def scenario():
        manager = DAGManager(tmp_path)
        dag = TaskDAG(project_id="p1", project_status=ProjectStatus.ACTIVE, tasks=[
            task("T1", status=TaskStatus.COMPLETED), task("T2", ("T1",)),
            task("T3", ("T2",)), task("T4", ("T1", "T3")),
        ])
        verified = {node.task_id for node in dag.tasks if node.pm_verified}
        assert [node.task_id for node in manager.promote_ready(dag, verified)] == ["T2"]
        await manager.save_atomic(dag)
        assert [node.status for node in manager.load().tasks] == [
            TaskStatus.COMPLETED, TaskStatus.READY, TaskStatus.PENDING, TaskStatus.PENDING,
        ]
        assert manager.promote_ready(manager.load(), verified) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(("tasks", "error_type"), [
    ([task("T1", ("T2",)), task("T2", ("T1",))], CyclicDAGError),
    ([task("T1", ("absent",))], StateFormatError),
    ([task("T1"), task("T1")], StateFormatError),
])
def test_invalid_graph_is_rejected_on_load(tmp_path, tasks, error_type):
    manager = DAGManager(tmp_path)
    dag = TaskDAG(project_id="p1", project_status=ProjectStatus.ACTIVE, tasks=tasks)
    manager.dag_path.write_text(dag.model_dump_json(), encoding="utf-8")
    with pytest.raises(error_type, match="TASK_DAG.json"):
        manager.load()


def test_contract_rejects_unknown_fields_and_coercion():
    fields = task("T1").model_dump(mode="json")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Task.model_validate_json(json.dumps({**fields, "unknown": 1}))
    with pytest.raises(ValidationError, match="int_type"):
        Task.model_validate_json(json.dumps({**fields, "retry_count": "0"}))
    assert Task.model_validate_json(json.dumps(fields)).task_id == "T1"


def test_concurrent_jsonl_writes_and_individual_acknowledgements_are_lossless(tmp_path):
    async def scenario():
        bus = EventBus(tmp_path)
        await bus.initialize()
        events = [make_inbox(
            InboxEventType.USER_FEEDBACK, project_id="p1", event_id=f"evt_{index}", summary="完成\u2028测试",
        ) for index in range(50)]
        await asyncio.gather(*(bus.append_inbox(event) for event in events))
        pending = await bus.pending_inbox()
        assert {event.event_id for event in pending} == {event.event_id for event in events}
        assert all(not event.consumed for event in pending)
        async with bus.lock:
            for event in pending:
                bus._ack_inbox(event.event_id)
            bus._ack_inbox(pending[0].event_id)
        assert await bus.pending_inbox() == []
        with bus.inbox_path.open(encoding="utf-8") as stream:
            saved = [InboxEvent.model_validate_json(line) for line in stream]
        assert len(saved) == 50 and all(event.consumed for event in saved)
        assert all(event.payload.summary == "完成\u2028测试" for event in saved)
        assert not list(tmp_path.glob("*.tmp"))

    asyncio.run(scenario())


def test_bad_jsonl_does_not_get_rewritten(tmp_path):
    async def scenario():
        bus = EventBus(tmp_path)
        await bus.initialize()
        bus.inbox_path.write_text("{\n", encoding="utf-8")
        with pytest.raises(StateFormatError, match="inbox.jsonl:1"):
            await bus.pending_inbox()
        assert bus.inbox_path.read_text(encoding="utf-8") == "{\n"

    asyncio.run(scenario())
