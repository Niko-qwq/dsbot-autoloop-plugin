import asyncio
import json
import time

from api.bridge import ACTION, OUTBOX, PANEL, PM_PLAN, PM_STATUS, PROPOSE_PM_PLAN, Request
from core.bus import make_inbox, make_outbox
from core.schema import InboxEventType, OutboxEventType, ProjectBinding, ProjectBrief, ProjectStatus, TaskDAG, TaskStatus, WorkerRun
from test_draft_receipt import _chief_request, _new_plugin, _request
from test_phase1 import publish, task


PROJECT = "proj_sample"
SESSION = "project-pm"


async def prepare_project(tmp_path, status, tasks):
    plugin = await _new_plugin(tmp_path)
    route = _request()
    binding = ProjectBinding(project_id=PROJECT, session_id=route.session_id, route=route.route, name="Sample project")
    await plugin.manager.save_atomic(TaskDAG(project_id=PROJECT, project_status=status, tasks=tasks, binding=binding))
    return plugin


def propose(change):
    return Request(method="POST", path=PROPOSE_PM_PLAN, project_id=PROJECT, session_id=SESSION, body=json.dumps(change))


def confirm(identifier, session=SESSION):
    return Request(method="POST", path=PM_PLAN, project_id=PROJECT, session_id=session,
                   body=json.dumps({"proposal_id": identifier}))


async def pm_chat(plugin, session=SESSION):
    status, payload = await plugin.bridge.handle(Request(method="GET", path=OUTBOX, project_id=PROJECT, session_id=session))
    assert status == 200
    return payload["pm"]


def spec(task_id, dependencies=()):
    return {"task_id": task_id, "title": "Implement and verify one coherent change", "acceptance": "Record the result",
            "dependencies": list(dependencies), "permission_tier": 2}


def test_managed_plan_merge_preserves_acceptance_criteria(tmp_path):
    async def scenario():
        source = task("source").model_copy(update={"acceptance_refs": ("C1",)})
        plugin = await prepare_project(tmp_path, ProjectStatus.IDLE, [source, task("survivor")])
        dag = plugin.manager.load()
        dag.binding.brief = ProjectBrief(
            source_message_id="brief", user_instruction="Summarize the tests", acceptance=("Record the findings",),
            authorization="Read-only research",
        )
        await plugin.manager.save_atomic(dag)
        change = {"summary": "Fold the evidence into one task", "removals": [{"task_id": "source", "replaced_by": "survivor"}]}
        status, response = await plugin.bridge.handle(propose(change))
        assert status == 409 and "acceptance criteria: C1" in response["error"]
        change["tasks"] = [{**spec("survivor"), "phase": "RESEARCH", "acceptance_refs": ["C1"]}]
        status, preview = await plugin.bridge.handle(propose(change))
        assert status == 200
        assert (await plugin.bridge.handle(confirm(preview["id"])))[0] == 200
        survivor, = plugin.manager.load().tasks
        assert survivor.task_id == "survivor" and survivor.acceptance_refs == ("C1",)

    asyncio.run(scenario())


