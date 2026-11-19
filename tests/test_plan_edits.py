import asyncio

import pytest

from core.schema import ProjectStatus, TaskDAG, TaskStatus, WorkerRun
from core.state_manager import CyclicDAGError, DAGManager, StalePlanEditError, StateFormatError, UneditableTaskError
from test_phase1 import task


def test_merge_updates_edges_and_audit_without_touching_running_work(tmp_path):
    async def scenario():
        manager = DAGManager(tmp_path)
        source = task("A", ("P",)).model_copy(update={"source_issue_ids": ("issue-A",)})
        target = task("B", status=TaskStatus.READY).model_copy(update={"source_issue_ids": ("issue-B",)})
        dependent = task("C", ("A", "B"))
        running = task("R", status=TaskStatus.RUNNING).model_copy(update={"worker_runs": (WorkerRun(run_id="fixture-run", started_at=1),)})
        dag = TaskDAG(project_id="p1", project_status=ProjectStatus.PAUSED, tasks=[task("P"), source, target, dependent, running])
        await manager.save_atomic(dag)
        before = manager.dag_path.read_bytes()

        prospective = manager.edit_tasks(dag, [], {"A": "B"})
        assert manager.dag_path.read_bytes() == before
        assert dag.tasks[1:4] == [source, target, dependent]
        assert prospective.tasks[1].dependencies == ("P",)
        assert prospective.tasks[1].status == TaskStatus.PENDING
        assert prospective.tasks[1].source_issue_ids == ("issue-B", "issue-A")
        assert prospective.tasks[2].dependencies == ("B",)

        fingerprint = manager.plan_fingerprint(dag)
        async with manager.lock:
            edited = manager.commit_task_edit(
                "p1", "edit-1", "Combine overlapping tasks", "PM_CHAT", [], {"A": "B"},
                expected_fingerprint=fingerprint, source_ref="proposal-1",
            )
            save = manager.dag_path.read_bytes()
            assert manager.commit_task_edit(
                "p1", "edit-1", "Combine overlapping tasks", "PM_CHAT", [], {"A": "B"}, expected_fingerprint=fingerprint,
            ) == edited
            assert manager.dag_path.read_bytes() == save
            with pytest.raises(StateFormatError, match="Retired task IDs"):
                manager.commit_task_edit("p1", "edit-2", "Reuse the ID", "INBOX", [task("A")], {})
            assert manager.dag_path.read_bytes() == save

        assert manager.load() == edited
        assert edited.project_status == ProjectStatus.PAUSED and edited.tasks[-1] == running
        receipt, = edited.plan_edits
        assert (receipt.id, receipt.source, receipt.source_ref) == ("edit-1", "PM_CHAT", "proposal-1")
        assert receipt.timestamp > 0
        assert (receipt.added, receipt.updated, receipt.removed) == ((), ("B", "C"), ("A",))
        assert edited.start_approval_base.startswith("start-p1-pm-")
        assert edited.completion_event_id == "completed-p1"

    asyncio.run(scenario())


def test_plan_fingerprint_changes_with_goal_and_receipt_but_ignores_worker_progress(tmp_path):
    async def scenario():
        manager = DAGManager(tmp_path)
        running = task("R", status=TaskStatus.RUNNING)
        dag = TaskDAG(project_id="p1", project_status=ProjectStatus.ACTIVE, tasks=[task("A"), running])
        await manager.save_atomic(dag)
        fingerprint = manager.plan_fingerprint(dag)
        progress = running.model_copy(update={
            "status": TaskStatus.COMPLETED, "output_artifacts": ("result.txt",), "retry_count": 1,
            "worker_runs": (WorkerRun(run_id="fixture-run", started_at=1, finished_at=2, status="COMPLETED"),),
        })
        dag.tasks[-1] = progress
        await manager.save_atomic(dag)
        assert manager.plan_fingerprint(manager.load()) == fingerprint

        dag.goal_tree = "# Changed goal"
        await manager.save_atomic(dag)
        async with manager.lock:
            with pytest.raises(StalePlanEditError):
                manager.commit_task_edit("p1", "edit-1", "Revise plan", "INBOX", [], {}, expected_fingerprint=fingerprint)
            fingerprint = manager.plan_fingerprint(manager.load())
            updated = manager.commit_task_edit(
                "p1", "edit-2", "Feedback addressed by current plan", "INBOX", [], {},
                expected_fingerprint=fingerprint, event_ids=("issue-1",),
            )
        assert updated.tasks[-1] == progress
        assert updated.applied_feedback == ("issue-1",)
        assert updated.plan_edits[-1].event_ids == ("issue-1",)
        assert manager.plan_fingerprint(updated) != fingerprint
        assert updated.start_approval_base == "start-p1"
        assert updated.completion_event_id == "completed-p1"

    asyncio.run(scenario())


