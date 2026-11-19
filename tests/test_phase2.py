import asyncio

import pytest
from pydantic import ValidationError

from core.bus import EventBus
from core.governance import BudgetExceededError, GovernanceGate
from core.schema import BudgetPolicy, ProjectStatus, TaskDAG, TaskStatus
from core.state_manager import DAGManager
from test_phase1 import task


async def workspace_gate(workspace, *, tasks=None, cost=0.0, max_cost=10.0):
    workspace.mkdir(exist_ok=True)
    manager = DAGManager(workspace)
    bus = EventBus(workspace, manager.lock)
    await bus.initialize()
    await manager.save_atomic(TaskDAG(
        project_id="p1", project_status=ProjectStatus.ACTIVE,
        tasks=[task("T1", status=TaskStatus.RUNNING)] if tasks is None else tasks,
        budget=BudgetPolicy(max_total_cost_usd=max_cost, current_cost_usd=cost, max_task_retries=3),
    ))
    return GovernanceGate(manager, bus)


def test_exhausted_budget_records_an_alert_in_the_dag(tmp_path):
    async def scenario():
        gate = await workspace_gate(tmp_path, cost=10.0)
        async with gate.manager.lock:
            with pytest.raises(BudgetExceededError, match="p1.*budget"):
                gate._enforce_budget(gate.manager.load())
        dag = gate.manager.load()
        assert dag.project_status == ProjectStatus.ALERT
        assert dag.tasks[0].status == TaskStatus.RUNNING
        assert dag.budget_alert.event_id.startswith("budget-alert-")
        assert gate.bus.outbox() == []

    asyncio.run(scenario())


def test_cost_additions_are_atomic_and_negative_cost_is_rejected(tmp_path):
    async def scenario():
        gate = await workspace_gate(tmp_path)
        async def record_cost():
            async with gate.manager.lock:
                gate._record_cost(0.2)

        await asyncio.gather(*(record_cost() for _ in range(50)))
        with pytest.raises(ValidationError):
            async with gate.manager.lock:
                gate._record_cost(-0.01)
        assert gate.load_budget().current_cost_usd == 10.0

    asyncio.run(scenario())