def test_pm_status_supplies_scoped_checks_and_current_feedback(tmp_path):
    async def scenario():
        editable_acceptance = "Keep full requirements · " + "验收" * 300
        plugin = await prepare_project(tmp_path, ProjectStatus.ACTIVE, [
            task("checked", status=TaskStatus.COMPLETED),
            task("next", ("checked",)).model_copy(update={"acceptance": editable_acceptance}),
        ])
        directory = plugin.manager.project_directory(PROJECT)
        directory.mkdir(parents=True)
        artifact = directory / "checks.md"
        artifact.write_text("Original report body", encoding="utf-8")
        dag = plugin.manager.load()
        dag.tasks[0] = dag.tasks[0].model_copy(update={
            "acceptance": "Confirm export files reopen", "output_artifacts": (str(artifact),),
            "worker_runs": (WorkerRun(run_id="fixture-run", started_at=1, finished_at=2, status="COMPLETED", detail="The files reopened."),),
        })
        await plugin.manager.save_atomic(dag)
        issue = "保留验收范围。\n按照下列要求修改导出。" + "新要求" * 300
        pending = make_inbox(InboxEventType.USER_FEEDBACK, summary=issue, project_id=PROJECT)
        await plugin.bus.append_inbox(pending)
        await plugin.bus.append_inbox(make_inbox(InboxEventType.USER_FEEDBACK, summary="Different project", project_id="other"))
        failed = make_inbox(InboxEventType.USER_FEEDBACK, summary="Previous issue text " * 100, project_id=PROJECT)
        failed.consumed = True
        await plugin.bus.append_inbox(failed)
        await publish(plugin.bus, make_outbox(
            OutboxEventType.PROJECT_ALERT, project_id=PROJECT,
            event_id=f"feedback-error-{failed.event_id}", summary="Old error log " * 100,
        ))

        req = Request(method="GET", path=PM_STATUS, project_id=PROJECT, session_id=SESSION)
        status, response = await plugin.bridge.handle(req)
        assert status == 200 and response["project"]["id"] == PROJECT
        assert response["plan"]["editable_task_ids"] == ["next"]
        checked, next_task = response["plan"]["tasks"]
        assert checked["acceptance"] == "Confirm export files reopen"
        assert checked["evidence"] == {
            "run_status": "COMPLETED", "conclusion": "The files reopened.", "paths": ["checks.md"],
        }
        assert next_task["acceptance"] == editable_acceptance
        assert "snapshot" not in response and "tasks" not in response and "worker_runs" not in checked
        rows = {entry["event_id"]: entry for entry in response["inbox"]}
        assert rows[pending.event_id]["status"] == "QUEUED" and rows[pending.event_id]["issue"] == issue
        assert rows[failed.event_id]["status"] == "FAILED" and len(rows[failed.event_id]["error"]) < 200
        assert "issue" not in rows[failed.event_id] and len(rows) == 2
        assert "Original report body" not in json.dumps(response)
        assert (await plugin.bridge.handle(req.model_copy(update={"project_id": None})))[0] == 403
        assert (await plugin.bridge.handle(req.model_copy(update={"project_id": "other"})))[0] == 404

    asyncio.run(scenario())


def test_direct_pm_proposal_only_changes_dag_after_session_scoped_confirmation(tmp_path):
    async def scenario():
        plugin = await prepare_project(tmp_path, ProjectStatus.ACTIVE, [
            task("existing", status=TaskStatus.COMPLETED), task("design", ("existing",)),
            task("checks", ("design",)), task("delivery", ("checks",)),
        ])
        before = plugin.manager.dag_path.read_bytes()
        status, preview = await plugin.bridge.handle(propose({
            "summary": "Combine the implementation and its checks",
            "tasks": [spec("design", ("existing",))],
            "removals": [{"task_id": "checks", "replaced_by": "design"}],
        }))
        assert status == 200
        assert plugin.manager.dag_path.read_bytes() == before
        assert not await plugin.bus.pending_inbox()

        assert (await pm_chat(plugin))["proposal"] == preview
        assert (await pm_chat(plugin, "another-session"))["proposal"] is None
        identifier = preview["id"]
        assert (await plugin.bridge.handle(confirm(identifier, "another-session")))[0] == 409
        result = await plugin.bridge.handle(confirm(identifier))
        assert result[0] == 200 and result[1]["receipt"]["id"] == identifier
        applied = plugin.manager.dag_path.read_bytes()
        assert await plugin.bridge.handle(confirm(identifier)) == result
        assert plugin.manager.dag_path.read_bytes() == applied
        chat = await pm_chat(plugin)
        assert chat["proposal"] is None and "proposal_expired" not in chat
        dag = plugin.manager.load()
        assert [node.task_id for node in dag.tasks] == ["existing", "design", "delivery"]
        assert dag.tasks[-1].dependencies == ("design",)
        assert dag.plan_edits[0].source == "PM_CHAT"
        assert not await plugin.bus.pending_inbox()

    asyncio.run(scenario())


