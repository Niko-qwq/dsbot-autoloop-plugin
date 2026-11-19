import asyncio
import json

from api.bridge import ACTION, PANEL, PM_STATUS, Request
from core.bus import make_inbox
from core.schema import InboxEventType, OutboxEventType, PlanEditReceipt, ProjectStatus, TaskStatus, WorkerRun
from runtime.daemon import AgentReply
from runtime.pm_agent import PM_PROMPT_MAX_BYTES
from test_draft_receipt import _chief_request
from test_phase1 import task
from test_plan_revision import _seed_review


def _planned(task_id, *dependencies, source_issue_ids=()):
    return {
        "task_id": task_id, "title": "Implement and verify the change", "acceptance": "Scenario passes",
        "dependencies": dependencies, "permission_tier": 2, "source_issue_ids": source_issue_ids,
    }


async def _seed_started(tmp_path, tasks, status=ProjectStatus.ACTIVE):
    plugin, issue = await _seed_review(tmp_path)
    dag = plugin.manager.load()
    await plugin.manager.save_atomic(dag.model_copy(update={"project_status": status, "tasks": tasks}))
    return plugin, issue


def test_feedback_applies_a_research_task_that_also_checks_acceptance(tmp_path, monkeypatch):
    async def scenario():
        original = task("baseline", status=TaskStatus.COMPLETED)
        plugin, issue = await _seed_started(tmp_path, [original], status=ProjectStatus.COMPLETED)
        dag = plugin.manager.load()
        dag.binding.brief = _chief_request().brief
        await plugin.manager.save_atomic(dag)

        async def plan(_project, _directory, _route, prompt, _budget, draft, **_options):
            assert not draft and "C1" in json.loads(prompt.splitlines()[-1])["brief"]["acceptance"]
            return AgentReply(cost_usd=0.0, output=json.dumps({"tasks": [{
                **_planned("T15", "baseline"), "phase": "RESEARCH", "acceptance_refs": ["C1"],
            }]}))

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.daemon._apply_feedback((issue,))
        saved = plugin.manager.load()
        baseline, addition = saved.tasks
        assert baseline == original
        assert (addition.task_id, addition.phase, addition.acceptance_refs) == ("T15", "RESEARCH", ("C1",))
        assert saved.applied_feedback == (issue.event_id,)
        assert not await plugin.bus.pending_inbox()
        assert all(not event.event_id.startswith("feedback-error-")
                   for event in plugin.bus.outbox())

    asyncio.run(scenario())


def test_deleting_an_issue_fences_inflight_plan_and_preserves_other_feedback(tmp_path, monkeypatch):
    async def scenario():
        plugin, issue = await _seed_started(
            tmp_path, [task("baseline", status=TaskStatus.COMPLETED)], status=ProjectStatus.COMPLETED,
        )
        project = issue.project_id
        other = make_inbox(InboxEventType.USER_FEEDBACK, project_id=project, summary="Keep this other feedback")
        await plugin.bus.append_inbox(other)
        entered, release = asyncio.Event(), asyncio.Event()
        deliveries = []

        async def plan(_project, _directory, _route, prompt, _budget, _draft, **_options):
            context = json.loads(prompt.splitlines()[-1])
            deliveries.append([item["event_id"] for item in context.get("issues", [context])])
            if len(deliveries) == 1:
                entered.set()
                await release.wait()
                return AgentReply(cost_usd=0.0, output=json.dumps({"tasks": [_planned("stale-task")]}))
            return AgentReply(cost_usd=0.0, output='{"tasks":[]}')

        monkeypatch.setattr(plugin.pm, "call", plan)
        before = plugin.manager.dag_path.read_bytes()
        await plugin.daemon.step()
        job = plugin.daemon._feedback_job
        assert job is not None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            request = Request(method="POST", path=ACTION, project_id=project, event_id=issue.event_id,
                              body='{"action":"DELETE_FEEDBACK","task_id":null}')
            receipt = await plugin.bridge.handle(request)
            assert receipt == (200, {"ok": True, "project_id": project, "event_id": issue.event_id})
            assert await plugin.bridge.handle(request) == receipt
            _, panel = await plugin.bridge.handle(Request(method="GET", path=PANEL, project_id=project))
            assert [item["event_id"] for item in panel["inbox"]] == [other.event_id]
            _, pm_status = await plugin.bridge.handle(Request(method="GET", path=PM_STATUS, project_id=project, session_id="pm"))
            assert [item["event_id"] for item in pm_status["inbox"]] == [other.event_id]
        finally:
            release.set()
            await asyncio.wait_for(job, timeout=5)
        assert plugin.manager.dag_path.read_bytes() == before
        assert await plugin.bus.pending_inbox() == [other]

        await plugin.daemon.step()
        await asyncio.wait_for(plugin.daemon._feedback_job, timeout=5)
        assert deliveries == [[issue.event_id, other.event_id], [other.event_id]]
        saved = plugin.manager.load()
        assert saved.applied_feedback == (other.event_id,)
        assert [node.task_id for node in saved.tasks] == ["baseline"]
        assert (await plugin.bridge.handle(request.model_copy(update={"event_id": other.event_id})))[0] == 409
        assert plugin.manager.load() == saved

    asyncio.run(scenario())


