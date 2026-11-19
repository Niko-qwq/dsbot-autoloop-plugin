import asyncio
import json

from api.bridge import ACTION, COMMUNICATION, NOTICES, PANEL, Request
from core.bus import make_inbox, make_outbox
from core.schema import (
    InboxEventType, OutboxEventType, ProjectBinding, ProjectReport,
    ProjectStatus, TaskDAG, TaskEscalation, TaskStatus, WorkerRun,
)
from test_draft_receipt import _new_plugin, _request
from test_phase1 import publish, task


async def _active_project(plugin, *tasks):
    chief = _request()
    project = "proj_chief_communication"
    dag = TaskDAG(project_id=project, project_status=ProjectStatus.ACTIVE, tasks=list(tasks), binding=ProjectBinding(
        project_id=project, session_id=chief.session_id, route=chief.route,
    ))
    await plugin.manager.save_atomic(dag)
    return dag, chief.session_id


def _failed_task(task_id, reason):
    run = WorkerRun(run_id=task_id + "-run", started_at=1, finished_at=2,
                    status="INTERRUPTED" if reason == "WORKER_INTERRUPTED" else "FAILED",
                    escalation=TaskEscalation(reason_code=reason, summary="Worker requires attention"))
    return task(task_id, status=TaskStatus.FAILED).model_copy(update={"worker_runs": (run,)})


