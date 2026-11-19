import asyncio
import json
import time

import pytest

from api.bridge import ACTION, OUTBOX, PANEL, RETRY_DRAFT, ActionConflict, Request
from core.schema import (
    ModelRoute, OutboxEventType, PlanningProgress, PrepareProject,
    ProjectBinding, ProjectBrief, ProjectStatus, TaskDAG, TaskStatus,
)
from core.state_manager import StateFormatError, write_atomic
from plugin_entry import Plugin
from runtime.daemon import AgentReply
from runtime.pm_agent import PMDraft, PMTask
from test_phase1 import task


def _request() -> PrepareProject:
    return PrepareProject(
        session_id="chief-session", goal="制作一个网页小游戏", max_budget_usd=10.0,
        route=ModelRoute(
            provider="test-provider", model="test-model", reasoning_effort=None,
            input_rate=0.1, output_rate=0.2, cache_read_rate=0.0, cache_write_rate=0.0,
        ),
    )


def _chief_request() -> PrepareProject:
    request = _request()
    request.brief = ProjectBrief(
        source_session_id=request.session_id, source_message_id="user-message-1",
        user_instruction="制作试玩页，验证刷新后的得分。",
        acceptance=("刷新后得分取回。",), authorization="本地实现、验证与普通重试。",
    )
    return request


async def _structured_plan(plugin, request, monkeypatch) -> tuple[dict, list[dict]]:
    calls = []

    async def plan(_project_id, _directory, _route, prompt, _budget, draft):
        assert draft is True
        calls.append(json.loads(prompt.rsplit("\n\n", 1)[1]))
        return AgentReply(cost_usd=0.0, output=json.dumps({
            "name": "Game",
            "goal_tree": "# Game",
            "tasks": [
                {"task_id": "build", "title": "Game", "acceptance": "The game has a score.", "dependencies": [],
                 "permission_tier": 2},
                {"task_id": "verify", "title": "Score verification", "acceptance": "Browser refresh preserves score.",
                 "dependencies": ["build"], "permission_tier": 2, "phase": "VERIFY", "acceptance_refs": ["C1"]},
            ],
        }))

    monkeypatch.setattr(plugin.pm, "call", plan)
    receipt = await plugin.prepare(request)
    await plugin._plan_draft(plugin._load_draft())
    return receipt, calls


async def _new_plugin(tmp_path):
    data = tmp_path / "data"
    mailbox = tmp_path / "mailbox"
    data.mkdir()
    mailbox.mkdir()
    plugin = Plugin(data, mailbox, "unused-codex")
    await plugin.initialize()
    return plugin


def test_chief_assignment_survives_planning_restart_and_conversation_rollover(tmp_path, monkeypatch):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _chief_request()
        receipt = await plugin.prepare(request)
        successor = request.model_copy(update={"session_id": "next-segment", "goal": "Retried tool request"})
        chain = (successor.session_id, request.session_id)
        assert await plugin.prepare(successor, chain) == receipt
        with pytest.raises(ActionConflict, match="different Chief conversation"):
            await plugin.prepare(successor, (successor.session_id,))
        _, calls = await _structured_plan(plugin, request, monkeypatch)
        assert len(calls) == 1 and calls[0]["original_instruction"] == request.brief.user_instruction
        assert calls[0]["brief"]["acceptance"] == {"C1": request.brief.acceptance[0]}
        assert calls[0]["brief"]["authorization"] == request.brief.authorization
        binding = plugin.manager.load().binding
        assert binding.brief == request.brief
        assert plugin.manager.load().tasks[-1].acceptance_refs == ("C1",)
        restart = Plugin(tmp_path / "data", plugin.mailbox, "unused-codex")
        await restart.initialize()
        repeated = await restart.prepare(successor, chain)
        assert repeated["status"] == "PREPARED"
        assert repeated["project_id"] == receipt["project_id"]
        assert repeated["title"] == receipt["title"]
        replayed_in_current_segment = successor.model_copy(update={"brief": successor.brief.model_copy(
            update={"source_session_id": successor.session_id},
        )})
        assert await restart.prepare(replayed_in_current_segment, chain) == repeated
        assert restart._load_draft() is None
        assert len([event for event in restart.bus.outbox()
                    if event.type == OutboxEventType.PROJECT_PREPARED]) == 1

    asyncio.run(scenario())