def test_inbox_batch_updates_and_merges_unstarted_work_with_issue_links(tmp_path, monkeypatch):
    async def scenario():
        baseline = task("baseline", status=TaskStatus.COMPLETED)
        redundant = task("redundant", ("baseline",)).model_copy(update={"source_issue_ids": ("older-issue",)})
        plugin, first = await _seed_started(tmp_path, [
            baseline, task("keep", ("baseline",)), redundant, task("downstream", ("redundant",)), task("release"),
        ])
        second = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, summary="Add keyboard access")
        await plugin.bus.append_inbox(second)
        calls = []

        async def plan(_project, _directory, _route, prompt, _budget, draft, **_options):
            context = json.loads(prompt.splitlines()[-1])
            assert not draft and context["mode"] == "feedback"
            assert context["editable_task_ids"] == ["keep", "redundant", "downstream", "release"]
            calls.append([entry["event_id"] for entry in context["issues"]])
            return AgentReply(cost_usd=0.1, output=json.dumps({
                "tasks": [
                    _planned("keep", "baseline", source_issue_ids=(first.event_id, "unrelated")),
                    _planned("keyboard", "keep", source_issue_ids=(second.event_id,)),
                ],
                "removals": [{"task_id": "redundant", "replaced_by": "keep"}, {"task_id": "release", "replaced_by": None}],
                "summary": "Merge the overlapping feature work and add a keyboard scenario.",
            }))

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.daemon._apply_feedback((first, second))
        await plugin.pm.feedback((first, second))

        ids = (first.event_id, second.event_id)
        assert calls == [list(ids)]
        updated = plugin.manager.load()
        assert updated.project_status == ProjectStatus.ACTIVE and updated.applied_feedback == ids
        tasks = {node.task_id: node for node in updated.tasks}
        assert list(tasks) == ["baseline", "keep", "downstream", "keyboard"]
        assert tasks["baseline"] == baseline
        assert tasks["keep"].source_issue_ids == (first.event_id, "older-issue")
        assert tasks["downstream"].dependencies == ("keep",)
        assert tasks["keyboard"].source_issue_ids == (second.event_id,)
        receipt, = updated.plan_edits
        assert receipt.source == "INBOX" and receipt.event_ids == ids
        assert receipt.added == ("keyboard",) and receipt.updated == ("keep", "downstream")
        assert receipt.removed == ("redundant", "release")
        notice, = plugin.bus.outbox()
        assert notice.type == OutboxEventType.PROJECT_PREPARED and notice.plan.task_count == 5
        assert not await plugin.bus.pending_inbox()

    asyncio.run(scenario())