def test_failed_feedback_has_native_delivery_and_idempotent_retry(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        dag, chief = await _active_project(plugin, task("export", status=TaskStatus.PENDING))
        dag.project_status = ProjectStatus.ALERT
        await plugin.manager.save_atomic(dag)
        for event_id in ("prior-issue", "current-issue"):
            issue = make_inbox(InboxEventType.USER_FEEDBACK, event_id=event_id, project_id=dag.project_id, summary="Compare another result")
            issue.consumed = True
            await plugin.bus.append_inbox(issue)
            await publish(plugin.bus, make_outbox(OutboxEventType.PROJECT_ALERT, project_id=dag.project_id,
                                                  event_id=f"feedback-error-{event_id}", summary="Plan failed"))
        _, data = await plugin.bridge.handle(Request(method="GET", path=NOTICES))
        cards = {card["id"]: card for card in data["notices"]}
        assert "feedback-error-prior-issue" not in cards
        _, panel = await plugin.bridge.handle(_call(PANEL, dag.project_id, chief))
        assert {row["feedback"]["retry_event_id"] for row in panel["inbox"]} == {
            "feedback-error-prior-issue", "feedback-error-current-issue",
        }
        card = cards["feedback-error-current-issue"]
        assert card["active"] and card["native_delivery_pending"]
        status, receipt = await plugin.bridge.handle(_call(COMMUNICATION, dag.project_id, chief, card["id"],
                                                            {"operation": "deliver_claim", "start_seq": 1}))
        assert status == 200 and receipt["delivery"]["facts"]["kind"] == "PROJECT_ALERT"
        retry = _call(ACTION, dag.project_id, chief, "feedback-error-current-issue", {"action": "RETRY_FEEDBACK"})
        first, second = await asyncio.gather(plugin.bridge.handle(retry), plugin.bridge.handle(retry))
        assert first == second and first[0] == 200
        pending, = await plugin.bus.pending_inbox()
        assert pending.event_id == first[1]["event_id"] and pending.action_ref == "current-issue"
        assert pending.payload.summary == "Compare another result"
        assert plugin.manager.load().tasks == dag.tasks

        remove = _call(ACTION, dag.project_id, chief, "prior-issue", {"action": "DELETE_FEEDBACK"})
        assert (await plugin.bridge.handle(remove.model_copy(update={"project_id": "other"})))[0] == 409
        assert (await plugin.bridge.handle(remove.model_copy(update={"session_id": "other-session"})))[0] == 403
        assert (await plugin.bridge.handle(remove))[0] == 200
        removed = next(event for event in plugin.bus.inbox() if event.event_id == "prior-issue")
        assert removed.consumed and removed.deleted_at is not None
        _, panel = await plugin.bridge.handle(_call(PANEL, dag.project_id, chief))
        assert [item["event_id"] for item in panel["inbox"]] == [pending.event_id]
        _, notices = await plugin.bridge.handle(Request(method="GET", path=NOTICES))
        assert all(card["id"] != "feedback-error-prior-issue" for card in notices["notices"])
        assert (await plugin.bridge.handle(retry.model_copy(update={"event_id": "feedback-error-prior-issue"})))[0] == 409
        assert (await plugin.bridge.handle(remove.model_copy(update={"event_id": pending.event_id})))[0] == 200
        assert (await plugin.bridge.handle(retry))[0] == 409
        assert not await plugin.bus.pending_inbox()

    asyncio.run(scenario())


def _call(path, project, session, event=None, body=None, chain=None):
    return Request(
        method="POST" if body is not None else "GET", path=path, session_id=session, chief_sessions=chain,
        project_id=project, event_id=event, body=json.dumps(body) if body is not None else None,
    )


def test_native_delivery_keeps_original_request_until_a_visible_receipt(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        dag, chief = await _active_project(plugin, _failed_task("T1", "BLOCKED_PERMISSION"))
        event = make_outbox(OutboxEventType.ALERT_ESCALATED, project_id=dag.project_id, event_id="escalation-T1-run", task_id="T1",
                            summary="The required document needs write permission.",
                            reason_code="BLOCKED_PERMISSION")
        await publish(plugin.bus, event)

        async def deliver(operation, *, hop=False, **fields):
            session = "successor-chief" if hop else chief
            return await plugin.bridge.handle(_call(COMMUNICATION, dag.project_id, session, event.event_id,
                                                    {"operation": operation, **fields}, chain=(session, chief) if hop else None))

        _, data = await plugin.bridge.handle(Request(method="GET", path=NOTICES))
        notice, = data["notices"]
        assert notice["native_delivery_pending"]
        status, data = await deliver("deliver_claim", start_seq=10)
        assert status == 200 and data["claimed"]
        original = data["delivery"]
        key = original["request_id"]
        assert original["session_id"] == chief and original["facts"]["reason_code"] == "BLOCKED_PERMISSION"
        assert (await deliver("deliver_claim", start_seq=91, hop=True))[1] == {"delivery": original, "claimed": False}
        assert (await deliver("deliver_finish", request_id=key, turn=1, message_seq=9))[0] == 409
        assert (await deliver("deliver_attention", request_id=key, error="History unavailable", retryable=False))[0] == 200
        _, panel = await plugin.bridge.handle(_call(PANEL, None, chief))
        assert panel["notification_attention"][0]["request_id"] == key
        _, panel = await plugin.bridge.handle(_call(PANEL, None, "stranger"))
        assert panel["notification_attention"] == []
        status, data = await deliver("deliver_retry", request_id=key, hop=True, start_seq=91)
        assert status == 200 and data["delivery"]["request_id"] == key and data["delivery"]["session_id"] == chief
        status, data = await deliver("deliver_finish", request_id=key, turn=5, message_seq=21, hop=True, text="Blocked.")
        assert status == 200 and data["delivery"]["status"] == "DELIVERED"
        assert (await deliver("deliver_finish", request_id=key, turn=5, message_seq=21))[1] == data
        assert (await deliver("deliver_finish", request_id=key, turn=5, message_seq=22))[0] == 409

    asyncio.run(scenario())


def test_proven_notification_failure_retries_on_the_current_chief_session(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        dag, chief = await _active_project(plugin, task("T1", status=TaskStatus.COMPLETED))
        dag.project_status = ProjectStatus.COMPLETED
        await plugin.manager.save_atomic(dag)
        event = make_outbox(OutboxEventType.PROJECT_COMPLETED, project_id=dag.project_id, event_id=dag.completion_event_id,
                            summary="Accepted a text-only result.",
                            report=ProjectReport(done=1, total=1, outcomes=[], artifacts=[], delivery_verified=True))
        await publish(plugin.bus, event)

        async def deliver(operation, *, hop=False, **fields):
            session = "successor-chief" if hop else chief
            return await plugin.bridge.handle(_call(COMMUNICATION, dag.project_id, session, event.event_id,
                                                    {"operation": operation, **fields}, chain=(session, chief) if hop else None))

        status, data = await deliver("deliver_claim", start_seq=12)
        assert status == 200 and data["claimed"]
        key = data["delivery"]["request_id"]
        assert (await deliver("deliver_attention", request_id=key, error="Original turn ended without a message", retryable=True))[0] == 200
        status, data = await deliver("deliver_retry", request_id=key, hop=True, start_seq=8)
        retry = data["delivery"]
        assert status == 200 and retry["attempt"] == 2 and retry["request_id"] != key
        assert (retry["session_id"], retry["start_seq"]) == ("successor-chief", 8)
        assert (await deliver("deliver_finish", request_id=key, turn=8, message_seq=31, hop=True))[0] == 409

    asyncio.run(scenario())


def test_interrupted_worker_retry_requires_confirmation_of_current_alert(tmp_path):
    async def scenario():
        plugin = await _new_plugin(tmp_path)
        dag, chief = await _active_project(plugin, _failed_task("export", "WORKER_INTERRUPTED"))
        event = make_outbox(OutboxEventType.ALERT_ESCALATED, project_id=dag.project_id,
                            event_id="escalation-export-run", task_id="export", reason_code="WORKER_INTERRUPTED")
        await publish(plugin.bus, event)
        body = {"action": "RETRY", "task_id": "export"}
        unconfirmed = _call(ACTION, dag.project_id, chief, event.event_id, body)
        assert (await plugin.bridge.handle(unconfirmed))[0] == 409
        confirmed = _call(ACTION, dag.project_id, chief, event.event_id, {**body, "confirm_worker_stopped": True})
        assert (await plugin.bridge.handle(confirmed.model_copy(update={"event_id": "old-interruption"})))[0] == 409
        assert not await plugin.bus.pending_inbox()
        result = await plugin.bridge.handle(confirmed)
        assert result[0] == 200 and await plugin.bridge.handle(confirmed) == result
        retry, = await plugin.bus.pending_inbox()
        assert retry.confirm_worker_stopped and retry.action_ref == event.event_id
        await plugin.daemon._apply(retry)
        saved, = plugin.manager.load().tasks
        assert saved.status == TaskStatus.READY and saved.last_action.event_id == retry.event_id
        assert (await plugin.bridge.handle(confirmed))[1]["status"] == "APPLIED"

    asyncio.run(scenario())
