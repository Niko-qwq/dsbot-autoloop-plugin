import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue

from api.bridge import ActionConflict, AutoloopBridge, PMPlanChange, ProjectScopeDenied, Request
from config.constants import BRIDGE_POLL_SEC, MAX_AGENT_POLL_TICKS, MAX_RETRY
from core.bus import EventBus, make_outbox
from core.governance import GovernanceGate
from core.product_state import ProductError, ProductForbidden
from core.schema import (
    BudgetPolicy, InboxEventType, ModelRoute, NonEmptyText, NonnegativeCost, OutboxEventType, PMTaskVerdict,
    PlanPrepared, PlanningProgress, PrepareProject, ProjectBinding, ProjectStatus, StrictModel, TaskDAG,
)
from core.state_manager import DAGManager, StateFormatError, read_model, write_atomic
from runtime.codex_worker import CodexWorker
from runtime.daemon import AgentReply, AutoloopDaemon, ModelCallError
from runtime.managed_products import MailboxGitHub, ManagedProducts, canonical
from runtime.pm_agent import PMAgent, PMDelta, PMDraft
from runtime.router import Router
from runtime.watchdog import RollbackError, Watchdog


class ModelResponse(StrictModel):
    output: str | None
    cost_usd: NonnegativeCost | None
    error: str | None
    progress: PlanningProgress | None = None


class ProjectDraft(StrictModel):
    project_id: str
    request: PrepareProject
    status: Literal["QUEUED", "PLANNING", "FAILED"] = "QUEUED"
    error: str | None = None
    progress: PlanningProgress | None = None
    attempt: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)

    def view(self) -> dict[str, JsonValue]:
        return {
            "project_id": self.project_id, "status": self.status, "error": self.error,
            "title": " ".join(self.request.goal.split())[:100],
            **({"attempt": self.attempt} if self.attempt else {}),
            **({"pm_name": self.request.pm_name} if self.request.pm_name is not None else {}),
            **({"progress": self.progress.model_dump(mode="json")} if self.progress is not None else {}),
        }


class PrepareReceipt(StrictModel):
    project_id: NonEmptyText
    source_session_id: NonEmptyText
    source_message_id: NonEmptyText
    title: str
    pm_name: str | None = None

    def view(self, dag: TaskDAG) -> dict[str, JsonValue]:
        if dag.project_id != self.project_id:
            status = "COMPLETED"
        elif dag.project_status == ProjectStatus.IDLE:
            status = "PREPARED"
        else:
            status = dag.project_status.value
        return {
            "project_id": self.project_id, "status": status, "title": self.title,
            **({"pm_name": self.pm_name} if self.pm_name is not None else {}),
        }


class PrepareReceipts(StrictModel):
    receipts: list[PrepareReceipt]


