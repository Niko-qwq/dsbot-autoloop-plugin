import asyncio
import json
import sys

import pytest

from core.schema import TaskStatus
from runtime.codex_worker import CodexWorker
from runtime.watchdog import WorkerCleanupError
from test_phase1 import task
from test_phase2 import workspace_gate


def test_failed_worker_checkpoint_restores_edits_and_recent_evidence(tmp_path):
    async def scenario():
        node = task("T1", status=TaskStatus.RUNNING).model_copy(update={"permission_tier": 2})
        gate = await workspace_gate(tmp_path / "workspace", tasks=[node])
        executable = tmp_path / "fake-codex"
        executable.write_text(f'''#!{sys.executable}
import json, subprocess, sys
from pathlib import Path

prompt = sys.stdin.read()
assert '--json' in sys.argv
if '上一轮执行进度' not in prompt:
    Path('tracked.txt').write_text('partial implementation')
    Path('obsolete.txt').unlink()
    Path('asset.bin').write_bytes(bytes(range(256)))
    subprocess.run(['git', 'add', '-A'], check=True)
    completed = {{'type': 'command_execution', 'command': 'pytest', 'exit_code': 1,
                 'aggregated_output': '4 passed, 1 failed'}}
    print(json.dumps({{'type': 'item.completed', 'item': completed}}), flush=True)
    print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message',
          'text': 'Implemented stage one; fixing the remaining test'}}}}), flush=True)
    Path('late-test-results.txt').write_text('5 passed')
    sys.exit(7)

assert Path('tracked.txt').read_text() == 'partial implementation'
assert not Path('obsolete.txt').exists()
assert Path('asset.bin').read_bytes() == bytes(range(256))
assert Path('late-test-results.txt').read_text() == '5 passed'
assert Path('independent.txt').read_text() == 'added since the checkpoint'
assert '4 passed, 1 failed' in prompt
assert 'Implemented stage one' in prompt
args = sys.argv
Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(
    {{'summary': 'verified the remaining test', 'output_artifacts': ['late-test-results.txt'], 'checks': []}}))
''', encoding="utf-8")
        executable.chmod(0o700)
        worker = CodexWorker(gate.manager, tmp_path, str(executable))
        project = await worker.initialize_project("p1")
        tracked = project / "tracked.txt"
        obsolete = project / "obsolete.txt"
        tracked.write_text("baseline")
        obsolete.write_text("older implementation")
        with pytest.raises(RuntimeError, match="Codex exited with code 7"):
            await worker(node, "Skill", "Project state")

        assert tracked.read_text() == "baseline"
        assert obsolete.read_text() == "older implementation"
        assert not (project / "asset.bin").exists()
        assert not (project / "late-test-results.txt").exists()
        manifest, = (gate.manager.workspace / "worker_checkpoints").glob("*/T1/checkpoint.json")
        saved = json.loads(manifest.read_text())
        assert set(saved["changed_files"]) == {"tracked.txt", "obsolete.txt", "asset.bin", "late-test-results.txt"}
        assert "4 passed, 1 failed" in "\n".join(saved["progress"])
        assert "Implemented stage one" in saved["summary"]
        (project / "independent.txt").write_text("added since the checkpoint")

        # A new Worker instance reads the checkpoint from disk, just like a daemon restart.
        retry = CodexWorker(gate.manager, tmp_path, str(executable))
        result = await retry(node, "Skill", "Project state")
        assert result.output_artifacts == (str(project / "late-test-results.txt"),)
        assert result.summary == "verified the remaining test"
        assert not manifest.exists()

    asyncio.run(scenario())


def test_retry_preserves_newer_workspace_files(tmp_path):
    async def scenario():
        node = task("T1", status=TaskStatus.RUNNING).model_copy(update={"permission_tier": 2})
        gate = await workspace_gate(tmp_path / "workspace", tasks=[node])
        executable = tmp_path / "fake-codex"
        executable.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
prompt = sys.stdin.read()
assert '上一轮执行进度' not in prompt
Path('tracked.txt').write_text('worker draft')
raise SystemExit(7)
''', encoding="utf-8")
        executable.chmod(0o700)
        worker = CodexWorker(gate.manager, tmp_path, str(executable))
        project = await worker.initialize_project("p1")
        tracked = project / "tracked.txt"
        tracked.write_text("baseline")
        with pytest.raises(RuntimeError, match="Codex exited with code 7"):
            await worker(node, "Skill", "Project state")
        manifest, = (gate.manager.workspace / "worker_checkpoints").glob("*/T1/checkpoint.json")
        tracked.write_text("newer worktree version")
        retry = CodexWorker(gate.manager, tmp_path, str(executable))
        original = manifest.read_bytes()
        with pytest.raises(WorkerCleanupError, match="checkpoint conflicts"):
            await retry(node, "Skill", "Project state")
        assert manifest.read_bytes() == original
        assert tracked.read_text() == "newer worktree version"

    asyncio.run(scenario())
