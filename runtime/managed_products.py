import asyncio
import base64
import hashlib
import json
import re
from uuid import uuid4

from core.product_state import ProductError, ProductForbidden, ProductStore, authorize, bind_product, register_version
from core.state_manager import write_atomic
from runtime.product_issues import issue_body, polling_since, reconcile_issues, triage_issue


DIGEST = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


class MailboxGitHub:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    async def __call__(self, operation, **arguments):
        stem = f"github-{uuid4().hex}"
        request = self.mailbox / f"{stem}.request.json"
        response = self.mailbox / f"{stem}.response.json"
        write_atomic(request, canonical({"operation": operation, **arguments}).decode())
        try:
            async with asyncio.timeout(90):
                while True:
                    try:
                        result = json.loads(response.read_text(encoding="utf-8"))
                        break
                    except FileNotFoundError:
                        await asyncio.sleep(0.1)
            if "error" in result:
                raise ProductError(str(result["error"])[:400])
            return result["value"]
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)


class ManagedProducts:
    def __init__(self, data_dir, github, planning):
        self.store = ProductStore(data_dir / "managed-products")
        self.github = github
        self.planning = planning

    def product_dir(self, repository_id):
        return self.store.workspace(repository_id, "v0.0.0").parent.parent

    def package_path(self, repository_id, digest):
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ProductError("Invalid workflow package digest")
        return self.product_dir(repository_id) / "packages" / digest

    def save_package(self, repository_id, frozen, dag):
        directory = self.package_path(repository_id, frozen.digest)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for name, payload in (("workflow.zip.b64", base64.b64encode(frozen.payload)), ("TASK_DAG.json", dag)):
            path = directory / name
            if path.exists():
                if path.read_bytes() != payload:
                    raise ProductError("Immutable workflow package content changed")
            else:
                write_atomic(path, payload.decode("utf-8"))

    def load_package(self, product, build):
        from runtime.workflow_package import load_workflow
        directory = self.package_path(product["repository_id"], build["artifact_sha256"])
        payload, dag = base64.b64decode((directory / "workflow.zip.b64").read_bytes(), validate=True), (directory / "TASK_DAG.json").read_bytes()
        if sha256(dag) != build["dag_sha256"]:
            raise ProductError("Frozen DAG digest mismatch")
        return load_workflow(payload, expected_digest=build["artifact_sha256"],
                             expected_commit=build["commit_sha"], expected_dag_digest=build["dag_sha256"])

    def listings(self, actor):
        return [self.view(product) for product in self.store.read()["products"].values()
                if isinstance(actor, str) and actor in product["roles"].values()]

    def view(self, product):
        from runtime.product_release import delivery_projection
        from runtime.product_trials import trial_progress
        return {
            "repository_id": product["repository_id"], "repository": product["repository"]["fullName"],
            "title": product["title"], "roles": product["roles"], "grant": product["grant"],
            "versions": list(product["versions"].values()), "issues": list(product["issues"].values()),
            "trials": trial_progress(product), "delivery": delivery_projection(product),
            "trial_history": [trial_progress(product, value) for value in product.get("builds", {})],
            "recent_runs": list(product["runs"].values())[-10:], "sync_error": product.get("sync_error"),
        }

    async def sync_product(self, state, product):
        try:
            remote = await self.github("issues", repository=product["repository"], since=polling_since(product))
            reconcile_issues(product, remote)
            product.pop("sync_error", None)
        except (ProductError, OSError, TimeoutError) as error:
            product["sync_error"] = str(error)[:400]
        self.store.put(state, product)

    async def poll(self):
        async with self.store.lock:
            state = self.store.read()
            for product in state["products"].values():
                await self.sync_product(state, product)

    async def attach(self, state, actor, args):
        if args.get("confirmed") is not True:
            raise ProductForbidden("Confirm the project scope and repository in the Chief setup panel")
        if args.get("create") is True:
            repository = await self.github("provision", owner=args["owner"], name=args["name"],
                                           accountType=args.get("account_type", "personal"))
        else:
            repository = await self.github("verify", owner=args["owner"], name=args["name"])
        existing = state["products"].get(str(repository["id"]))
        if existing:
            authorize(existing, actor, "chief")
            return self.view(existing)
        product = bind_product(repository, title=args["title"], actor=actor, pm=args["pm"], usage=args["usage"],
                               rollback=args.get("rollback"), budget=args["budget"])
        self.store.put(state, product)
        return self.view(product)

    async def handle(self, actor, args, *, chief=False, human=False, source=None):
        if not isinstance(args, dict) or not actor:
            raise ProductForbidden("Open this action from an assigned DS Bot conversation")
        operation = args.get("operation")
        async with self.store.lock:
            state = self.store.read()
            if operation == "list":
                return {"products": self.listings(actor)}
            if operation == "attach":
                if not chief or not human:
                    raise ProductForbidden("Bind a product from the Chief setup panel")
                return await self.attach(state, actor, args)
            product = self.store.product(state, args.get("repository_id"))
            authorize(product, actor, "chief", "pm", "usage")
            if operation == "status":
                view = self.view(product)
                if args.get("version") in product["versions"] and hasattr(self.planning, "status"):
                    view["plan"] = self.planning.status(product, product["versions"][args["version"]])
                return view
            if operation == "artifact":
                run = product["runs"].get(args.get("run_id"))
                item = next((a for a in (run or {}).get("artifacts", []) if a["path"] == args.get("artifact")), None)
                if not item or run["status"] != "completed":
                    raise ProductError("Choose a verified artifact from a completed product run")
                return {**item, "repository_id": product["repository_id"], "id": run["id"], "attempt": run["attempt"]}
            if operation == "start":
                authorize(product, actor, "chief", "pm")
                if not human:
                    raise ProductForbidden("Confirm the version plan in the project panel")
                return await self.planning.start(product, product["versions"][args["version"]])
            if operation == "run":
                authorize(product, actor, "usage")
                return self.run(state, product, source, args)
            if operation == "report_issue":
                authorize(product, actor, "usage")
                return await self.report_issue(state, product, source, args)
            if operation == "accept_trial":
                authorize(product, actor, "chief", "pm")
                if not human:
                    raise ProductForbidden("Confirm subjective acceptance in the project panel")
                from runtime.product_trials import verify_trial
                verify_trial(product, args["build_id"], run_id=args["run_id"], verifier_kind="human",
                                       verdict=args["verdict"], evidence_refs=[f"human:{source}"], verifier_id=actor)
                self.store.put(state, product)
                return self.view(product)
            if operation == "check_trial":
                authorize(product, actor, "chief")
                if not human:
                    raise ProductForbidden("Start an objective trial check from the authenticated Chief client")
                return self.check_trial(state, product, args)
            authorize(product, actor, "pm", "chief" if operation in ("plan", "sync", "issue") else "pm")
            return await self.manage(state, product, args)

    def check_trial(self, state, product, args):
        from runtime.product_trials import verify_trial
        from runtime.workflow_rule_verify import verify_generated_tool
        run = product["runs"].get(args["run_id"])
        if not run or run["status"] != "completed" or run["candidate_id"] != args["build_id"]:
            raise ProductError("Choose an actual completed run on this frozen candidate")
        output = self.product_dir(product["repository_id"]) / "runs" / run["id"] / str(run["attempt"])
        evidence = verify_generated_tool(output, args["expectation"], expected_manifest_digest=run["manifest_sha256"],
                                         expected_workflow_digest=run["build"]["artifact_sha256"])
        verify_trial(product, args["build_id"], run_id=run["id"], verifier_kind="rule", verdict="pass",
                     evidence_refs=[f"objective:{run['id']}:{evidence['sha256']}"], verifier_id="autoloop-offline-tool-v1")
        run["objective_check"] = evidence
        self.store.put(state, product)
        return {"run_id": run["id"], "evidence": evidence}

    async def report_issue(self, state, product, source, args):
        if not source:
            raise ProductForbidden("Issue reports require a user conversation turn")
        run = product["runs"].get(args.get("run_id"))
        if not run or run["status"] != "completed":
            raise ProductError("Choose a completed run in this product")
        marker = sha256(canonical([source, run["id"]]))[:32]
        pending = product["pending_issues"].get(marker)
        if pending and "number" in pending:
            return {"number": pending["number"]}
        title = args.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ProductError("Provide a short GitHub issue title")
        body = issue_body(run, args.get("repro"), args.get("actual"), args.get("expected"), marker)
        if pending and (pending["title"], pending["body"]) != (title, body):
            raise ProductError("This issue request is already pending with different content")
        product["pending_issues"][marker] = {"title": title, "body": body}
        self.store.put(state, product)
        result = await self.github("create_issue", repository=product["repository"], title=title, body=body, marker=marker)
        reconcile_issues(product, [result])
        product["pending_issues"][marker]["number"] = result["number"]
        self.store.put(state, product)
        return {"number": result["number"]}

    async def manage(self, state, product, args):
        operation = args["operation"]
        repository = product["repository"]
        if operation == "sync":
            await self.sync_product(state, product)
        elif operation == "issue":
            return await self.github("issue", repository=repository, number=args["number"])
        elif operation == "triage":
            issue = triage_issue(product, args["number"], args["decision"], args.get("duplicate"))
            marker = sha256(canonical([issue["number"], issue["triage"], issue["duplicate"]]))[:32]
            detail = f" into #{issue['duplicate']}" if issue["duplicate"] else ""
            await self.github("triage", repository=repository, number=issue["number"],
                              body=f"Autoloop triage: {issue['triage']}{detail}.", marker=marker,
                              decision=issue["triage"], duplicate=issue["duplicate"])
        elif operation == "reopen":
            if str(args["number"]) not in product["issues"]:
                raise ProductError("Choose an issue in this product")
            updated = await self.github("reopen", repository=repository, number=args["number"])
            reconcile_issues(product, [updated])
        elif operation == "plan":
            return await self.plan(state, product, args)
        elif operation == "revise":
            from runtime.product_trials import trial_progress
            version = product["versions"].get(args["version"])
            if not version or version.get("released") or version.get("interrupted"):
                raise ProductError("Revisions apply to a version still under development")
            if trial_progress(product) is not None or product.get("delivery", {}).get("pending"):
                raise ProductError("Pause trial and finish any pending delivery before revising")
            if version["project_id"] and hasattr(self.planning, "status"):
                if hasattr(self.planning, "retire"):
                    settled = await self.planning.retire(product, version)
                else:
                    current = self.planning.status(product, version)
                    settled = current and current["status"] in ("COMPLETED", "IDLE")
                if not settled:
                    raise ProductError("Finish current work, or pause and settle all writes before revising")
            if version["project_id"]:
                prior = {key: version.get(key) for key in ("project_id", "build_id", "revision")}
                prior["revision"] = prior["revision"] or 1
                version.setdefault("history", []).append(prior)
                version.update(revision=prior["revision"] + 1, project_id=None)
            self.store.put(state, product)
            receipt = await self.planning.prepare(product, version, args)
            version["project_id"] = receipt["project_id"]
        elif operation == "candidate":
            return await self.freeze(state, product, args)
        elif operation in ("pause_candidate", "resume_candidate"):
            from runtime.product_trials import activate_candidate, pause_candidate
            if operation == "pause_candidate":
                pause_candidate(product, args["build_id"])
            else:
                build = product.get("builds", {}).get(args["build_id"])
                if not build:
                    raise ProductError("Unknown frozen build")
                self.load_package(product, build)
                activate_candidate(product, build)
        elif operation in ("publish", "rollback", "continue_delivery"):
            return await self.deliver(state, product, args)
        else:
            raise ProductError("Unknown managed product operation")
        self.store.put(state, product)
        return self.view(product)

    async def plan(self, state, product, args):
        numbers = args.get("issue_numbers", [])
        if not isinstance(numbers, list) or any(type(n) is not int or n < 1 for n in numbers):
            raise ProductError("Invalid version Issue list")
        version = register_version(product, args["version"], args.get("base"), sorted(set(numbers)))
        if version["project_id"] is not None:
            return self.view(product)
        if args.get("chief_session"):
            product["chief_session"] = args["chief_session"]
        if not product.get("chief_session"):
            raise ProductError("Begin the initial version from its Chief conversation")
        self.store.put(state, product)
        receipt = await self.planning.prepare(product, version, args)
        version["project_id"] = receipt["project_id"]
        self.store.put(state, product)
        return self.view(product)

    async def freeze(self, state, product, args):
        from runtime.product_trials import activate_candidate, trial_progress
        from runtime.workflow_package import freeze_workflow, freeze_workflow_files, load_workflow
        version = product["versions"].get(args["version"])
        if not version or version.get("released"):
            raise ProductError("Choose an unreleased workflow version")
        repository = product["repository"]
        source_dir, dag = self.planning.accepted(product, version)
        preview = freeze_workflow(source_dir, commit="0" * 40, dag_snapshot=dag)
        frozen_sources = load_workflow(preview.payload, expected_digest=preview.digest).source_files()
        files = {name: content.decode("utf-8") for name, content in frozen_sources.items()}
        if version.get("accepted_digest") == preview.digest and version.get("build_id"):
            self.load_package(product, product["builds"][version["build_id"]])
            return self.view(product)
        if trial_progress(product) is not None:
            raise ProductError("Pause the active candidate before integrating a replacement")
        base_ref = version["base"] or repository["defaultBranch"]
        expected_base = version.get("integration_sha") or await self.github(
            "ref", repository=repository, ref=base_ref, kind="tag" if version["base"] else "branch")
        if isinstance(expected_base, dict):
            expected_base = expected_base["sha"]
        pr = await self.github("submit_workflow", repository=repository, version=version["version"],
                               expected_base=expected_base, files=files, marker=preview.digest)
        version["pull_request"] = pr
        self.store.put(state, product)
        integration = await self.github("merge_workflow", repository=repository, number=pr["number"],
                                        head=pr["head"], expected_base=expected_base, files=files)
        commit = integration.get("sha")
        if not isinstance(commit, str) or not COMMIT.fullmatch(commit) or integration.get("workflow_files") != files:
            raise ProductError("GitHub integration differs from the accepted workflow source")
        frozen = freeze_workflow_files(frozen_sources, commit=commit, dag_snapshot=dag)
        self.save_package(product["repository_id"], frozen, dag)
        build = {"id": frozen.digest, "version": version["version"], "repository": product["repository_id"],
                 "commit_sha": commit, "artifact_sha256": frozen.digest, "dag_sha256": frozen.dag_digest,
                 "issue_numbers": version["issue_numbers"]}
        version.update(build_id=build["id"], accepted_digest=preview.digest, integration_sha=commit)
        product.setdefault("builds", {})[build["id"]] = build
        activate_candidate(product, build)
        self.store.put(state, product)
        return self.view(product)

    def read_entry(self, repository_id):
        try:
            return json.loads((self.product_dir(repository_id) / "default-entry.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def run(self, state, product, source, args):
        from runtime.product_release import current_deployed_identity
        from runtime.product_trials import record_trial, trial_progress
        from runtime.workflow_runner import run_workflow, verify_run_artifacts
        if not source:
            raise ProductForbidden("Run a workflow from an actual Usage Bot user turn")
        repository_id = product["repository_id"]
        retry_id = args.get("retry_run_id")
        if retry_id and retry_id not in product["runs"]:
            raise ProductError("Choose an existing run in this product to retry")
        run_id = retry_id or sha256(canonical([repository_id, source]))[:32]
        previous = product["runs"].get(run_id)
        candidate_id = args.get("candidate_id")
        idea = args.get("idea")
        if previous and (previous["candidate_id"] != candidate_id or previous["idea"] != idea):
            raise ProductError("Retry uses the original input and pinned build")
        if previous and previous["status"] == "completed":
            return previous
        if previous:
            build = previous["build"]
        elif candidate_id:
            build = product.get("builds", {}).get(candidate_id)
            progress = trial_progress(product, candidate_id)
            if not build or not progress or progress.get("status") != "active":
                raise ProductError("This build is not the active candidate")
        else:
            build = current_deployed_identity(product, product["grant"]["target"])
            if not build:
                raise ProductError("This product has no released default workflow yet")
        workflow = self.load_package(product, build)
        workload_ref = (previous or {}).get("workload_ref", source)
        run = {"id": run_id, "status": "running", "build": build, "candidate_id": candidate_id, "workload_ref": workload_ref,
               "idea": idea, "attempt": (previous or {}).get("attempt", 0) + 1}
        product["runs"][run_id] = run
        self.store.put(state, product)
        directory = self.product_dir(repository_id) / "runs" / run_id
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        attempt = directory / str(run["attempt"])
        try:
            result = run_workflow(workflow, idea=idea, output_dir=attempt, workflow_version=build["version"])
            result = verify_run_artifacts(attempt, expected_manifest_digest=result.manifest_digest,
                                          expected_workflow_digest=build["artifact_sha256"])
            run.update(status="completed", entry=result.entry,
                       artifacts=[entry.model_dump(mode="json") for entry in result.artifacts],
                       manifest_sha256=result.manifest_digest)
            if candidate_id:
                evidence = [f"run:{run_id}:{result.manifest_digest}"]
                record_trial(product, candidate_id, run_id=run_id, artifact_sha256=build["artifact_sha256"],
                             workload_ref=workload_ref, evidence_refs=evidence)
        except (ValueError, OSError) as error:
            run.update(status="failed", error=str(error)[:400])
            self.store.put(state, product)
            raise ProductError(run["error"]) from error
        self.store.put(state, product)
        return run

    async def deliver(self, state, product, args):
        from runtime.product_release import advance_operation, begin_release, begin_rollback, delivery_projection
        operation = args["operation"]
        product_id = product["repository_id"]
        target = product["grant"]["target"]
        build = product.get("builds", {}).get(args.get("build_id"))
        if operation != "continue_delivery" and not build:
            raise ProductError("Choose an existing frozen build")
        if operation == "publish":
            begin_release(product, build, operation_id=f"release:{build['id']}", target=target)
        elif operation == "rollback":
            if not product["grant"]["rollback"]:
                raise ProductForbidden("This product has no preauthorized rollback scope")
            current = self.read_entry(product_id)
            if not current:
                raise ProductError("There is no deployed workflow to roll back")
            self.load_package(product, build)
            pending = delivery_projection(product).get("pending")
            rolling_back = pending and pending["request"]["kind"] == "rollback"
            operation_id = pending["request"]["id"] if rolling_back else f"rollback:{uuid4().hex}"
            expected = pending["expected"] if rolling_back else current
            begin_rollback(product, operation_id=operation_id, target=target, expected_build_id=expected["id"],
                           restore_build_id=build["id"], compatibility_ref="autoloop:offline-workflow-v1")
        self.store.put(state, product)
        github = ProductReleasePort(self, product)
        entry = DefaultWorkflowEntry(self, product)
        for _ in range(100):
            if not delivery_projection(product).get("pending"):
                break
            await advance_operation(product, github=github, entry=entry)
            self.store.put(state, product)
        projection = delivery_projection(product)
        current = projection["current"].get(target)
        if current and not projection.get("pending"):
            released = product["versions"].get(current["version"])
            if released:
                released["released"] = True
                for interrupted in projection["interrupted"].values():
                    product["versions"][interrupted["build"]["version"]]["interrupted"] = True
                self.store.put(state, product)
        return self.view(product)


class ProductReleasePort:
    def __init__(self, service, product):
        self.service = service
        self.product = product

    async def ensure_release(self, repository_id, build, operation_id):
        if repository_id != self.product["repository_id"]:
            raise ProductForbidden("Release belongs to another product")
        self.service.load_package(self.product, build)
        directory = self.service.package_path(repository_id, build["artifact_sha256"])
        return await self.service.github("ensure_release", repository=self.product["repository"], build=build,
                                         operation_id=operation_id,
                                         payload=(directory / "workflow.zip.b64").read_text(encoding="utf-8"),
                                         dag=base64.b64encode((directory / "TASK_DAG.json").read_bytes()).decode())

    async def close_issue(self, repository_id, number, operation_id):
        if repository_id != self.product["repository_id"]:
            raise ProductForbidden("Issue belongs to another product")
        result = await self.service.github("close_issue", repository=self.product["repository"], number=number,
                                           operation_id=operation_id)
        if isinstance(result, dict):
            reconcile_issues(self.product, [result])
        elif str(number) in self.product["issues"]:
            self.product["issues"][str(number)]["state"] = "closed"


class DefaultWorkflowEntry:
    def __init__(self, service, product):
        self.service = service
        self.product = product
        self.repository_id = product["repository_id"]

    async def read_identity(self, target):
        if target != self.product["grant"]["target"]:
            raise ProductForbidden("This product has a different deployment target")
        return self.service.read_entry(self.repository_id)

    async def compare_and_switch(self, target, expected, desired, operation_id):
        current = await self.read_identity(target)
        if current != desired:
            if current != expected:
                raise ProductError("The default workflow changed during deployment")
            self.service.load_package(self.product, desired)
            write_atomic(self.service.product_dir(self.repository_id) / "default-entry.json",
                         canonical(desired).decode())

    async def verify(self, target, desired):
        from runtime.workflow_runner import run_workflow, verify_run_artifacts
        actual = await self.read_identity(target)
        if actual != desired:
            raise ProductError("The default workflow did not resolve to the expected build")
        workflow = self.service.load_package(self.product, actual)
        directory = self.service.product_dir(self.repository_id) / "probes"
        directory.mkdir(mode=0o700, exist_ok=True)
        output = directory / uuid4().hex
        result = run_workflow(workflow, idea="离线工作流健康核验", output_dir=output, workflow_version=actual["version"])
        verify_run_artifacts(output, expected_manifest_digest=result.manifest_digest,
                             expected_workflow_digest=actual["artifact_sha256"])
        return f"probe:{result.manifest_digest}"