def test_feedback_replans_when_target_starts_and_accepts_legacy_tasks_only(tmp_path, monkeypatch):
    async def scenario():
        plugin, issue = await _seed_started(tmp_path, [task("target", status=TaskStatus.READY)])
        contexts = []
        attempt = WorkerRun(run_id="fixture-run", started_at=1)

        async def plan(_project, _directory, _route, prompt, _budget, _draft, **_options):
            context = json.loads(prompt.splitlines()[-1])
            contexts.append(context["editable_task_ids"])
            if len(contexts) == 1:
                async with plugin.manager.lock:
                    dag = plugin.manager.load()
                    dag.tasks[0] = dag.tasks[0].model_copy(update={"status": TaskStatus.RUNNING, "worker_runs": (attempt,)})
                    plugin.manager._save(dag)
                nodes = [_planned("target")]
            else:
                nodes = [_planned("followup", "target")]
            return AgentReply(cost_usd=0.1, output=json.dumps({"tasks": nodes}))

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.daemon._apply_feedback((issue,))
        assert contexts == [["target"], []]
        original, new = plugin.manager.load().tasks
        assert original.worker_runs == (attempt,) and original.status == TaskStatus.RUNNING
        assert new.dependencies == ("target",) and new.source_issue_ids == (issue.event_id,)
        assert plugin.gate.load_budget().current_cost_usd == 0.2
        assert not await plugin.bus.pending_inbox()

    asyncio.run(scenario())


def test_already_covered_issue_has_receipt_without_reopening_project(tmp_path, monkeypatch):
    async def scenario():
        completed = task("verified", status=TaskStatus.COMPLETED)
        plugin, issue = await _seed_started(tmp_path, [completed], ProjectStatus.COMPLETED)
        await plugin.daemon._publish_project_report()
        published, = plugin.bus.outbox()
        assert published.type == OutboxEventType.PROJECT_COMPLETED

        async def plan(*args, **_options):
            return AgentReply(cost_usd=0.1, output=json.dumps({"tasks": [], "summary": "Already verified in verified."}))

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.daemon._apply_feedback((issue,))
        updated = plugin.manager.load()
        assert updated.project_status == ProjectStatus.COMPLETED and updated.tasks == [completed]
        receipt, = updated.plan_edits
        assert receipt.event_ids == (issue.event_id,) and not (receipt.added or receipt.updated or receipt.removed)
        assert not await plugin.bus.pending_inbox()
        await plugin.daemon._publish_project_report()
        assert updated.completion_event_id == published.event_id
        assert plugin.bus.outbox() == [published]

    asyncio.run(scenario())


def test_pending_revision_replans_after_a_confirmed_pm_chat_edit(tmp_path, monkeypatch):
    async def scenario():
        plugin, issue = await _seed_review(tmp_path)
        contexts = []

        async def plan(_project, _directory, _route, prompt, _budget, _draft, *, revision):
            assert revision
            context = json.loads(prompt.splitlines()[-1])
            task_ids = [node["task_id"] for node in context["dag"]["tasks"]]
            contexts.append(task_ids)
            if len(contexts) == 1:
                async with plugin.manager.lock:
                    plugin.manager.commit_task_edit(
                        issue.project_id, "chat-edit", "Queue existing improvement", "PM_CHAT", [task("improvement")], {},
                    )
            return AgentReply(cost_usd=0.1, output=json.dumps({
                "name": "Project", "goal_tree": "# Revised plan\n",
                "tasks": [_planned(task_id) for task_id in task_ids] + [
                    _planned("issue-scope", source_issue_ids=(issue.event_id,)),
                ],
            }))

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.daemon._apply_feedback((issue,))
        updated = plugin.manager.load()
        assert contexts == [["T1"], ["T1", "improvement"]]
        assert updated.project_status == ProjectStatus.IDLE
        assert [node.task_id for node in updated.tasks] == ["T1", "improvement", "issue-scope"]
        assert updated.tasks[-1].source_issue_ids == (issue.event_id,)
        assert updated.plan_edits[0].id == "chat-edit"
        assert not await plugin.bus.pending_inbox()

    asyncio.run(scenario())