def test_retry_failed_draft_keeps_its_id_and_requires_a_current_attempt(tmp_path, monkeypatch):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        original = _chief_request()
        receipt = await plugin.prepare(original)
        project_id = receipt["project_id"]

        async def fail(*args):
            raise StateFormatError("Task research-platform must use phase VERIFY for user acceptance checks")

        monkeypatch.setattr(plugin.pm, "draft", fail)
        await plugin._plan_draft(plugin._load_draft())
        failed = await plugin.prepare(original)
        assert (failed["project_id"], failed["status"], failed.get("attempt", 0)) == (project_id, "FAILED", 0)
        before = plugin.manager.dag_path.read_bytes()
        body = json.dumps({"project_id": project_id, "attempt": 0})
        pm = Request(method="POST", path=RETRY_DRAFT, body=body, project_id=project_id, session_id="draft-pm")
        chief = pm.model_copy(update={"project_id": None, "session_id": "successor",
                                      "chief_sessions": ("successor", original.session_id)})
        assert (await plugin.bridge.handle(pm.model_copy(update={"project_id": "other"})))[0] == 403
        assert (await plugin.bridge.handle(chief.model_copy(update={"chief_sessions": ("successor",)})))[0] == 403
        first, duplicate = await asyncio.gather(plugin.bridge.handle(pm), plugin.bridge.handle(chief))
        assert first == duplicate
        status, queued = first
        assert status == 202 and queued == {**failed, "status": "QUEUED", "attempt": 1, "error": None}
        assert plugin._load_draft().request == original
        assert plugin.manager.dag_path.read_bytes() == before

        await plugin._plan_draft(plugin._load_draft())
        status, stale = await plugin.bridge.handle(pm)
        assert status == 202 and stale["status"] == "FAILED" and stale["attempt"] == 1
        retry = chief.model_copy(update={"body": json.dumps({"project_id": project_id, "attempt": 1})})
        assert (await plugin.bridge.handle(retry))[1]["attempt"] == 2

        restart = Plugin(tmp_path / "data", plugin.mailbox, "unused-codex")
        await restart.initialize()
        await _structured_plan(restart, original, monkeypatch)
        prepared = await restart.prepare(original)
        assert prepared["status"] == "PREPARED" and prepared["project_id"] == project_id
        dag = restart.manager.load()
        assert dag.project_status == ProjectStatus.IDLE and dag.project_id == project_id
        assert not await restart.bus.pending_inbox()
        assert restart._load_draft() is None
        assert (await restart.bridge.handle(retry))[0] == 409
        assert len([event for event in restart.bus.outbox()
                    if event.type == OutboxEventType.PROJECT_PREPARED]) == 1

    asyncio.run(scenario())


