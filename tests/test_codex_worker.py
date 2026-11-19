import asyncio
import json
import sys

import pytest

from core.schema import InboxEventType, TaskStatus, WorkerRun
from plugin_entry import Plugin
from runtime.codex_worker import CodexWorker
from runtime.router import Router
from runtime.watchdog import Watchdog
from test_phase1 import task
from test_phase2 import workspace_gate
from test_phase3 import worker_skills


@pytest.mark.parametrize("outcome", ["fast", "exit"])
def test_codex_subprocess_result_rollback_and_stderr(tmp_path, outcome):
    async def scenario():
        verified = task("R1", status=TaskStatus.COMPLETED)
        research = verified.model_copy(update={
            "phase": "RESEARCH", "worker_runs": (verified.worker_runs[-1].model_copy(update={
                "detail": "Research found the host uses pytest",
            }),),
        })
        node = task("T1", ("R1",), status=TaskStatus.READY).model_copy(update={
            "permission_tier": 2, "acceptance": "File exists", "phase": "IMPLEMENT", "acceptance_refs": ("C1", "C2"),
        })
        gate = await workspace_gate(tmp_path / "workspace", tasks=[research, node])
        skills = await worker_skills(tmp_path, gate)
        executable = tmp_path / "fake-codex"
        executable.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:3] == ['-a', 'never', 'exec']
assert args[args.index('--sandbox') + 1] == 'workspace-write'
assert '--ignore-user-config' not in args
assert 'sandbox_workspace_write.writable_roots=[]' in args
assert not any(arg.startswith('sandbox_workspace_write.network_access=') for arg in args)
assert not any(arg.startswith('model_reasoning_effort=') for arg in args)
prompt = sys.stdin.read()
assert 'File exists' in prompt and 'IMPLEMENT' in prompt
assert '用户验收项 · C1, C2' in prompt
assert 'Research found the host uses pytest' in prompt
Path('tracked.txt').write_text('changed')
Path('new.txt').write_text('deliverable')
if {outcome == 'exit'}:
    print('x' * 6000 + ' worker stderr tail', file=sys.stderr, flush=True)
    sys.exit(7)
result = Path(args[args.index('--output-last-message') + 1])
result.write_text(json.dumps({{'summary': 'done', 'output_artifacts': ['new.txt'], 'checks': []}}))
''', encoding="utf-8")
        executable.chmod(0o700)
        worker = CodexWorker(gate.manager, tmp_path, str(executable))
        project = await worker.initialize_project("p1")
        saved = project / "tracked.txt"
        saved.write_text("baseline", encoding="utf-8")
        router = Router(Watchdog(gate), skills, worker)
        await asyncio.gather(*await router.dispatch_ready_tasks())
        event, = await gate.bus.pending_inbox()
        assert event.type == InboxEventType.TASK_EXECUTION_FINISHED
        if outcome != "exit":
            assert event.execution_failure is None
            assert event.payload.output_artifacts == (str(project / "new.txt"),)
        else:
            assert event.execution_failure.reason_code == "WORKER_FAILED" and event.execution_failure.retryable
            assert "worker stderr tail" in event.payload.summary
            assert saved.read_text() == "baseline" and not (project / "new.txt").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(("tier", "network", "effort"), [(1, True, "xhigh"), (2, True, "high"), (2, False, None)])
def test_codex_worker_passes_profile_and_tier_capabilities(tmp_path, tier, network, effort):
    async def scenario():
        node = task("T1", status=TaskStatus.READY).model_copy(update={"permission_tier": tier})
        gate = await workspace_gate(tmp_path / "workspace", tasks=[node])
        argv_path = tmp_path / "argv.json"
        executable = tmp_path / "fake-codex"
        executable.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(argv_path)!r}).write_text(json.dumps(args))
sys.stdin.read()
Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps({{'summary': 'done', 'output_artifacts': [], 'checks': []}}))
''', encoding="utf-8")
        executable.chmod(0o700)
        computer_use = tmp_path / "Computer Use"
        computer_use.touch()
        computer_use.chmod(0o700)
        computer_use_cwd = tmp_path / "Desktop client state"
        computer_use_cwd.mkdir()
        worker = CodexWorker(
            gate.manager, tmp_path, str(executable), profile="worker", network_access=network,
            reasoning_effort=effort,
            computer_use=computer_use, computer_use_cwd=computer_use_cwd,
        )
        await worker(node, "Test skill", "Project state")
        args = json.loads(argv_path.read_text(encoding="utf-8"))
        assert args[:3] == ["-a", "never", "exec"]
        assert args[args.index("--sandbox") + 1] == ("read-only" if tier == 1 else "workspace-write")
        assert args[args.index("--profile") + 1] == "worker"
        assert {"--ephemeral", "--output-schema", "--json"} <= set(args)
        assert "--ignore-user-config" not in args
        overrides = [args[index + 1] for index, arg in enumerate(args) if arg == "-c"]
        quoted_project = json.dumps(str(gate.manager.project_directory("p1")), ensure_ascii=False)
        quoted_command = json.dumps(str(computer_use), ensure_ascii=False)
        quoted_cwd = json.dumps(str(computer_use_cwd), ensure_ascii=False)
        expected = [f'projects={{{quoted_project}={{trust_level="untrusted"}}}}']
        if effort is not None:
            expected.insert(0, f'model_reasoning_effort="{effort}"')
        if tier >= 2:
            expected_network = "true" if network else "false"
            expected.extend([
                "sandbox_workspace_write.writable_roots=[]",
                f"sandbox_workspace_write.network_access={expected_network}",
            ])
        enabled = "true" if tier >= 2 else "false"
        expected.append(f'mcp_servers.computer-use={{command={quoted_command},args=["mcp"],cwd={quoted_cwd},enabled={enabled}}}')
        assert overrides == expected

    asyncio.run(scenario())


def test_plugin_defaults_to_xhigh_and_can_inherit_cli_reasoning(tmp_path):
    mailbox = tmp_path / "mailbox"
    assert Plugin(tmp_path, mailbox, "codex").worker.reasoning_effort == "xhigh"
    assert Plugin(tmp_path, mailbox, "codex", codex_reasoning_effort="inherit").worker.reasoning_effort is None


def test_worker_hands_missing_expected_file_and_per_criterion_feedback_to_pm(tmp_path, monkeypatch):
    async def scenario():
        node = task("T1", status=TaskStatus.RUNNING).model_copy(update={
            "permission_tier": 1, "acceptance": "Write docs/report.md", "error_msg": "Report is missing",
            "worker_runs": (WorkerRun(started_at=1.0, run_id="run-1"),),
        })
        gate = await workspace_gate(tmp_path / "workspace", tasks=[node])
        worker = CodexWorker(gate.manager, tmp_path, "unused")

        async def execute(_attempt, schema, output, prompt):
            assert "TASK" in prompt and "Report is missing" in prompt
            contract = json.loads(schema.read_text())
            assert "checks" in contract["required"]
            output.write_text(json.dumps({
                "summary": "Only a read-only investigation was possible",
                "output_artifacts": ["docs/report.md"],
                "checks": [{"criterion_id": "TASK", "status": "FAIL", "reason": "No write permission", "evidence_refs": []}],
            }), encoding="utf-8")

        monkeypatch.setattr(worker, "_execute", execute)
        result = await worker(node, "Worker skill", "Project state")
        assert not result.output_artifacts
        assert "docs/report.md" in result.summary
        assert result.checks[0].status == "FAIL"

    asyncio.run(scenario())
