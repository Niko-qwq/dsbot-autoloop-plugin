import asyncio
import json

import pytest

from core.bus import make_inbox, make_outbox
from core.schema import (
    InboxEventType, OutboxEventType, PlanningProgress, ProjectBinding,
    ProjectStatus, TaskDAG, UIActionType,
)
from core.state_manager import write_atomic
from plugin_entry import DSHModel
from runtime.daemon import AgentReply, ModelCallError
from test_draft_receipt import _new_plugin, _request
from test_phase1 import publish, task


INITIAL_GOAL = "# Project\n\nInitial proposal.\n"
REVISED_GOAL = "# Project\n\n## Approach\n\nAdd different play styles and a score bonus.\n"


async def _seed_review(tmp_path):
    plugin = await _new_plugin(tmp_path)
    project_id = "proj_review"
    request = _request()
    await plugin.manager.save_atomic(TaskDAG(
        project_id=project_id, project_status=ProjectStatus.IDLE, tasks=[task("T1")],
        goal_tree=INITIAL_GOAL,
        binding=ProjectBinding(project_id=project_id, session_id=request.session_id, route=request.route, name="Project"),
    ))
    event = make_inbox(InboxEventType.USER_FEEDBACK, project_id=project_id, summary="Add replayability")
    await plugin.bus.append_inbox(event)
    return plugin, event


def _revision_reply(*, cyclic=False):
    return AgentReply(cost_usd=0.3, output=json.dumps({
        "name": "Project", "goal_tree": REVISED_GOAL,
        "tasks": [{"task_id": "T2", "title": "Create game mechanics", "acceptance": "Bonus updates score",
                   "dependencies": ["T2"] if cyclic else [], "permission_tier": 2, "phase": "IMPLEMENT"}],
    }))


def test_before_confirmation_revision_replaces_plan_and_remains_idle(tmp_path, monkeypatch):
    async def scenario():
        plugin, event = await _seed_review(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def revise(project_id, directory, route, prompt, budget, draft, *, revision=False):
            calls.append(project_id)
            assert directory == plugin.manager.project_directory(project_id)
            assert route == _request().route and draft and revision and budget > 0
            context = json.loads(prompt.splitlines()[-1])
            assert context["mode"] == "revision" and context["issue"] == event.payload.summary
            assert context["goal_tree"] == INITIAL_GOAL and context["dag"]["tasks"][0]["task_id"] == "T1"
            entered.set()
            await release.wait()
            return _revision_reply()

        monkeypatch.setattr(plugin.pm, "call", revise)
        await plugin.daemon.step()
        job = plugin.daemon._feedback_job
        assert job is not None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (await plugin.bus.pending_inbox()) == [event]
            assert plugin.manager.load().tasks[0].task_id == "T1"
            assert plugin.manager.load().tasks[0].phase is None
            assert plugin.router.active_workers == 0
        finally:
            release.set()
            await asyncio.wait_for(job, timeout=5)

        current = plugin.manager.load()
        assert current.project_status == ProjectStatus.IDLE and current.project_id == event.project_id
        assert [node.task_id for node in current.tasks] == ["T2"]
        assert current.tasks[0].phase == "IMPLEMENT"
        assert current.applied_feedback == (event.event_id,)
        assert current.goal_tree == REVISED_GOAL
        assert plugin.gate.load_budget().current_cost_usd == 0.3
        assert not await plugin.bus.pending_inbox()
        await plugin.pm.feedback((event,))
        assert calls == [event.project_id]
        assert not await plugin.router.dispatch_ready_tasks()

    asyncio.run(scenario())


def test_recovery_keeps_durable_revision_failure_and_requires_a_start(tmp_path):
    async def scenario():
        plugin, event = await _seed_review(tmp_path)
        await publish(plugin.bus, make_outbox(
            OutboxEventType.PROJECT_ALERT, project_id=event.project_id,
            event_id=f"feedback-error-{event.event_id}", summary="Test failure",
        ))
        await plugin.daemon.recover()
        assert not await plugin.bus.pending_inbox()
        for action in (UIActionType.PAUSE_PROJECT, UIActionType.RESUME_PROJECT):
            command = make_inbox(InboxEventType.USER_COMMAND, project_id=event.project_id, summary=action.value)
            await plugin.bus.append_inbox(command)
            await plugin.daemon._apply(command)
        assert plugin.manager.load().project_status == ProjectStatus.IDLE
        assert plugin.router.active_workers == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("reuse_event", [False, True])
def test_replacement_cancels_stale_revision_and_only_publishes_latest_plan(tmp_path, monkeypatch, reuse_event):
    async def scenario():
        plugin, first = await _seed_review(tmp_path)
        entered, cancelled = asyncio.Event(), asyncio.Event()
        replacement = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id,
                                 summary="Replace game with website", replace_plan=True,
                                 event_id=first.event_id if reuse_event else None)
        stored_binding = plugin.manager.load().binding
        modes = []

        async def revise(project_id, directory, route, prompt, budget, draft, *, revision=False):
            context = json.loads(prompt.splitlines()[-1])
            modes.append(context["mode"])
            if context["mode"] == "revision":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                return _revision_reply()
            assert context["mode"] == "replacement" and context["issue"] == replacement.payload.summary
            assert context["event_id"] == replacement.event_id and "dag" not in context
            if not reuse_event:
                assert context["superseded_issues"] == [
                    {"event_id": first.event_id, "issue": first.payload.summary, "status": "SUPERSEDED"},
                ]
                assert "issues" not in context
            return AgentReply(cost_usd=0.1, output=json.dumps({
                "name": "Creative website", "goal_tree": "# New website\n",
                "tasks": [{"task_id": "W1", "title": "Create page", "acceptance": "Page loads",
                           "dependencies": [], "permission_tier": 2, "phase": "IMPLEMENT"}],
            }))

        monkeypatch.setattr(plugin.pm, "call", revise)
        await plugin.daemon.step()
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert plugin.daemon.feedback_event_ids == (first.event_id,)
        async with plugin.manager.lock:
            prior = [] if reuse_event else [first.model_copy(update={"consumed": True, "superseded_by": replacement.event_id})]
            plugin.bus._write(plugin.bus.inbox_path, [*prior, replacement])
        await plugin.daemon.step()
        assert cancelled.is_set()
        assert plugin.daemon.feedback_event_ids == (replacement.event_id,)
        assert plugin.daemon._feedback_job is not None
        await asyncio.wait_for(plugin.daemon._feedback_job, timeout=5)
        await plugin.daemon.step()

        dag = plugin.manager.load()
        assert modes == ["revision", "replacement"]
        assert dag.project_id == first.project_id and dag.project_status == ProjectStatus.IDLE
        assert dag.project_name == "Creative website"
        assert dag.goal_tree == "# New website\n"
        assert [item.task_id for item in dag.tasks] == ["W1"]
        assert dag.tasks[0].source_issue_ids == (replacement.event_id,)
        assert dag.applied_feedback == (replacement.event_id,)
        assert plugin.manager.load().binding == stored_binding
        assert not await plugin.bus.pending_inbox()
        prepared, = plugin.bus.outbox()
        assert prepared.type == OutboxEventType.PROJECT_PREPARED
        assert prepared.plan.mode == "REVISION"
        assert prepared.plan.approval_token == f"start-{dag.project_id}-{replacement.event_id}"
        assert plugin.daemon.feedback_event_ids == () and not plugin.router.active_workers

    asyncio.run(scenario())


