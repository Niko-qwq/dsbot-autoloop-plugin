import asyncio

from core.bus import make_inbox
from core.schema import InboxEventType, ProjectStatus, UIActionType
from runtime.daemon import AutoloopDaemon
from runtime.router import Router
from runtime.watchdog import Watchdog
from test_phase1 import task
from test_phase2 import workspace_gate
from test_phase3 import unused, worker_skills


def test_start_receipt_expires_when_a_new_issue_changes_the_plan(tmp_path):
    async def scenario():
        gate = await workspace_gate(tmp_path / "workspace", tasks=[task("T1")])
        skills = await worker_skills(tmp_path, gate)
        daemon = AutoloopDaemon(Router(Watchdog(gate), skills, unused), feedback=unused, task_acceptance=unused)
        dag = gate.manager.load()
        dag.project_status = ProjectStatus.IDLE
        await gate.manager.save_atomic(dag)
        queued = make_inbox(
            InboxEventType.USER_COMMAND, project_id=dag.project_id, summary=UIActionType.START_PROJECT.value,
            action_ref=dag.start_approval_base,
        )
        await gate.bus.append_inbox(queued)
        await gate.bus.append_inbox(make_inbox(
            InboxEventType.USER_FEEDBACK, project_id=dag.project_id, summary="A new decision is needed",
        ))
        await daemon._apply(queued)
        assert gate.manager.load().project_status == ProjectStatus.IDLE
        assert gate.manager.load().started_at is None

    asyncio.run(scenario())
