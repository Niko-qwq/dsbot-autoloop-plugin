import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.schema import ProjectStatus, TaskStatus
from plugin_entry import VersionPlans


def test_paused_revision_retires_only_after_worker_cleanup_is_confirmed():
    product = {"repository_id": 41}
    version = {"version": "v0.1.0", "revision": None, "project_id": "legacy-plan"}
    plans = VersionPlans(None)
    runtime = Mock()
    runtime.router.active_workers = {}
    runtime.daemon._acceptance_job = None
    run = SimpleNamespace(escalation=SimpleNamespace(reason_code="WORKER_CLEANUP_FAILED"))
    dag = SimpleNamespace(project_id=version["project_id"], project_status=ProjectStatus.PAUSED,
                          tasks=[SimpleNamespace(status=TaskStatus.READY, worker_runs=[run])])
    runtime.manager.load.return_value = dag
    key = (41, "v0.1.0", 1)
    plans.runtimes[key] = runtime
    watchers = (Mock(), Mock())
    plans.jobs[runtime] = watchers

    async def check():
        runtime.manager.lock = asyncio.Lock()
        assert await plans.retire(product, version) is False
        assert runtime not in plans.retired
        assert plans.jobs[runtime] == watchers
        for watcher in watchers:
            watcher.cancel.assert_not_called()

        dag.tasks.clear()
        assert await plans.retire(product, version) is True
        assert runtime in plans.retired
        assert runtime not in plans.jobs
        for watcher in watchers:
            watcher.cancel.assert_called_once()

    asyncio.run(check())


def test_restore_keeps_historical_revision_numbers_separate_from_active_revision():
    versions = {"v0.1.0": {"version": "v0.1.0", "revision": 3, "project_id": "active", "history": [
        {"project_id": "first"}, {"project_id": "second", "revision": 2},
    ]}}
    product = {"repository_id": 41, "versions": versions}
    plans = VersionPlans(None)
    plans.service = SimpleNamespace(store=SimpleNamespace(read=lambda: {"products": {"41": product}}))
    observed = []

    async def runtime(current_product, version, *, historical=False):
        observed.append((version["project_id"], plans.key(current_product, version)[2], historical))

    plans.runtime = runtime
    asyncio.run(plans.restore(None))
    assert observed == [("first", 1, True), ("second", 2, True), ("active", 3, False)]


def test_workflow_planning_uses_product_labels_and_preserves_explicit_scope():
    plans = VersionPlans(None)
    runtime = SimpleNamespace(prepare=AsyncMock(return_value={"project_id": "new-plan"}))
    plans.runtime = AsyncMock(return_value=runtime)
    product = {"chief_session": "test-chief", "grant": {"budget": 1}}
    version = {"version": "v0.1.0", "base": None}
    goal = "现有清单固定标签依次为「核对原始素材」「复核交付结果」，仅修复打包。"
    route = {"provider": "test", "model": "test", "reasoning_effort": None, "input_rate": 0,
             "output_rate": 0, "cache_read_rate": 0, "cache_write_rate": 0}

    asyncio.run(plans.prepare(product, version, {"goal": goal, "route": route}))
    request = runtime.prepare.await_args.args[0]
    assert request.brief.user_instruction == goal
    assert request.goal.startswith(goal)
    assert "tool.labels 使用产品简报和经 PM 确认的来源中的实际标签" in request.goal
    assert "仅更改本轮明确要求的范围" in request.goal
    assert "检查输入" not in request.goal and "核对结果" not in request.goal