def pm_tool_schema(model: type[StrictModel]) -> dict[str, JsonValue]:
    """Project the canonical model into the JSON Schema subset used by DSH tools."""
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})
    supported = ("type", "required", "additionalProperties", "enum", "const", "description")

    def project(node: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if reference := node.get("$ref"):
            return project(definitions[reference.removeprefix("#/$defs/")])
        result = {key: node[key] for key in supported if key in node}
        if "properties" in node:
            result["properties"] = {name: project(value) for name, value in node["properties"].items()}
        if "items" in node:
            result["items"] = project(node["items"])
        if "anyOf" in node:
            result["oneOf"] = [project(variant) for variant in node["anyOf"]]
        return result

    return project(schema)


class DSHModel:
    def __init__(self, mailbox: Path):
        self.mailbox = mailbox
        self._planning_calls: dict[str, Path] = {}

    def planning_progress(self, project_id: str) -> PlanningProgress | None:
        path = self._planning_calls.get(project_id)
        if path is None:
            return None
        try:
            return read_model(path, PlanningProgress)
        except FileNotFoundError:
            return None

    async def plan(
        self, project_id: str, directory: Path, route: ModelRoute, prompt: str, budget: float, draft: bool,
        *, revision: bool = False, acceptance: bool = False,
    ) -> AgentReply:
        mode = "acceptance" if acceptance else "revision" if revision else "draft" if draft else "feedback"
        call = f"agent-{uuid4().hex}"
        request = self.mailbox / f"{call}.request.json"
        response = self.mailbox / f"{call}.response.json"
        progress = self.mailbox / f"{call}.progress.json"
        planning = not acceptance
        model = PMTaskVerdict if acceptance else PMDraft if draft else PMDelta
        write_atomic(request, json.dumps({
            "kind": "pm", "project_id": project_id, "cwd": str(directory), "prompt": prompt,
            "route": route.model_dump(mode="json"), "budget_usd": budget, "mode": mode,
            "output_schema": pm_tool_schema(model),
        }))
        if planning:
            self._planning_calls[project_id] = progress
        try:
            for _ in range(MAX_AGENT_POLL_TICKS):
                if response.exists():
                    result = read_model(response, ModelResponse)
                    if result.error is not None:
                        raise ModelCallError(result.error, result.cost_usd, result.progress)
                    if result.output is None or result.cost_usd is None:
                        raise StateFormatError("Incomplete DSH model result")
                    return AgentReply(output=result.output, cost_usd=result.cost_usd)
                await asyncio.sleep(BRIDGE_POLL_SEC)
            raise TimeoutError(f"DSH request timed out: {response.name}")
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
            progress.unlink(missing_ok=True)
            if planning and self._planning_calls.get(project_id) == progress:
                del self._planning_calls[project_id]


class Plugin:
    def __init__(
        self, data_dir: Path, mailbox: Path, codex: str, *,
        codex_profile: str | None = None, codex_reasoning_effort: str = "xhigh",
        codex_network: bool | None = None,
        codex_computer_use: bool = False,
        managed_root: bool = True,
    ):
        self.mailbox = mailbox
        self.draft_path = data_dir / "project-draft.json"
        self.prepare_receipts_path = data_dir / "project-prepare-receipts.json"
        self.manager = DAGManager(data_dir / "workspace")
        self.bus = EventBus(self.manager.workspace, self.manager.lock)
        self.gate = GovernanceGate(self.manager, self.bus)
        self.model = DSHModel(mailbox)
        skills = Path(__file__).resolve().parent / "skills"
        computer_use = None
        companion = None
        if codex_computer_use:
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
            companion = home / "computer-use"
            client = (companion / "Codex Computer Use.app" / "Contents" / "SharedSupport"
                      / "SkyComputerUseClient.app" / "Contents" / "MacOS" / "SkyComputerUseClient")
            if client.is_file() and os.access(client, os.X_OK):
                computer_use = client
            else:
                logging.getLogger(__name__).warning("Codex Computer Use companion was not found in CODEX_HOME")
        self.worker = CodexWorker(
            self.manager, mailbox, codex, profile=codex_profile, network_access=codex_network,
            reasoning_effort=None if codex_reasoning_effort == "inherit" else codex_reasoning_effort,
            computer_use=computer_use, computer_use_cwd=companion if computer_use else None,
        )
        self.pm = PMAgent(self.gate, skills / "leader_decompose.md", self.model.plan)
        self.router = Router(Watchdog(self.gate), skills, self.worker)
        self.daemon = AutoloopDaemon(
            self.router, feedback=self.pm.feedback, read_planning_progress=self.model.planning_progress,
            task_acceptance=self.pm.accept_task,
        )
        self.bridge = AutoloopBridge(self.daemon, self.prepare, self.read_draft, self.retry_draft)
        self.managed = None
        if managed_root:
            def version_runtime(directory):
                return Plugin(directory, mailbox, codex, codex_profile=codex_profile,
                              codex_reasoning_effort=codex_reasoning_effort, codex_network=codex_network,
                              codex_computer_use=codex_computer_use, managed_root=False)

            self.version_plans = VersionPlans(version_runtime)
            self.managed = ManagedProducts(data_dir, MailboxGitHub(mailbox), self.version_plans)
            self.version_plans.service = self.managed

    async def initialize(self) -> None:
        workspace = self.manager.workspace
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.manager.dag_path.exists():
            if any(path.is_file() and path.stat().st_size for path in (self.bus.inbox_path, self.bus.outbox_path)):
                raise StateFormatError(f"Autoloop event files exist without a DAG in {workspace}")
            await self.bus.initialize()
            await self.manager.save_atomic(TaskDAG(project_id="idle", project_status=ProjectStatus.IDLE, tasks=[]))
        self.bus.inbox()
        self.bus.outbox()
        self.bridge.initialize_deliveries()
        dag = self.manager.load()
        if dag.tasks:
            if dag.binding is None:
                raise StateFormatError(f"Autoloop project has no Chief binding in {workspace}")
            await self.worker.initialize_project(dag.project_id)
        draft = self._load_draft()
        if draft and draft.project_id == dag.project_id:
            self._publish_initial_plan(draft.project_id, len(dag.tasks))
            self._record_prepare_receipt(draft)
            self.draft_path.unlink()
        elif draft and draft.status == "PLANNING":
            self._save_draft(draft.model_copy(update={"status": "FAILED", "error": "PM 规划在服务重启时中断，可在草稿上重试。"}))
        await self.daemon.recover()

    def _confirm_can_prepare(self, dag: TaskDAG) -> None:
        if dag.tasks and dag.project_status != ProjectStatus.COMPLETED:
            raise ActionConflict("The workspace already has a project awaiting confirmation or execution")
        if any(event.type == InboxEventType.USER_FEEDBACK and not event.consumed
               for event in self.bus.inbox(dag.project_id)):
            raise ActionConflict("The PM is handling an issue for the current project")

    def _load_draft(self) -> ProjectDraft | None:
        try:
            return read_model(self.draft_path, ProjectDraft)
        except FileNotFoundError:
            return None

    def _save_draft(self, draft: ProjectDraft) -> None:
        write_atomic(self.draft_path, draft.model_dump_json() + "\n")

    def _prepare_receipts(self) -> PrepareReceipts:
        try:
            return read_model(self.prepare_receipts_path, PrepareReceipts)
        except FileNotFoundError:
            return PrepareReceipts(receipts=[])

    @staticmethod
    def _prepare_source(request: PrepareProject) -> tuple[str, str] | None:
        brief = request.brief
        if brief is None:
            return None
        return brief.source_session_id or request.session_id, brief.source_message_id

    def _record_prepare_receipt(self, draft: ProjectDraft) -> None:
        source = self._prepare_source(draft.request)
        if source is None:
            return
        receipts = self._prepare_receipts()
        for receipt in receipts.receipts:
            if (receipt.source_session_id, receipt.source_message_id) == source:
                if receipt.project_id != draft.project_id:
                    raise StateFormatError("A Chief message has more than one project assignment")
                return
        receipts.receipts.append(PrepareReceipt(
            project_id=draft.project_id, source_session_id=source[0], source_message_id=source[1],
            title=" ".join(draft.request.goal.split())[:100], pm_name=draft.request.pm_name,
        ))
        write_atomic(self.prepare_receipts_path, receipts.model_dump_json() + "\n")

    def _publish_initial_plan(self, project_id: str, task_count: int) -> None:
        self.bus._append_outbox(make_outbox(
            OutboxEventType.PROJECT_PREPARED, project_id=project_id, event_id=f"prepared-{project_id}",
            summary=f"项目方案已就绪，共 {task_count} 项任务，等待确认启动。",
            plan=PlanPrepared(mode="INITIAL", approval_token=f"start-{project_id}", task_count=task_count),
        ))

    def read_draft(self, session_ids: tuple[str, ...] | str | None, project_id: str | None = None) -> dict[str, JsonValue] | None:
        draft = self._load_draft()
        if draft is None:
            return None
        if isinstance(session_ids, str):
            session_ids = (session_ids,)
        matches = draft.project_id == project_id if project_id is not None else draft.request.session_id in (session_ids or ())
        if not matches:
            return None
        if draft.status == "PLANNING":
            draft.progress = self.model.planning_progress(draft.project_id)
        return draft.view()

    async def prepare(self, request: PrepareProject, chief_sessions: tuple[str, ...] | None = None) -> dict[str, JsonValue]:
        async with self.manager.lock:
            dag = self.manager.load()
            session_ids = chief_sessions if chief_sessions is not None else (request.session_id,)
            source = self._prepare_source(request)
            if source is not None:
                if source[0] not in session_ids:
                    raise ActionConflict("The original message belongs to a different Chief conversation")
                for receipt in self._prepare_receipts().receipts:
                    if receipt.source_session_id in session_ids and receipt.source_message_id == source[1]:
                        return receipt.view(dag)
            draft = self._load_draft()
            if draft is not None and source is not None:
                existing_source = self._prepare_source(draft.request)
                if existing_source is not None and existing_source[0] in session_ids and existing_source[1] == source[1]:
                    return draft.view()
            self._confirm_can_prepare(dag)
            if draft and draft.status in ("QUEUED", "PLANNING"):
                if (source is None and draft.request.brief is None and draft.request.session_id in session_ids
                        and draft.request.goal == request.goal):
                    return draft.view()
                raise ActionConflict("A project draft is already being prepared")
            draft = ProjectDraft(project_id=f"proj_{uuid4().hex[:12]}", request=request)
            self._save_draft(draft)
            return draft.view()

    async def retry_draft(self, project_id: str, attempt: int, chief_sessions: tuple[str, ...] | None) -> dict[str, JsonValue]:
        async with self.manager.lock:
            draft = self._load_draft()
            if draft is None or draft.project_id != project_id:
                raise ActionConflict("This project draft is no longer available")
            if chief_sessions is not None and draft.request.session_id not in chief_sessions:
                raise ProjectScopeDenied("This draft belongs to a different Chief conversation")
            if attempt > draft.attempt:
                raise ActionConflict("Refresh the draft before retrying")
            if attempt < draft.attempt or draft.status != "FAILED":
                return draft.view()
            self._confirm_can_prepare(self.manager.load())
            queued = draft.model_copy(update={
                "status": "QUEUED", "attempt": draft.attempt + 1, "error": None, "progress": None,
            })
            self._save_draft(queued)
            return queued.view()

    async def _plan_draft(self, draft: ProjectDraft) -> None:
        request, project_id = draft.request, draft.project_id
        async with self.manager.lock:
            if self._load_draft() != draft or draft.status != "QUEUED":
                return
            self._save_draft(draft.model_copy(update={"status": "PLANNING"}))
        try:
            directory = await self.worker.initialize_project(project_id)
            plan, dag, cost = await self.pm.draft(project_id, request, directory)
            async with self.manager.lock:
                current = self.manager.load()
                self._confirm_can_prepare(current)
                if current.tasks:
                    self.bridge.archive_project(current)
                binding = ProjectBinding(
                    project_id=project_id, session_id=request.session_id, route=request.route, name=plan.name,
                    pm_name=request.pm_name, brief=request.brief,
                )
                budget = BudgetPolicy(max_total_cost_usd=request.max_budget_usd, current_cost_usd=cost,
                                      max_task_retries=MAX_RETRY)
                self.manager._save(dag.model_copy(update={"binding": binding, "budget": budget}))
                self._publish_initial_plan(project_id, len(dag.tasks))
                self._record_prepare_receipt(draft)
                self.draft_path.unlink()
        except (ActionConflict, ModelCallError, OSError, RollbackError, StateFormatError, TimeoutError) as error:
            async with self.manager.lock:
                if self.manager.load().project_id == project_id:
                    raise
                progress = error.progress if isinstance(error, ModelCallError) else None
                self._save_draft(draft.model_copy(update={"status": "FAILED", "error": str(error)[:600], "progress": progress}))

    async def _serve_request(self, path: Path, request: Request) -> None:
        try:
            if self.managed and request.path == "/api/autoloop/product":
                data = await self.managed.handle(request.actor_id, json.loads(request.body or '{}'),
                                                 chief=request.actor_chief, human=request.actor_human,
                                                 source=request.source_id)
                status = 200
            elif self.managed and request.path == "/api/autoloop/notices":
                async with self.manager.lock:
                    data = self.bridge._notices()
                data["history"].extend(await self.version_plans.notices())
                status = 200
            elif self.managed and (runtime := self.version_plans.by_project(request.project_id)):
                if request.method == "POST" and request.path in ("/api/autoloop/pm-plan", "pm-plan-proposal"):
                    raise ProductError("Revise a managed workflow explicitly in its product panel")
                if request.method == "POST" and request.path == "/api/autoloop/action":
                    body = json.loads(request.body or '{}')
                    if body.get("action") in ("USER_FEEDBACK", "DELETE_FEEDBACK", "RETRY_FEEDBACK"):
                        raise ProductError("Use the product GitHub Issue inbox for maintenance")
                status, data = await runtime.bridge.handle(request)
            else:
                status, data = await self.bridge.handle(request)
        except ProductForbidden as error:
            status, data = 403, {"error": str(error)}
        except (ValueError, ActionConflict) as error:
            status, data = 409, {"error": str(error)}
        except KeyError:
            status, data = 400, {"error": "Missing managed product action field"}
        except (ModelCallError, OSError, RollbackError, StateFormatError, TimeoutError) as error:
            status, data = 500, {"error": f"{type(error).__name__}: {error}"}
        if path.exists():
            destination = path.with_name(path.name.replace(".request.json", ".response.json"))
            write_atomic(destination, json.dumps({"status": status, "data": data}, ensure_ascii=False))
            if not path.exists():
                destination.unlink(missing_ok=True)

    async def serve(self, parent: int) -> None:
        await self.initialize()
        write_atomic(self.mailbox / "ready.json", json.dumps({
            "ok": True, "pm_plan_change_schema": pm_tool_schema(PMPlanChange),
        }) + "\n")
        async with asyncio.TaskGroup() as services:
            daemon = services.create_task(self.daemon.run_forever())
            draft_job = None
            requests = {}
            if self.managed:
                await self.version_plans.restore(services)
                poller = services.create_task(self._poll_products())
            while os.getppid() == parent:
                if draft_job is None or draft_job.done():
                    draft = self._load_draft()
                    if draft and draft.status == "QUEUED":
                        draft_job = services.create_task(self._plan_draft(draft))
                for path in sorted(self.mailbox.glob("ui-*.request.json")):
                    reply = path.with_name(path.name.replace(".request.json", ".response.json"))
                    if reply.exists() or path in requests:
                        continue
                    try:
                        request = read_model(path, Request)
                    except FileNotFoundError:
                        continue  # The owner cancelled this mailbox request.
                    if self.managed and request.path == "/api/autoloop/product":
                        job = requests[path] = services.create_task(self._serve_request(path, request))
                        job.add_done_callback(lambda _, request_path=path: requests.pop(request_path, None))
                    else:
                        await self._serve_request(path, request)
                await asyncio.sleep(BRIDGE_POLL_SEC)
            if self.managed:
                poller.cancel()
                self.version_plans.stop()
            daemon.cancel()
            if draft_job is not None:
                draft_job.cancel()

    async def _poll_products(self):
        while True:
            try:
                await self.version_plans.publish_facts()
                await self.managed.poll()
            except (ValueError, OSError) as error:
                logging.getLogger(__name__).warning("Autoloop product polling: %s", error)
            await asyncio.sleep(60)


class VersionPlans:
    def __init__(self, make_runtime):
        self.make_runtime = make_runtime
        self.runtimes = {}
        self.jobs = []

    async def restore(self, services):
        self.services = services
        for product in self.service.store.read()["products"].values():
            for version in product["versions"].values():
                await self.runtime(product, version)

    async def runtime(self, product, version):
        key = self.key(product, version)
        runtime = self.runtimes.get(key)
        if runtime is None:
            runtime = self.make_runtime(self.directory(*key))
            await runtime.initialize()
            self.runtimes[key] = runtime
            if not version.get("released") and not version.get("interrupted"):
                self.jobs.append(self.services.create_task(runtime.daemon.run_forever()))
                self.jobs.append(self.services.create_task(self.drafts(runtime)))
        return runtime

    @staticmethod
    def key(product, version):
        return product["repository_id"], version["version"], version.get("revision", 1)

    def directory(self, repository_id, version, revision):
        directory = self.service.store.workspace(repository_id, version)
        return directory if revision == 1 else directory / "revisions" / str(revision)

    async def drafts(self, runtime):
        while True:
            draft = runtime._load_draft()
            if draft and draft.status == "QUEUED":
                await runtime._plan_draft(draft)
            await asyncio.sleep(BRIDGE_POLL_SEC)

    def stop(self):
        for job in self.jobs:
            job.cancel()

    def by_project(self, project_id):
        if project_id:
            for runtime in self.runtimes.values():
                draft = runtime._load_draft()
                if runtime.manager.load().project_id == project_id or (draft and draft.project_id == project_id):
                    return runtime
        return None

    async def publish_facts(self):
        from runtime.product_release import delivery_projection
        for product in self.service.store.read()["products"].values():
            for operation in delivery_projection(product)["completed"].values():
                version = product["versions"][operation["build"]["version"]]
                runtime = self.by_project(version["project_id"])
                if runtime is None:
                    continue
                rollback = operation["request"]["kind"] == "rollback"
                event_type = OutboxEventType.PRODUCT_ROLLED_BACK if rollback else OutboxEventType.PRODUCT_RELEASED
                async with runtime.manager.lock:
                    runtime.bus._append_outbox(make_outbox(event_type, project_id=version["project_id"],
                        event_id=operation["fact_id"],
                        summary=f"默认工作流已核验为 {version['version']}。"))

    async def notices(self):
        await self.publish_facts()
        result = []
        for runtime in self.runtimes.values():
            async with runtime.manager.lock:
                batch = runtime.bridge._notices()
            if batch.get("project") and batch.get("notices"):
                batch["project"]["static_preview"] = False
                result.append(batch)
        return result

    async def prepare(self, product, version, args):
        runtime = await self.runtime(product, version)
        session = product["chief_session"]
        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 5600:
            raise ProductError("Provide a bounded workflow development brief")
        instruction = ("交付可反复运行的小红书小工具生产工作流。版本交付物是根目录 workflow.json 和声明的 resources 文本。"
                       '最小清单 {"schema":1,"title":"小工具生产","steps":["research","generate","validate","package"],'
                       '"tool":{"kind":"checklist","labels":["检查输入","核对结果"]},'
                       '"allowed_kinds":["checklist","calculator","template"],"resources":[]}。'
                       "请先调研，核对声明与测试预期，再生成清单及回归用例。工作流执行器由插件提供，使用 Bot 在发布后提交小工具 Idea。")
        request = PrepareProject.model_validate({
            "session_id": session, "route": args["route"], "goal": f"{goal}\n\n{instruction}",
            "max_budget_usd": product["grant"]["budget"],
            "brief": {"source_session_id": session, "source_message_id": f"managed-{version['version']}",
                      "user_instruction": goal,
                      "acceptance": ["调研适用范围和交付形式", "工作流清单可运行且核对正常和边界样例"],
                      "authorization": "在产品工作目录开发已授权的离线小工具生产工作流。"},
        })
        receipt = await runtime.prepare(request, (session,))
        base_build = version.get("build_id")
        if not base_build and version["base"]:
            base_build = product["versions"][version["base"]]["build_id"]
        if base_build:
            workflow = self.service.load_package(product, product["builds"][base_build])
            directory = runtime.manager.project_directory(receipt["project_id"])
            for name, content in workflow.source_files().items():
                path = directory / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not path.exists():
                    write_atomic(path, content.decode("utf-8"))
        return receipt

    async def start(self, product, version):
        from api.bridge import ACTION
        runtime = await self.runtime(product, version)
        dag = runtime.manager.load()
        if dag.project_id != version["project_id"]:
            raise ProductError("The version plan is still being prepared")
        result = await runtime.bridge.handle(Request(method="POST", path=ACTION, project_id=dag.project_id,
            session_id=product["chief_session"], event_id=dag.start_approval_base,
            body='{"action":"START_PROJECT","task_id":null}'))
        if result[0] != 200:
            raise ProductError(result[1].get("error", "Unable to start version"))
        return result[1]

    def accepted(self, product, version):
        directory = self.directory(*self.key(product, version))
        manager = DAGManager(directory / "workspace")
        dag = manager.load()
        if dag.project_id != version["project_id"] or dag.project_status != ProjectStatus.COMPLETED:
            raise ProductError("Complete PM acceptance of the version DAG before freezing")
        snapshot = dag.model_dump(mode="json", exclude={"binding"})
        return manager.project_directory(dag.project_id), canonical(snapshot)

    def status(self, product, version):
        runtime = self.runtimes.get(self.key(product, version))
        if runtime is None:
            return None
        draft = runtime.read_draft(None, version["project_id"])
        if draft:
            return {"status": "planning", "draft_status": draft["status"], "error": draft["error"]}
        dag = runtime.manager.load()
        if dag.project_id != version["project_id"]:
            return None
        return {"status": dag.project_status.value, "tasks": [
            {"task_id": task.task_id, "title": task.title, "status": task.status.value} for task in dag.tasks
        ]}


async def main(args: argparse.Namespace) -> None:
    current = asyncio.current_task()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, current.cancel)
    plugin = Plugin(
        args.data_dir, args.mailbox, args.codex, codex_profile=args.codex_profile,
        codex_reasoning_effort=args.codex_reasoning_effort,
        codex_network={"inherit": None, "enabled": True, "disabled": False}[args.codex_network],
        codex_computer_use=args.codex_computer_use,
    )
    workspace = plugin.manager.workspace
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    writer = os.open(workspace / ".writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StateFormatError(f"Autoloop already has a writer for {workspace}") from error
        await plugin.serve(args.parent)
    except asyncio.CancelledError:
        pass
    finally:
        os.close(writer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DSH Autoloop Python runtime")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--mailbox", required=True, type=Path)
    parser.add_argument("--parent", required=True, type=int)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--codex-profile")
    parser.add_argument("--codex-reasoning-effort", default="xhigh")
    parser.add_argument("--codex-network", choices=("enabled", "inherit", "disabled"), default="inherit")
    parser.add_argument("--codex-computer-use", action="store_true")
    asyncio.run(main(parser.parse_args()))
