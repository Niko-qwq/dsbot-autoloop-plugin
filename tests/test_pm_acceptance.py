import asyncio
import json

import pytest

from core.schema import PMTaskCheck, ProjectBinding, ProjectStatus, TaskDAG, TaskStatus, WorkerRun
from core.state_manager import StateFormatError
from runtime.daemon import AgentReply
from test_draft_receipt import _new_plugin, _request
from test_phase1 import task


async def _seed_worker(tmp_path):
    plugin = await _new_plugin(tmp_path)
    binding = ProjectBinding(project_id="proj_review", session_id="chief-session", route=_request().route)
    dag = TaskDAG(project_id=binding.project_id, project_status=ProjectStatus.ACTIVE, binding=binding, tasks=[
        task("T1", status=TaskStatus.RUNNING).model_copy(update={"acceptance": "Verify the generator tests"}),
    ])
    await plugin.manager.save_atomic(dag)
    await plugin.worker.initialize_project(dag.project_id)
    return plugin, binding, dag


def test_pm_checks_read_only_worker_submission_without_changing_the_task(tmp_path, monkeypatch):
    async def scenario():
        plugin, binding, original = await _seed_worker(tmp_path)
        worker_check = PMTaskCheck(criterion_id="TASK", status="FAIL", reason="只读权限，未写入报告。")
        task = original.tasks[0].model_copy(update={
            "status": TaskStatus.PENDING_VERIFY, "task_revision": 2, "permission_tier": 1,
            "acceptance": "需要提交 docs/report.md",
            "worker_runs": (WorkerRun(started_at=1.0, finished_at=2.0, status="COMPLETED", run_id="run-1",
                                      execution_checks=(worker_check,)),),
        })
        dag = original.model_copy(update={"tasks": [task]})
        await plugin.manager.save_atomic(dag)
        claim = {"run_id": "run-1", "task_revision": 2, "criteria": {"TASK": task.acceptance},
                 "summary": "只读权限未生成文件。\n" + "报" * 10_000}

        async def accept(project_id, directory, route, prompt, budget, draft, *, acceptance):
            assert acceptance is True and not draft and budget == 10.0
            assert (project_id, route) == (dag.project_id, binding.route)
            assert directory == plugin.manager.project_directory(project_id)
            assert not plugin.manager.lock.locked()
            context = json.loads(prompt.splitlines()[-1])
            assert context["mode"] == "task_acceptance"
            assert context["criteria"] == claim["criteria"]
            assert context["worker_checks"] == [worker_check.model_dump(mode="json")]
            assert context["evidence"]["output_artifacts"] == []
            assert len(context["evidence"]["summary"].encode("utf-8")) <= 4096
            return AgentReply(output=json.dumps({
                "verdict": "REJECTED", "disposition": "BLOCKED_PERMISSION", "reason": "需要写权限交付报告。",
                "checks": [worker_check.model_dump(mode="json")],
            }), cost_usd=0.125)

        monkeypatch.setattr(plugin.pm, "call", accept)
        verdict = await plugin.pm.accept_task(dag.project_id, "T1", claim)
        assert verdict.disposition == "BLOCKED_PERMISSION"
        assert plugin.gate.load_budget().current_cost_usd == 0.125
        assert plugin.manager.load().tasks == dag.tasks
        with pytest.raises(StateFormatError, match="attempt changed"):
            await plugin.pm.accept_task(dag.project_id, "T1", {**claim, "run_id": "old-run"})
        assert plugin.gate.load_budget().current_cost_usd == 0.125

    asyncio.run(scenario())