def test_chief_recovers_assignment_if_interrupted_after_plan_commit(tmp_path, monkeypatch):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _chief_request()
        receipt = await plugin.prepare(request)

        def interrupted(*args):
            raise OSError("interrupted between plan and receipt")

        monkeypatch.setattr(plugin, "_record_prepare_receipt", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            await _structured_plan(plugin, request, monkeypatch)
        assert plugin.manager.load().project_id == receipt["project_id"]
        restart = Plugin(tmp_path / "data", plugin.mailbox, "unused-codex")
        await restart.initialize()
        repeated = await restart.prepare(request)
        assert repeated["status"] == "PREPARED" and repeated["project_id"] == receipt["project_id"]
        assert len([event for event in restart.bus.outbox()
                    if event.type == OutboxEventType.PROJECT_PREPARED]) == 1

    asyncio.run(scenario())


def test_prepare_returns_receipt_before_pm_finishes_and_outbox_becomes_confirmation(tmp_path, monkeypatch):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _request().model_copy(update={"pm_name": "猫队长"})
        entered, release = asyncio.Event(), asyncio.Event()

        async def draft(project_id, received, directory):
            assert received == request
            assert directory == plugin.manager.project_directory(project_id)
            entered.set()
            await release.wait()
            node = PMTask(task_id="T1", title="实现小游戏", acceptance="静态页面可以打开", dependencies=(), permission_tier=2)
            plan = PMDraft(tasks=[node], name="猫鼠小游戏", goal_tree="# 猫鼠小游戏\n")
            dag = TaskDAG(project_id=project_id, project_status=ProjectStatus.IDLE, tasks=[node.to_task()], goal_tree=plan.goal_tree)
            return plan, dag, 0.25

        monkeypatch.setattr(plugin.pm, "draft", draft)
        response, receipt = await asyncio.wait_for(plugin.bridge.handle(Request(
            method="POST", path="prepare", body=request.model_dump_json(),
        )), timeout=2)
        assert response == 202
        assert receipt == {
            "project_id": receipt["project_id"], "status": "QUEUED", "title": request.goal, "error": None,
            "pm_name": "猫队长",
        }
        assert not entered.is_set()
        assert plugin.manager.load().project_id == "idle"
        assert plugin.read_draft("other-session") is None
        projection = Request(method="GET", path=OUTBOX, session_id=request.session_id)
        assert await plugin.bridge.handle(projection) == (200, {"active": False, "draft": receipt})
        successor = {"session_id": "chief-successor", "chief_sessions": ("chief-successor", request.session_id)}
        assert await plugin.bridge.handle(projection.model_copy(update=successor)) == (200, {"active": False, "draft": receipt})
        repeated = Request(method="POST", path="prepare", **successor,
                           body=request.model_copy(update={"session_id": "chief-successor"}).model_dump_json())
        assert await plugin.bridge.handle(repeated) == (202, receipt)

        pending = plugin._load_draft()
        assert pending is not None
        job = asyncio.create_task(plugin._plan_draft(pending))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            status, outbox = await plugin.bridge.handle(projection)
            assert status == 200
            assert outbox["draft"]["status"] == "PLANNING"
            assert not job.done()
        finally:
            release.set()
            await asyncio.wait_for(job, timeout=5)

        assert plugin._load_draft() is None
        assert not plugin.draft_path.exists()
        status, outbox = await plugin.bridge.handle(projection)
        assert status == 200 and outbox["active"] is True and "draft" not in outbox
        assert outbox["snapshot"]["dag"]["project_id"] == receipt["project_id"]
        assert outbox["snapshot"]["dag"]["project_status"] == "IDLE"
        assert outbox["snapshot"]["budget"]["current_cost_usd"] == 0.25
        assert outbox["plan"]["approval_token"] == plugin.manager.load().start_approval_base
        assert "events" not in outbox
        status, panel = await plugin.bridge.handle(Request(method="GET", path=PANEL, session_id=request.session_id))
        assert status == 200 and panel["projects"][0]["pm_name"] == receipt["pm_name"]

    asyncio.run(scenario())


def test_review_projects_saved_plan_and_rejects_old_confirmation_after_revision(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _request()
        project_id = "proj_existing"
        await plugin.manager.save_atomic(TaskDAG(
            project_id=project_id, project_status=ProjectStatus.IDLE, tasks=[task("T1")],
            goal_tree="## Goal\n\nAn existing plan\n",
            binding=ProjectBinding(project_id=project_id, session_id=request.session_id, route=request.route, name="Current game"),
        ))
        panel = Request(method="GET", path=PANEL, session_id=request.session_id)
        outbox = Request(method="GET", path=OUTBOX, session_id=request.session_id)
        action = Request(method="POST", path=ACTION, project_id=project_id)
        start = action.model_copy(update={"body": '{"action":"START_PROJECT","task_id":null}'})

        status, feed = await plugin.bridge.handle(outbox)
        assert status == 200 and feed["plan"]["goal_tree"] == "## Goal\n\nAn existing plan\n"
        old_token = feed["plan"]["approval_token"]
        status, receipt = await plugin.bridge.handle(action.model_copy(update={
            "body": '{"action":"USER_FEEDBACK","issue":"Add another gameplay route"}',
        }))
        assert status == 200 and receipt["mode"] == "revision"
        old_start = start.model_copy(update={"event_id": old_token})
        assert (await plugin.bridge.handle(old_start))[0] == 409

        first_id = receipt["event_id"]
        status, receipt = await plugin.bridge.handle(action.model_copy(update={
            "body": '{"action":"USER_FEEDBACK","issue":"Design a creative website","replace_plan":true}',
        }))
        assert status == 200 and receipt["mode"] == "replacement"
        status, data = await plugin.bridge.handle(panel)
        feedback = {event["event_id"]: event["feedback"]["status"] for event in data["inbox"] if event["feedback"]}
        assert first_id not in feedback
        assert feedback == {receipt["event_id"]: "QUEUED"}
        assert data["projects"][0]["plan"]["revision"]["issue"] == "Design a creative website"
        pm_outbox = outbox.model_copy(update={"session_id": "project-pm", "project_id": project_id})
        status, pm_feed = await plugin.bridge.handle(pm_outbox)
        assert status == 200 and pm_feed["pm"]["activity"]["status"] == "QUEUED"
        plugin.daemon._feedback_events = tuple(await plugin.bus.pending_inbox())
        started_at = time.time() - 185
        plugin.daemon._read_planning_progress = lambda _project: PlanningProgress(started_at=started_at, preview="Drafting the page")
        _, pm_feed = await plugin.bridge.handle(pm_outbox)
        assert pm_feed["pm"]["activity"] == {"status": "RUNNING", "started_at": started_at,
                                             "public_text": "Drafting the page", "error": None}

        async with plugin.manager.lock:
            linked = task("T2").model_copy(update={"source_issue_ids": (receipt["event_id"],)})
            plugin.manager.replace_plan(project_id, (receipt["event_id"],), "## Approach\n\nTwo routes\n", [linked])
            plugin.bus._ack_inbox(receipt["event_id"])
        assert (await plugin.bridge.handle(old_start))[0] == 409
        assert (await plugin.bridge.handle(start))[0] == 409
        status, data = await plugin.bridge.handle(panel)
        plan = data["projects"][0]["plan"]
        assert status == 200 and plan["revision"]["status"] == "APPLIED"
        assert data["inbox"] == []
        assert plan["tasks"][0]["source_issue_ids"] == [receipt["event_id"]]
        assert plan["sources"] == [{"id": receipt["event_id"], "summary": "Design a creative website"}]
        assert plan["goal_tree"] == "## Approach\n\nTwo routes\n" and [node["task_id"] for node in plan["tasks"]] == ["T2"]
        assert data["snapshot"]["dag"]["project_status"] == "IDLE"
        plugin.daemon._read_planning_progress = None
        _, pm_feed = await plugin.bridge.handle(pm_outbox)
        assert pm_feed["pm"]["activity"]["status"] == "READY"
        assert pm_feed["pm"]["plan"]["sources"] == plan["sources"]
        assert (await plugin.bridge.handle(start.model_copy(update={"event_id": plan["approval_token"]})))[0] == 200

    asyncio.run(scenario())


def test_failed_planning_keeps_prior_project_and_displays_actual_error(tmp_path, monkeypatch):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _request()
        previous = TaskDAG(project_id="proj_previous", project_status=ProjectStatus.COMPLETED,
                           tasks=[task("T1", status=TaskStatus.COMPLETED)], binding=ProjectBinding(
                               project_id="proj_previous", session_id=request.session_id, route=request.route,
                           ))
        await plugin.manager.save_atomic(previous)

        async def reject(*_):
            raise StateFormatError("PM draft has an invalid dependency")

        monkeypatch.setattr(plugin.pm, "draft", reject)
        receipt = await plugin.prepare(request)
        pending = plugin._load_draft()
        assert pending is not None
        await plugin._plan_draft(pending)

        assert plugin.manager.load() == previous
        status, outbox = await plugin.bridge.handle(Request(method="GET", path=OUTBOX, session_id=request.session_id))
        assert status == 200
        assert outbox["draft"]["project_id"] == receipt["project_id"]
        assert outbox["draft"]["status"] == "FAILED"
        assert "invalid dependency" in outbox["draft"]["error"]

    asyncio.run(scenario())


def test_startup_surfaces_interrupted_planning_in_original_session(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _request()
        receipt = await plugin.prepare(request)
        path = plugin.draft_path
        persisted = json.loads(path.read_text(encoding="utf-8"))
        persisted["status"] = "PLANNING"
        write_atomic(path, json.dumps(persisted))

        restarted = Plugin(tmp_path / "data", plugin.mailbox, "unused-codex")
        await restarted.initialize()
        assert restarted.manager.load().project_id == "idle"
        projection = restarted.read_draft(request.session_id)
        assert projection["project_id"] == receipt["project_id"]
        assert projection["status"] == "FAILED"
        assert projection["error"]
        assert restarted.read_draft("other-session") is None

    asyncio.run(scenario())