def test_feedback_request_keeps_live_constraints_without_worker_history(tmp_path, monkeypatch):
    async def scenario():
        work = task("baseline", status=TaskStatus.COMPLETED).model_copy(update={
            "worker_runs": (
                WorkerRun(run_id="fixture-run", started_at=1, finished_at=2, status="FAILED", detail="Old history " * 6000),
                WorkerRun(run_id="fixture-run", started_at=3, finished_at=4, status="COMPLETED", detail="Recent acceptance evidence"),
            ),
            "output_artifacts": ("old-artifact/" * 6000, "checks.md", "missing.txt", "outside.txt"),
            "acceptance": "Retain the existing workflow and its keyboard behavior",
            "source_issue_ids": ("recorded-provenance",),
        })
        dependency = task("followup", ("baseline",))
        unstarted = task("unstarted")
        plugin, first = await _seed_started(tmp_path, [work, dependency, unstarted])
        project = plugin.manager.project_directory(first.project_id)
        project.mkdir(parents=True)
        (project / "checks.md").write_text("Full report body should stay on disk.\n")
        outside = tmp_path / "outside.txt"
        outside.write_text("Out-of-project text\n")
        (project / "outside.txt").symlink_to(outside)
        dag = plugin.manager.load()
        dag.plan_edits = (PlanEditReceipt(id="retire-id", summary="Retired old work", source="PM_CHAT",
                                          timestamp=1, removed=("retired-task",)),)
        dag.binding.brief = _chief_request().brief.model_copy(update={
            "acceptance": (), "authorization": "Keep all keyboard shortcuts.",
        })
        await plugin.manager.save_atomic(dag)
        latest = "Additional exact requirement · " + "保留原有功能" * 85
        second = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id, summary=latest)
        await plugin.bus.append_inbox(second)

        async def plan(_project, _directory, _route, prompt, _budget, _draft, **_options):
            assert len(prompt.encode("utf-8")) <= PM_PROMPT_MAX_BYTES
            for excluded in ("Old history", "old-artifact", "Historical diary omitted", "Full result history", "Full report body"):
                assert excluded not in prompt
            assert prompt.count(latest) == 1
            context = json.loads(prompt.splitlines()[-1])
            assert context["goal_tree"] == dag.goal_tree
            assert "keyboard shortcuts" in context["brief"]["authorization"]
            assert context["removed_task_ids"] == ["retired-task"]
            assert [row["issue"] for row in context["issues"]] == [first.payload.summary, latest]
            assert context["dag"]["tasks"][0]["acceptance"] == work.acceptance
            assert context["dag"]["tasks"][1]["dependencies"] == ["baseline"]
            assert context["task_evidence"] == {"baseline": {
                "run_status": "COMPLETED", "conclusion": "Recent acceptance evidence", "paths": ["checks.md"],
            }}
            return AgentReply(cost_usd=0.0, output='{"tasks":[]}')

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.pm.feedback((first, second))
        assert plugin.manager.load().tasks[0] == work
        assert not await plugin.bus.pending_inbox()

    asyncio.run(scenario())


def test_excessive_feedback_batch_reports_budget_failure_before_calling_model(tmp_path, monkeypatch):
    async def scenario():
        plugin, first = await _seed_started(tmp_path, [task("existing")])
        batch = [first]
        for index in range(11):
            event = make_inbox(InboxEventType.USER_FEEDBACK, project_id=first.project_id,
                               summary=f"{index} " + "验" * 1350)
            await plugin.bus.append_inbox(event)
            batch.append(event)

        calls = []

        async def plan(*args, **_options):
            calls.append(args)
            return AgentReply(cost_usd=0.0, output='{"tasks":[]}')

        monkeypatch.setattr(plugin.pm, "call", plan)
        await plugin.daemon._apply_feedback(tuple(batch))
        assert not calls and plugin.gate.load_budget().current_cost_usd == 0.0
        assert plugin.manager.load().project_status == ProjectStatus.ACTIVE
        alerts = plugin.bus.outbox()
        assert len(alerts) == len(batch)
        assert all("input budget" in row.payload.summary and "No model request was sent" in row.payload.summary
                   for row in alerts)
        stored = plugin.bus.inbox()
        assert [row.payload.summary for row in stored] == [row.payload.summary for row in batch]

    asyncio.run(scenario())