def test_plan_rejects_attempt_history_started_descendants_and_invalid_graph(tmp_path):
    manager = DAGManager(tmp_path)
    history = task("C", ("B",), status=TaskStatus.READY).model_copy(update={"worker_runs": (WorkerRun(run_id="fixture-run", started_at=1),)})
    dag = TaskDAG(project_id="p1", project_status=ProjectStatus.ACTIVE, tasks=[task("A"), task("B", ("A",)), history])

    assert not manager.task_is_editable(history)
    with pytest.raises(UneditableTaskError, match="after an attempt"):
        manager.edit_tasks(dag, [task("C", ("B",))], {})
    with pytest.raises(UneditableTaskError, match="downstream task 'C'"):
        manager.edit_tasks(dag, [task("A").model_copy(update={"title": "Changed"})], {})
    with pytest.raises(StateFormatError, match="still depends on removed task 'A'"):
        manager.edit_tasks(dag, [], {"A": None})
    with pytest.raises(CyclicDAGError):
        manager.edit_tasks(dag, [task("A", ("B",))], {})
    with pytest.raises(StateFormatError, match="at least one task"):
        manager.edit_tasks(dag.model_copy(update={"tasks": [task("D")]}), [], {"D": None})

    dag.tasks[1] = dag.tasks[1].model_copy(update={"source_issue_ids": ("prior",)})
    dag.tasks[-1] = task("C", ("B",))
    replacement = task("B").model_copy(update={"source_issue_ids": ("new", "prior")})
    edited = manager.edit_tasks(dag, [replacement], {"A": None})
    assert [entry.task_id for entry in edited.tasks] == ["B", "C"]
    assert edited.tasks[0].status == TaskStatus.READY
    assert edited.tasks[0].source_issue_ids == ("prior", "new")


def test_draft_replacement_cannot_restore_retired_ids(tmp_path):
    async def scenario():
        manager = DAGManager(tmp_path)
        await manager.save_atomic(TaskDAG(project_id="p1", project_status=ProjectStatus.IDLE,
                                          tasks=[task("A"), task("B")], goal_tree="# Original goal"))
        async with manager.lock:
            manager.commit_task_edit("p1", "edit-1", "Remove B", "PM_CHAT", [], {"B": None})
            committed = manager.dag_path.read_bytes()
            with pytest.raises(StateFormatError, match="Retired task IDs"):
                manager.replace_plan("p1", ("issue-1",), "# Replacement goal", [task("B")])
        assert manager.dag_path.read_bytes() == committed
        assert manager.load().goal_tree == "# Original goal"

    asyncio.run(scenario())


@pytest.mark.parametrize(("status", "expected"), [
    (ProjectStatus.IDLE, ProjectStatus.IDLE), (ProjectStatus.PAUSED, ProjectStatus.PAUSED),
    (ProjectStatus.ALERT, ProjectStatus.ALERT), (ProjectStatus.COMPLETED, ProjectStatus.ACTIVE),
])
def test_task_edit_keeps_project_gate_except_new_work_reopens_completed(tmp_path, status, expected):
    async def scenario():
        manager = DAGManager(tmp_path)
        dag = TaskDAG(project_id="p1", project_status=status, tasks=[task("A", status=TaskStatus.COMPLETED)])
        await manager.save_atomic(dag)
        async with manager.lock:
            noop = manager.commit_task_edit("p1", "edit-1", "Existing coverage", "INBOX", [], {}, event_ids=("issue-1",))
            assert noop.project_status == status
            added = manager.commit_task_edit("p1", "edit-2", "Schedule verification", "PM_CHAT", [task("B", ("A",))], {})
        assert added.project_status == expected and added.tasks[-1].status == TaskStatus.READY
        assert added.completion_event_id == ("completed-p1-1" if status == ProjectStatus.COMPLETED else "completed-p1")
        assert "completion_event_id" not in added.model_dump()

    asyncio.run(scenario())
