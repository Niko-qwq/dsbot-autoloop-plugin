import asyncio
import base64
import hashlib
import json
import os
import re
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from pydantic import Field

from core.schema import PMTaskCheck, StrictModel, Task, TaskPaths
from core.state_manager import DAGManager, StateFormatError, read_model, write_atomic
from runtime.watchdog import RollbackError, WorkerCancelled, WorkerCleanupError, WorkerResult, _git


STDERR_TAIL_BYTES = 4096
MAX_EVENT_BYTES = 256 * 1024
STOP_TIMEOUT_SEC = 10
TASK_FILENAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")


class CodexWorkerCheck(PMTaskCheck):
    evidence_refs: TaskPaths


class CodexReport(StrictModel):
    summary: str
    output_artifacts: list[str]
    checks: Annotated[list[CodexWorkerCheck], Field(max_length=13)]


class _SavedCheckpoint(StrictModel):
    project_id: str
    task_id: str
    task_spec: str
    baseline_tree: str | None
    patch: str
    summary: str
    changed_files: list[str]
    progress: list[str]


@dataclass
class _WorkerAttempt:
    project_id: str
    task: Task
    directory: Path
    baseline_tree: str | None
    progress: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    summary: str = ""
    current_command: str = ""


class CodexWorker:
    def __init__(
        self,
        manager: DAGManager,
        scratch: Path,
        executable: str,
        *,
        profile: str | None = None,
        reasoning_effort: str | None = None,
        network_access: bool | None = None,
        computer_use: Path | None = None,
        computer_use_cwd: Path | None = None,
    ):
        if reasoning_effort is not None and not reasoning_effort.strip():
            raise ValueError("Codex worker reasoning effort must be nonempty")
        if computer_use is not None and (
            not computer_use.is_absolute() or not computer_use.is_file() or not os.access(computer_use, os.X_OK)
        ):
            raise ValueError("The Computer Use MCP client must be an absolute executable file path")
        if computer_use_cwd is not None and (not computer_use_cwd.is_absolute() or not computer_use_cwd.is_dir()):
            raise ValueError("The Computer Use MCP working directory must be an absolute directory path")
        if computer_use is None and computer_use_cwd is not None:
            raise ValueError("The Computer Use MCP working directory requires a client")
        self.manager = manager
        self.scratch = scratch
        self.executable = executable
        self.profile = profile
        self.reasoning_effort = reasoning_effort
        self.network_access = network_access
        self.computer_use = computer_use
        self.computer_use_cwd = computer_use_cwd

    async def initialize_project(self, project_id: str) -> Path:
        directory = self.manager.project_directory(project_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.resolve() != directory or (directory / ".git").is_symlink():
            raise StateFormatError(f"Unexpected link in the project directory: {directory}")
        if not (directory / ".git").is_dir():
            await _git(directory, "init", "-q", root=directory)
        return directory

    async def __call__(self, task: Task, skill: str, goal: str) -> WorkerResult:
        dag = self.manager.load()
        directory = await self.initialize_project(dag.project_id)
        if not TASK_FILENAME.fullmatch(task.task_id):
            raise StateFormatError(f"Task {task.task_id!r} cannot be used as a Codex log filename")
        dependencies = []
        for node in dag.tasks:
            if node.task_id not in task.dependencies:
                continue
            summary = next((run.detail for run in reversed(node.worker_runs) if run.status == "COMPLETED"), "")
            if not node.output_artifacts and not summary:
                continue
            parts = [f"{node.task_id} · {node.title}"]
            if node.output_artifacts:
                parts.append("交付文件 · " + ", ".join(node.output_artifacts))
            if summary:
                parts.append("完成摘要 · " + summary[:2000])
            dependencies.append("\n".join(parts))
        prompt = (f"{skill}\n\n## 项目计划\n{goal}\n\n## {task.task_id} · {task.title}\n"
                  f"\n验收要求 · {task.acceptance or '按目标检查结果并注明验收证据'}\n")
        if task.phase:
            prompt += f"\n计划阶段 · {task.phase}\n"
        if task.acceptance_refs:
            prompt += "\n用户验收项 · " + ", ".join(task.acceptance_refs) + "。在结果中按编号记录实测结论与证据。\n"
        prompt += "\n本轮检查项编号 · " + ", ".join(("TASK", *task.acceptance_refs)) + "。TASK 对应本任务的验收要求。\n"
        if dag.binding is not None and dag.binding.brief is not None:
            prompt += f"\n用户授权范围 · {dag.binding.brief.authorization}\n"
        if task.error_msg:
            prompt += f"\n前一轮验收或执行记录 · {task.error_msg[:1000]}\n核对已有成果，在原任务和授权范围内只补做尚未满足的部分。\n"
        if dependencies:
            prompt += "\n前置任务结果\n" + "\n\n".join(dependencies) + "\n"
        writable = task.permission_tier >= 2
        baseline_tree = None
        if not writable:
            prompt += "\n该任务只读。把结果放入最终摘要，并列出相关的已有文件。\n"
        else:
            await _git(directory, "add", "-A", "--", ".", root=directory)
            baseline_tree = (await _git(directory, "write-tree", root=directory)).decode().strip()
        attempt = _WorkerAttempt(dag.project_id, task, directory, baseline_tree)
        checkpoint_path = self._checkpoint_path(attempt)
        try:
            prompt += await self._resume(attempt, checkpoint_path)
            with TemporaryDirectory(prefix="codex-", dir=self.scratch) as temporary:
                schema = Path(temporary) / "report.schema.json"
                output = Path(temporary) / f"{task.task_id}.log"
                write_atomic(schema, json.dumps(CodexReport.model_json_schema()))
                await self._execute(attempt, schema, output, prompt)
                report = read_model(output, CodexReport)
                if not report.summary.strip():
                    raise StateFormatError(f"Codex returned an empty summary for {task.task_id}")
                artifacts = []
                unavailable = []
                for supplied in report.output_artifacts:
                    artifact = (directory / supplied).resolve()
                    if not artifact.is_relative_to(directory):
                        raise StateFormatError(f"Codex reported an invalid project artifact: {supplied}")
                    if not artifact.is_file():
                        unavailable.append(artifact.relative_to(directory).as_posix())
                        continue
                    path = str(artifact)
                    if path not in artifacts:
                        artifacts.append(path)
                if unavailable:
                    paths = json.dumps(list(dict.fromkeys(unavailable))[:16], ensure_ascii=False)
                    report.summary = f"Worker 报告的路径未能确认为实际文件 · {paths}\n\n{report.summary}"
                checkpoint_path.unlink(missing_ok=True)
                return WorkerResult(cost_usd=0.0, summary=report.summary, output_artifacts=tuple(artifacts),
                                    checks=tuple(report.checks))
        except (Exception, asyncio.CancelledError) as error:
            if not isinstance(error, WorkerCleanupError):
                try:
                    await self._capture(attempt, str(error))
                except (OSError, StateFormatError, RollbackError, TimeoutError) as capture_error:
                    if writable:
                        raise WorkerCleanupError(f"{error}\nCodex checkpoint failed: {capture_error}") from capture_error
                if baseline_tree is not None:
                    try:
                        await _git(directory, "read-tree", baseline_tree, root=directory)
                        # Use the same working-tree ignore rules that the checkpoint used.
                        await _git(directory, "clean", "-fd", "--", ".", root=directory)
                        if await _git(directory, "ls-files", "-z", root=directory):
                            await _git(directory, "checkout", "--", ".", root=directory)
                    except (OSError, RollbackError, TimeoutError) as rollback:
                        raise WorkerCleanupError(f"{error}\nCodex project checkout failed: {rollback}") from rollback
            raise

    def _checkpoint_path(self, attempt: _WorkerAttempt) -> Path:
        project_key = hashlib.sha256(attempt.project_id.encode()).hexdigest()
        path = self.manager.workspace / "worker_checkpoints" / project_key / attempt.task.task_id / "checkpoint.json"
        if path.resolve() != path:
            raise WorkerCleanupError(f"Unexpected link in the worker checkpoint path: {path}")
        return path

    @staticmethod
    def _task_spec(task: Task) -> str:
        fields = {"title", "skill_ref", "dependencies", "permission_tier", "acceptance", "acceptance_refs", "phase", "task_revision"}
        return hashlib.sha256(task.model_dump_json(include=fields).encode()).hexdigest()

    async def _capture(self, attempt: _WorkerAttempt, failure: str) -> None:
        path = self._checkpoint_path(attempt)
        patch = b""
        changed_files = []
        baseline_tree = attempt.baseline_tree
        if baseline_tree is not None:
            directory = attempt.directory
            with TemporaryDirectory(prefix="codex-index-", dir=self.scratch) as temporary:
                index = Path(temporary) / "index"
                await _git(directory, "read-tree", baseline_tree, root=directory, index_file=index)
                await _git(directory, "add", "-A", "--", ".", root=directory, index_file=index)
                current_tree = (await _git(directory, "write-tree", root=directory, index_file=index)).decode().strip()
            names = await _git(directory, "diff-tree", "--no-commit-id", "-r", "--name-only", "-z",
                               baseline_tree, current_tree, "--", root=directory)
            changed_files = [name.decode(errors="replace") for name in names.split(b"\0") if name]
            patch = await _git(directory, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
                               "--no-color", "--src-prefix=a/", "--dst-prefix=b/", baseline_tree, current_tree,
                               "--", root=directory)
        progress = list(attempt.progress)
        if attempt.current_command:
            progress.append(f"最近开始的命令（未收到退出结果） · {attempt.current_command}")
        if failure:
            progress.append(f"本轮停止原因 · {failure[-1600:]}")
        saved = _SavedCheckpoint(
            project_id=attempt.project_id, task_id=attempt.task.task_id, task_spec=self._task_spec(attempt.task),
            baseline_tree=baseline_tree, patch=base64.b64encode(patch).decode("ascii"),
            summary=attempt.summary or "已记录当前变更和可用的命令结果。", changed_files=changed_files, progress=progress,
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(path, saved.model_dump_json() + "\n")

    async def _resume(self, attempt: _WorkerAttempt, path: Path) -> str:
        if not path.is_file():
            return ""
        try:
            saved = read_model(path, _SavedCheckpoint)
            patch = base64.b64decode(saved.patch, validate=True)
        except (OSError, ValueError) as error:
            raise WorkerCleanupError(f"Codex could not read checkpoint {path}: {error}") from error
        if (saved.project_id, saved.task_id, saved.task_spec) != (
            attempt.project_id, attempt.task.task_id, self._task_spec(attempt.task),
        ):
            return ""
        resume = "\n## 上一轮执行进度\n"
        if patch:
            with TemporaryDirectory(prefix="codex-resume-", dir=self.scratch) as temporary:
                patch_path = Path(temporary) / "workspace.patch"
                patch_path.write_bytes(patch)
                directory = attempt.directory
                try:
                    await _git(directory, "apply", "--binary", "--whitespace=nowarn", str(patch_path), root=directory)
                except (OSError, RollbackError, TimeoutError) as error:
                    # A restarted daemon may find the existing patch already present in the working tree.
                    if saved.baseline_tree == attempt.baseline_tree:
                        raise WorkerCleanupError(f"Codex could not restore checkpoint {path}: {error}") from error
                    try:
                        await _git(directory, "apply", "--binary", "--reverse", "--check", str(patch_path), root=directory)
                    except (OSError, RollbackError, TimeoutError):
                        raise WorkerCleanupError(f"Codex checkpoint conflicts with the current project files: {path}\n{error}") from error
            resume += "工作区已含检查点中的变更。请从已有实现继续，并完成剩余验证。\n"
        else:
            resume += "请检查当前文件，复用仍适用的结果，并继续尚未完成的步骤。\n"
        attempt.summary = saved.summary
        attempt.progress.extend(record for record in saved.progress if record.startswith("命令 · "))
        resume += f"{saved.summary}\n"
        if saved.changed_files:
            resume += "涉及文件 · " + ", ".join(saved.changed_files) + "\n"
        return resume + "\n".join(saved.progress) + "\n"

    async def _execute(self, attempt: _WorkerAttempt, schema: Path, output: Path, prompt: str) -> None:
        mode = "workspace-write" if attempt.baseline_tree is not None else "read-only"
        config_args = ["--profile", self.profile] if self.profile else []
        if self.reasoning_effort is not None:
            config_args.extend(("-c", f"model_reasoning_effort={json.dumps(self.reasoning_effort, ensure_ascii=False)}"))
        project = json.dumps(str(attempt.directory), ensure_ascii=False)
        # This project is generated by Workers. Apply trust only to this CLI invocation.
        config_args.extend(("-c", f'projects={{{project}={{trust_level="untrusted"}}}}'))
        if mode == "workspace-write":
            config_args.extend(("-c", "sandbox_workspace_write.writable_roots=[]"))
            if self.network_access is not None:
                network = "true" if self.network_access else "false"
                config_args.extend(("-c", f"sandbox_workspace_write.network_access={network}"))
        if self.computer_use is not None:
            command = json.dumps(str(self.computer_use), ensure_ascii=False)
            cwd = json.dumps(str(self.computer_use_cwd or self.computer_use.parent), ensure_ascii=False)
            enabled = "true" if mode == "workspace-write" else "false"
            config_args.extend(("-c", f'mcp_servers.computer-use={{command={command},args=["mcp"],cwd={cwd},enabled={enabled}}}'))
        process = await asyncio.create_subprocess_exec(
            self.executable, "-a", "never", "exec", "--sandbox", mode,
            *config_args, "--ephemeral", "--color", "never", "--json",
            "--output-schema", str(schema), "--output-last-message", str(output), "-",
            cwd=attempt.directory, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        tail = bytearray()

        async def drain() -> None:
            while chunk := await process.stderr.read(STDERR_TAIL_BYTES):
                tail.extend(chunk)
                del tail[:-STDERR_TAIL_BYTES]

        async def communicate() -> int:
            try:
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # The exit code and stderr identify the CLI failure.
            finally:
                process.stdin.close()
            return await process.wait()

        stderr = asyncio.create_task(drain())
        exit_code = asyncio.create_task(communicate())
        progress = asyncio.create_task(self._read_progress(process.stdout, attempt))
        settled = asyncio.gather(exit_code, stderr, progress)
        try:
            code, _, _ = await asyncio.shield(settled)
        except (Exception, asyncio.CancelledError) as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(settled), timeout=STOP_TIMEOUT_SEC)
            except TimeoutError as stop_error:
                raise WorkerCleanupError(f"Codex process group {process.pid} did not exit") from stop_error
            if isinstance(error, asyncio.CancelledError):
                raise WorkerCancelled(self._stderr(tail)) from error
            raise
        if code != 0:
            raise RuntimeError(f"Codex exited with code {code}\n{self._stderr(tail)}")

    @staticmethod
    async def _read_progress(stdout: asyncio.StreamReader, attempt: _WorkerAttempt) -> None:
        pending = bytearray()
        oversized = False
        while chunk := await stdout.read(16 * 1024):
            lines = chunk.split(b"\n")
            for line in lines[:-1]:
                if not oversized and len(pending) + len(line) <= MAX_EVENT_BYTES:
                    pending.extend(line)
                    CodexWorker._record_progress(pending, attempt)
                pending.clear()
                oversized = False
            if not oversized:
                if len(pending) + len(lines[-1]) <= MAX_EVENT_BYTES:
                    pending.extend(lines[-1])
                else:
                    pending.clear()
                    oversized = True
        if pending and not oversized:
            CodexWorker._record_progress(pending, attempt)

    @staticmethod
    def _record_progress(line: bytearray, attempt: _WorkerAttempt) -> None:
        try:
            event = json.loads(line)
        except (UnicodeError, ValueError, RecursionError):
            return
        if not isinstance(event, dict) or event.get("type") not in ("item.started", "item.completed"):
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        if event["type"] == "item.started":
            if item.get("type") == "command_execution" and isinstance(item.get("command"), str):
                attempt.current_command = item["command"][:500]
            return
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            attempt.summary = item["text"].strip()[:1600]
        if item.get("type") == "command_execution" and isinstance(item.get("command"), str):
            output = item.get("aggregated_output")
            excerpt = output.strip()[-2500:] if isinstance(output, str) else ""
            command = item["command"][:500]
            exit_code = item.get("exit_code")
            if command == attempt.current_command:
                attempt.current_command = ""
            code = exit_code if isinstance(exit_code, int) else "未报告"
            attempt.progress.append(f"命令 · {command}\n退出码 · {code}\n{excerpt}")

    @staticmethod
    def _stderr(tail: bytearray) -> str:
        return tail.decode(errors="replace").strip()[-1500:]
