import asyncio
import json

from api.bridge import ACTION, CONTEXT, DETAILS, NOTICES, OUTBOX, Request
from core.bus import make_outbox
from core.schema import OutboxEventType, ProjectBinding, ProjectStatus, TaskDAG, TaskEscalation, TaskStatus, WorkerRun
from test_draft_receipt import _new_plugin, _request
from test_phase1 import publish, task


def test_chief_context_is_scoped_and_evidence_is_pulled_by_reference(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        request = _request()
        project = "proj_context"
        directory = await plugin.worker.initialize_project(project)
        result = directory / "app.html"
        result.write_text("app")
        hidden = directory / ".private"
        hidden.write_text("hidden")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        link = directory / "link.html"
        link.symlink_to(outside)
        error = "PRIVATE EARLY LOG\n" + "traceback line\n" * 30 + "pytest timed out\n"
        failed = task("T1", status=TaskStatus.FAILED).model_copy(update={
            "error_msg": error, "retry_count": 3, "output_artifacts": (str(result), str(outside), str(link), str(hidden)),
            "worker_runs": (WorkerRun(
                run_id="fixture-run", started_at=5.0, finished_at=8.0, status="FAILED", detail="public failure summary",
                escalation=TaskEscalation(reason_code="WORKER_TIMEOUT", summary=error),
            ),),
        })
        binding = ProjectBinding(project_id=project, session_id=request.session_id, route=request.route)
        dag = TaskDAG(project_id=project, project_status=ProjectStatus.ACTIVE, binding=binding, tasks=[
            failed, task("T2", status=TaskStatus.RUNNING), task("T3", status=TaskStatus.COMPLETED),
        ])
        await plugin.manager.save_atomic(dag)
        await publish(plugin.bus, make_outbox(
            OutboxEventType.ALERT_ESCALATED, project_id=project, event_id="escalation-fixture-run", task_id="T1",
            reason_code="WORKER_TIMEOUT", summary=error,
        ))
        context_request = Request(method="GET", path=CONTEXT, session_id=request.session_id)
        status, context = await plugin.bridge.handle(context_request)
        assert status == 200
        assert context["project"] == {
            "id": project, "status": "ACTIVE", "done": 1, "total": 3, "running": ["T2"], "blocked": ["T1"],
        }
        anchor = context["recent_event"]
        assert anchor["event_id"] == "escalation-fixture-run" and anchor["reason_code"] == "WORKER_TIMEOUT"
        assert context["recent_issue"] == anchor
        assert len(anchor["event_ref"]) == 12 and context["pending"] == [anchor]
        assert "PRIVATE" not in json.dumps(context) and "traceback" not in json.dumps(context)
        denied, _ = await plugin.bridge.handle(context_request.model_copy(update={"session_id": "different"}))
        assert denied == 403
        successor = {"session_id": "new-chief-part", "chief_sessions": ("new-chief-part", request.session_id)}
        separate = {"session_id": "other-chief-thread", "chief_sessions": ("other-chief-thread", "other-archived-part")}
        assert await plugin.bridge.handle(context_request.model_copy(update=successor)) == (200, context)
        assert (await plugin.bridge.handle(context_request.model_copy(update=separate)))[0] == 403

        evidence = Request(method="GET", path=DETAILS, session_id=request.session_id, event_id=anchor["event_ref"])
        status, record = await plugin.bridge.handle(evidence)
        assert status == 200
        assert record["event"]["event_id"] == "escalation-fixture-run" and record["task"]["artifacts"] == ["app.html"]
        assert "logs" not in record
        assert await plugin.bridge.handle(evidence.model_copy(update=successor)) == (200, record)
        assert (await plugin.bridge.handle(evidence.model_copy(update=separate)))[0] == 403
        default = await plugin.bridge.handle(evidence.model_copy(update={"event_id": None}))
        assert default == (200, record)
        dag.tasks[0] = failed.model_copy(update={"error_msg": "CURRENT TASK ONLY"})
        await plugin.manager.save_atomic(dag)
        status, record = await plugin.bridge.handle(evidence.model_copy(update={"include_logs": True}))
        assert status == 200
        assert record["logs"]["latest_public"] == "public failure summary"
        assert record["logs"]["error"].endswith("pytest timed out") and record["logs"]["tail_truncated"]
        assert "PRIVATE" not in json.dumps(record) and "CURRENT TASK ONLY" not in json.dumps(record)
        status, _ = await plugin.bridge.handle(evidence.model_copy(update={"task_id": "T2"}))
        assert status == 404

        action = Request(method="POST", path=ACTION, session_id=request.session_id, project_id=project,
                         body='{"action":"RETRY","task_id":"T1"}')
        status, _ = await plugin.bridge.handle(action)
        assert status == 409
        status, _ = await plugin.bridge.handle(action.model_copy(update={"event_id": "older-failure"}))
        assert status == 409
        bound = action.model_copy(update={"event_id": anchor["event_ref"]})
        assert (await plugin.bridge.handle(bound.model_copy(update={"session_id": "different"})))[0] == 403
        assert (await plugin.bridge.handle(bound.model_copy(update=separate)))[0] == 403
        status, queued = await plugin.bridge.handle(bound.model_copy(update=successor))
        assert status == 200 and queued["status"] == "QUEUED" and queued["target_event_id"] == "escalation-fixture-run"
        assert await plugin.bridge.handle(bound) == (200, queued)
        event, = plugin.bus.inbox()
        assert event.event_id == queued["event_id"] and event.action_ref == "escalation-fixture-run"
        await plugin.daemon._apply(event)
        _, notices = await plugin.bridge.handle(Request(method="GET", path=NOTICES))
        resolved, = [notice for notice in notices["notices"] if notice["id"] == "escalation-fixture-run"]
        assert resolved["active"] is False and resolved["receipt"]["action"] == "RETRY"
        _, handled = await plugin.bridge.handle(evidence)
        assert handled["task"]["last_action"] == resolved["receipt"]
        outbox = Request.model_validate_json(json.dumps({"method": "GET", "path": OUTBOX, **successor}))
        assert (await plugin.bridge.handle(outbox))[1]["active"] is True
        assert await plugin.bridge.handle(outbox.model_copy(update=separate)) == (200, {"active": False})

        plugin.bridge.archive_project(plugin.manager.load())
        next_project = "proj_next"
        await plugin.manager.save_atomic(TaskDAG(
            project_id=next_project, project_status=ProjectStatus.ACTIVE, tasks=[task("T1", status=TaskStatus.FAILED)],
            binding=binding.model_copy(update={"project_id": next_project}),
        ))
        await publish(plugin.bus, make_outbox(
            OutboxEventType.ALERT_ESCALATED, project_id=next_project, event_id="future-failure",
            task_id="T1", reason_code="WORKER_FAILED", summary="NEXT PROJECT ONLY",
        ))
        archived = evidence.model_copy(update={"project_id": project})
        assert (await plugin.bridge.handle(archived))[0] == 200
        assert (await plugin.bridge.handle(archived.model_copy(update=successor)))[0] == 200
        assert (await plugin.bridge.handle(archived.model_copy(update=separate)))[0] == 403
        assert (await plugin.bridge.handle(archived.model_copy(update={"event_id": "future-failure"})))[0] == 404
        assert (await plugin.bridge.handle(archived.model_copy(update={"session_id": "different"})))[0] == 403

    asyncio.run(scenario())