def test_revision_preserves_existing_issue_links_and_checks_planner_references(tmp_path, monkeypatch):
    async def scenario():
        plugin, first = await _seed_review(tmp_path)
        applied = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, event_id="older-applied")
        obsolete = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, event_id="older-obsolete")
        second = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, summary="Introduce tutorial")
        applied.consumed = obsolete.consumed = True
        obsolete.superseded_by = first.event_id
        async with plugin.manager.lock:
            dag = plugin.manager.load()
            dag.applied_feedback = (applied.event_id, obsolete.event_id)
            dag.tasks[0] = dag.tasks[0].model_copy(update={"source_issue_ids": (applied.event_id, obsolete.event_id)})
            plugin.manager._save(dag)
            plugin.bus._write(plugin.bus.inbox_path, [applied, obsolete, first, second])

        async def revise(*args, **kwargs):
            base = {"title": "Implement", "acceptance": "Verified", "dependencies": [], "permission_tier": 2}
            return AgentReply(cost_usd=0.1, output=json.dumps({
                "name": "Project", "goal_tree": "# Goal\n",
                "tasks": [
                    {**base, "task_id": "T1", "source_issue_ids": [first.event_id, obsolete.event_id, "unknown"]},
                    {**base, "task_id": "T2", "source_issue_ids": [second.event_id, first.event_id, second.event_id]},
                    {**base, "task_id": "T3"},
                ],
            }))

        monkeypatch.setattr(plugin.pm, "call", revise)
        await plugin.daemon._apply_feedback((first, second))
        assert [task.source_issue_ids for task in plugin.manager.load().tasks] == [
            (applied.event_id, first.event_id), (second.event_id, first.event_id), (),
        ]

    asyncio.run(scenario())