def test_pm_confirmation_rejects_task_that_started_while_proposal_was_open(tmp_path):
    async def scenario():
        plugin = await prepare_project(tmp_path, ProjectStatus.ACTIVE, [
            task("work", status=TaskStatus.READY), task("unrelated", status=TaskStatus.READY),
        ])
        status, request = await plugin.bridge.handle(propose({"summary": "Change work", "tasks": [spec("work")]}))
        assert status == 200
        dag = plugin.manager.load()
        fingerprint = plugin.manager.plan_fingerprint(dag)
        dag.tasks[1] = dag.tasks[1].model_copy(update={"status": TaskStatus.COMPLETED})
        await plugin.manager.save_atomic(dag)
        assert (await pm_chat(plugin))["proposal"] == request
        dag.tasks[0] = dag.tasks[0].model_copy(update={
            "status": TaskStatus.RUNNING, "worker_runs": (WorkerRun(run_id="fixture-run", started_at=time.time()),),
        })
        await plugin.manager.save_atomic(dag)
        assert plugin.manager.plan_fingerprint(dag) == fingerprint
        chat = await pm_chat(plugin)
        assert chat["proposal"] is None and "proposal_expired" not in chat
        status, response = await plugin.bridge.handle(confirm(request["id"]))
        assert status == 409 and response["error"]
        assert plugin.manager.load() == dag

    asyncio.run(scenario())


def test_pm_chat_hides_superseded_plan_and_can_propose_new_work_after_completion(tmp_path):
    async def scenario():
        plugin = await prepare_project(tmp_path, ProjectStatus.ACTIVE, [task("work", status=TaskStatus.READY)])
        status, old = await plugin.bridge.handle(propose({"summary": "Change work", "tasks": [spec("work")]}))
        assert status == 200
        dag = plugin.manager.load().model_copy(update={"goal_tree": "# Updated plan"})
        await plugin.manager.save_atomic(dag)
        chat = await pm_chat(plugin)
        assert chat["proposal"] is None and "proposal_expired" not in chat
        assert (await plugin.bridge.handle(confirm(old["id"])))[0] == 409
        assert plugin.manager.load() == dag

        finished = task("work", status=TaskStatus.COMPLETED)
        await plugin.manager.save_atomic(dag.model_copy(update={"tasks": [finished], "project_status": ProjectStatus.COMPLETED}))
        chat = await pm_chat(plugin)
        assert chat["proposal"] is None and chat["plan"] is None
        status, panel = await plugin.bridge.handle(Request(method="GET", path=PANEL, project_id=PROJECT, session_id=SESSION))
        assert status == 200 and panel["projects"][0]["plan"]["tasks"][0]["task_id"] == "work"
        complete = plugin.manager.load()
        complete.binding.brief = _chief_request().brief
        await plugin.manager.save_atomic(complete)
        additions = {"summary": "Add and check a new deliverable", "tasks": [spec("new", ("work",))]}
        additions["tasks"][0]["acceptance_refs"] = ["C9"]
        status, invalid = await plugin.bridge.handle(propose(additions))
        assert status == 409 and "unknown acceptance criteria: C9" in invalid["error"]

        additions["tasks"][0]["acceptance_refs"] = ["C1"]
        status, new = await plugin.bridge.handle(propose(additions))
        assert status == 200
        chat = await pm_chat(plugin)
        assert chat["proposal"] == new
        assert (await plugin.bridge.handle(confirm(new["id"])))[0] == 200
        saved = plugin.manager.load()
        assert saved.project_status == ProjectStatus.ACTIVE
        assert saved.tasks[0] == finished and saved.tasks[1].status == TaskStatus.READY
        assert saved.tasks[1].phase is None and saved.tasks[1].acceptance_refs == ("C1",)

    asyncio.run(scenario())


def test_confirming_pm_change_to_unstarted_plan_requires_fresh_start_confirmation(tmp_path):
    async def scenario():
        plugin = await prepare_project(tmp_path, ProjectStatus.IDLE, [task("work")])
        before = plugin.manager.load().start_approval_base
        status, preview = await plugin.bridge.handle(propose({"summary": "Plan one deliverable", "tasks": [spec("work")]}))
        assert status == 200
        assert (await plugin.bridge.handle(confirm(preview["id"])))[0] == 200
        latest = plugin.manager.load()
        assert latest.project_status == ProjectStatus.IDLE and latest.start_approval_base != before
        assert latest.start_approval_base == plugin.bus.start_token(latest)
        start = Request(method="POST", path=ACTION, body=json.dumps({"action": "START_PROJECT", "task_id": None}),
                        project_id=PROJECT, session_id=_request().session_id, event_id=before)
        assert (await plugin.bridge.handle(start))[0] == 409
        assert (await plugin.bridge.handle(start.model_copy(update={"event_id": latest.start_approval_base})))[0] == 200

    asyncio.run(scenario())