def test_planner_mailbox_surfaces_progress_and_canonical_schema(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr("plugin_entry.BRIDGE_POLL_SEC", 0)
        model = DSHModel(tmp_path)
        route = _request().route
        job = asyncio.create_task(model.plan("p1", tmp_path, route, "draft", 1, True))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert not job.done()
            request, = tmp_path.glob("*.request.json")
            message = json.loads(request.read_text())
            assert message["mode"] == "draft"
            assert message["output_schema"]["properties"]["tasks"]["items"]["properties"]["task_id"]["type"] == "string"
            base = request.name.removesuffix(".request.json")
            progress = PlanningProgress(started_at=1760000000, preview="Public planning note")
            write_atomic(tmp_path / f"{base}.progress.json", progress.model_dump_json())
            assert model.planning_progress("p1") == progress
            write_atomic(tmp_path / f"{base}.response.json", json.dumps({
                "output": None, "error": "Provider unavailable", "cost_usd": 0.1,
                "progress": progress.model_dump(mode="json"),
            }))
            with pytest.raises(ModelCallError, match="Provider unavailable") as failure:
                await asyncio.wait_for(job, timeout=5)
            assert failure.value.progress == progress
            assert model.planning_progress("p1") is None
            assert not list(tmp_path.iterdir())
        finally:
            if not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    asyncio.run(scenario())


def test_provider_failure_keeps_public_preview_without_applying_it(tmp_path, monkeypatch):
    async def scenario():
        plugin, issue = await _seed_review(tmp_path)
        progress = PlanningProgress(started_at=1760000000, preview="Drafted an implementation outline")

        async def revise(*args, **kwargs):
            raise ModelCallError("Provider disconnected", None, progress)

        monkeypatch.setattr(plugin.pm, "call", revise)
        snapshot = plugin.manager.load()
        await plugin.daemon._apply_feedback((issue,))
        assert plugin.manager.load() == snapshot
        failure, = plugin.bus.outbox()
        assert failure.progress == progress and "Provider disconnected" in failure.payload.summary

    asyncio.run(scenario())


def test_revisions_arriving_during_planning_are_replanned_as_one_batch(tmp_path, monkeypatch):
    async def scenario():
        plugin, first = await _seed_review(tmp_path)
        started = asyncio.Event()
        second = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, summary="Improve pacing")
        third = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, summary="Add a tutorial")
        batch = (first, second, third)
        contexts = []

        async def revise(_project, _directory, _route, prompt, _budget, _draft, *, revision=False):
            context = json.loads(prompt.splitlines()[-1])
            contexts.append(context)
            if len(contexts) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
            return _revision_reply()

        monkeypatch.setattr(plugin.pm, "call", revise)
        await plugin.daemon.step()
        await asyncio.wait_for(started.wait(), timeout=5)
        await plugin.bus.append_inbox(second)
        await plugin.bus.append_inbox(third)
        await plugin.daemon.step()
        assert plugin.daemon.feedback_event_ids == tuple(event.event_id for event in batch)
        assert plugin.daemon._feedback_job is not None
        await asyncio.wait_for(plugin.daemon._feedback_job, timeout=5)

        assert len(contexts) == 2
        assert contexts[1]["issues"] == [{
            "event_id": event.event_id, "issue": event.payload.summary, "status": "PENDING",
        } for event in batch]
        assert plugin.manager.load().applied_feedback == tuple(event.event_id for event in batch)
        assert plugin.manager.load().project_status == ProjectStatus.IDLE
        assert not await plugin.bus.pending_inbox()
        assert not plugin.router.active_workers

    asyncio.run(scenario())


@pytest.mark.parametrize("exhausted", [False, True])
def test_unsuccessful_revision_retains_the_original_plan(tmp_path, monkeypatch, exhausted):
    async def scenario():
        plugin, event = await _seed_review(tmp_path)
        if exhausted:
            async with plugin.manager.lock:
                plugin.gate._record_cost(plugin.gate.load_budget().max_total_cost_usd)

        async def revise(*args, **kwargs):
            assert not exhausted
            return _revision_reply(cyclic=True)

        original = plugin.manager.load()
        monkeypatch.setattr(plugin.pm, "call", revise)
        await plugin.daemon._apply_feedback((event,))

        current = plugin.manager.load()
        assert current.model_dump(exclude={"budget"}) == original.model_dump(exclude={"budget"})
        assert current.goal_tree == INITIAL_GOAL
        assert not await plugin.bus.pending_inbox()
        failure, = plugin.bus.outbox()
        assert failure.event_id == f"feedback-error-{event.event_id}"
        assert "revise the plan" in failure.payload.summary
        assert ("No budget" if exhausted else "Cycle") in failure.payload.summary

    asyncio.run(scenario())
