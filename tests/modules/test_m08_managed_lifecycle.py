import asyncio
import base64
import hashlib
import json
from copy import deepcopy
from zipfile import ZipFile

import pytest

from core.product_state import ProductError, ProductForbidden
from runtime.managed_products import ManagedProducts
from runtime.product_trials import trial_progress
from runtime.workflow_native import NATIVE_ARCHIVE, NATIVE_FILES, NATIVE_ROUTE


REPOSITORY = {"id": 4107, "owner": "example", "name": "tool-workflow", "fullName": "example/tool-workflow",
              "defaultBranch": "main", "cloneUrl": "https://github.com/example/tool-workflow.git",
              "visibility": "private", "installationId": 299}
TARGET = "ds-bot-local"


class GitHubFixture:
    def __init__(self):
        self.pull_requests = {}
        self.releases = {}
        self.issues = {}
        self.triage_comments = set()
        self.main_sha = "1" * 40

    def issue(self, number, title, body=""):
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open",
                               "updated_at": "2026-11-19T13:00:00Z"}
        return self.issues[number]

    async def __call__(self, operation, **args):
        if operation in ("verify", "provision"):
            assert (args["owner"], args.get("name", args.get("repo"))) == (REPOSITORY["owner"], REPOSITORY["name"])
            return dict(REPOSITORY)
        assert args["repository"] == REPOSITORY
        arguments = {key: value for key, value in args.items() if key != "repository"}
        return deepcopy(getattr(self, f"op_{operation}")(**arguments))

    def op_ref(self, ref, kind):
        return self.releases[ref]["commit_sha"] if kind == "tag" else self.main_sha

    def op_submit_workflow(self, version, expected_base, files, marker):
        assert expected_base in {self.main_sha, *(release["commit_sha"] for release in self.releases.values())}
        number = len(self.pull_requests) + 1
        assert len(marker) == 64
        record = {"number": number, "version": version, "head": f"{number + 10:040x}", "files": files,
                  "expected_base": expected_base}
        self.pull_requests[number] = record
        return record

    def op_merge_workflow(self, number, head, files, expected_base):
        record = self.pull_requests[number]
        assert (head, files, expected_base) == (record["head"], record["files"], record["expected_base"])
        self.main_sha = f"{number + 20:040x}"
        return {"sha": self.main_sha, "workflow_files": record["files"]}

    def op_ensure_release(self, build, operation_id, payload, dag):
        assert operation_id.endswith(build["id"])
        assert hashlib.sha256(base64.b64decode(payload)).hexdigest() == build["artifact_sha256"]
        assert hashlib.sha256(base64.b64decode(dag)).hexdigest() == build["dag_sha256"]
        release = build | {"url": f"https://github.com/example/tool-workflow/releases/tag/{build['version']}"}
        assert self.releases.setdefault(build["version"], release) == release
        return release

    def op_issues(self, since):
        return list(self.issues.values())

    def op_issue(self, number):
        return self.issues[number]

    def op_create_issue(self, title, body, marker):
        original = next((issue for issue in self.issues.values() if marker in issue["body"]), None)
        return original if original else self.issue(17, title, body)

    def op_triage(self, number, body, marker, decision, duplicate):
        assert self.issues[number]["state"] == "open"
        assert (decision, duplicate) == ("accepted", None)
        self.triage_comments.add((number, body, marker))

    def op_close_issue(self, number, operation_id):
        assert any(release["id"] in operation_id for release in self.releases.values())
        self.issues[number]["state"] = "closed"
        return self.issues[number]

    def op_reopen(self, number):
        self.issues[number]["state"] = "open"
        return self.issues[number]


class PlanningFixture:
    def __init__(self, root):
        self.root = root
        self.prepared = {}
        self.started = set()
        self.accepted_projects = set()

    async def prepare(self, product, version, args):
        number = version["version"]
        revision = version.get("revision", 1)
        record = {"project_id": f"project-{number}-r{revision}", "issues": list(version["issue_numbers"]),
                  "base": version["base"]}
        self.prepared[number] = record
        return record

    async def start(self, product, version):
        self.started.add(version["project_id"])
        return {"project_id": version["project_id"], "status": "ACTIVE"}

    def status(self, product, version):
        project_id = version["project_id"]
        status = "IDLE"
        if project_id in self.accepted_projects:
            status = "COMPLETED"
        elif project_id in self.started:
            status = "ACTIVE"
        return {"project_id": project_id, "status": status}

    def complete(self, version, labels):
        project_id = self.prepared[version]["project_id"]
        assert project_id in self.started
        source = self.root / project_id
        source.mkdir(parents=True)
        manifest = {"schema": 1, "title": f"小红书小工具 {version}",
                    "steps": ["research", "generate", "validate", "package"],
                    "tool": {"kind": "checklist", "labels": labels}}
        (source / "workflow.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.accepted_projects.add(project_id)

    def accepted(self, product, version):
        if version["project_id"] not in self.accepted_projects:
            raise ProductError("Current DAG has not passed PM acceptance")
        source = self.root / version["project_id"]
        dag = json.dumps({"project_id": version["project_id"], "status": "COMPLETED", "issue_numbers": version["issue_numbers"]})
        return source, dag.encode()


def test_ds_bot_product_runs_maintains_releases_and_restores_actual_workflow(tmp_path):
    github = GitHubFixture()
    planning = PlanningFixture(tmp_path / "source")

    async def lifecycle():
        service = ManagedProducts(tmp_path / "state", github, planning)

        async def request(actor, operation, *, human=False, chief=False, source=None, **args):
            return await service.handle(actor, {"operation": operation, "repository_id": REPOSITORY["id"], **args},
                                        human=human, chief=chief, source=source)

        async def idea(text, source, candidate=None):
            return await request("usage-bot", "run", idea=text, candidate_id=candidate, subjective=True, source=source)

        async def accept(build, run):
            return await request("chief-bot", "accept_trial", build_id=build, run_id=run["id"], verdict="pass",
                                 human=True, source=f"panel:{run['id']}")

        def output(run):
            assert run["status"] == "completed"
            directory = service.product_dir(REPOSITORY["id"]) / "runs" / run["id"] / str(run["attempt"])
            entry = (directory / run["entry"]).read_text(encoding="utf-8")
            with ZipFile(directory / "tool.zip") as archive:
                assert archive.read(run["entry"]).decode() == entry
                assert {"index.html", "tool.js", "tool.css", "README.md"}.issubset(archive.namelist())
            with ZipFile(directory / NATIVE_ARCHIVE) as archive:
                assert set(archive.namelist()) == NATIVE_FILES
                assert json.loads(archive.read("app.json"))["pages"] == [NATIVE_ROUTE]
            return entry

        setup = {"confirmed": True, "owner": REPOSITORY["owner"], "name": REPOSITORY["name"], "pm": "pm-bot",
                 "usage": "usage-bot", "title": "小红书小工具", "rollback": True, "budget": 10}
        with pytest.raises(ProductForbidden):
            await request("chief-bot", "attach", chief=True, **setup)
        attached = await request("chief-bot", "attach", chief=True, human=True, **setup)
        assert attached["title"] == "小红书小工具"
        first, second, next_version = "v0.1.0", "v0.1.1", "v0.1.2"
        planned = await request("chief-bot", "plan", version=first, chief_session="chief-session")
        assert planned["versions"][0]["project_id"] == f"project-{first}-r1"
        with pytest.raises(ProductError, match="PM acceptance"):
            await request("pm-bot", "candidate", version=first)
        with pytest.raises(ProductForbidden):
            await request("chief-bot", "start", version=first)
        started = await request("chief-bot", "start", version=first, human=True)
        assert started["project_id"] == planned["versions"][0]["project_id"]
        planning.complete(first, ["核对标题", "核对资料来源"])
        stable = (await request("pm-bot", "candidate", version=first))["trials"]["build"]
        with pytest.raises(ProductError, match="no released"):
            await idea("首发前的默认入口", "no-release")
        first_ideas = ["周末咖啡路线选题", "旧衣改造发布清单", "城市散步路线校对"]
        for index, text in enumerate(first_ideas):
            source = f"initial-user-turn-{index}"
            run = await idea(text, source, stable["id"])
            assert run == await idea(text, source, stable["id"])
            entry = output(run)
            assert text in entry and "核对资料来源" in entry
            if index == 0:
                with pytest.raises(ProductForbidden):
                    await request("pm-bot", "accept_trial", build_id=stable["id"], run_id=run["id"], verdict="pass")
                with pytest.raises(ValueError, match="three verified trials"):
                    await request("pm-bot", "publish", build_id=stable["id"])
            accepted = await accept(stable["id"], run)
            assert accepted["trials"]["verified_count"] == index + 1
        with pytest.raises(ProductForbidden):
            await request("usage-bot", "publish", build_id=stable["id"])
        published = await request("pm-bot", "publish", build_id=stable["id"])
        assert published["delivery"]["current"][TARGET] == stable

        service = ManagedProducts(tmp_path / "state", github, planning)
        normal = await idea("春季相片笔记发布清单", "normal-a")
        assert normal["build"] == stable and "为图片补充说明" not in output(normal)
        for name in ("tool.zip", NATIVE_ARCHIVE):
            artifact = await request("usage-bot", "artifact", run_id=normal["id"], artifact=name)
            assert artifact["id"] == normal["id"] and artifact["path"] == name
            assert artifact["sha256"] == next(item["sha256"] for item in normal["artifacts"] if item["path"] == name)
        reported = await request("usage-bot", "report_issue", run_id=normal["id"], title="补充图片说明检查项",
                                 repro="生成春季相片笔记清单", actual="没有图片说明检查项",
                                 expected="加入图片说明检查项", source="report-turn-17")
        assert reported["number"] == 17
        assert first in github.issues[17]["body"] and normal["id"] in github.issues[17]["body"]
        await request("pm-bot", "triage", number=17, decision="accepted")
        await request("pm-bot", "plan", version=second, base=first, issue_numbers=[17])
        await request("chief-bot", "start", version=second, human=True)
        planning.complete(second, ["核对标题", "为图片补充说明"])
        candidate_a = await request("pm-bot", "candidate", version=second)
        build_a = candidate_a["trials"]["build"]
        project_a = next(version["project_id"] for version in candidate_a["versions"] if version["version"] == second)
        for index, text in enumerate(("现场照片说明清单", "换季着装图注核对")):
            run = await idea(text, f"a-user-turn-{index}", build_a["id"])
            assert "核对资料来源" not in output(run)
            accepted = await accept(build_a["id"], run)
            assert accepted["trials"]["verified_count"] == index + 1

        github.issue(18, "为标题增加长度提示")
        await service.poll()
        await service.poll()
        await request("pm-bot", "triage", number=18, decision="accepted")
        for bot in ("usage-bot", "pm-bot"):
            view = await request(bot, "status")
            assert sorted((issue["number"], issue["triage"]) for issue in view["issues"]) == [(17, "accepted"), (18, "accepted")]
            assert all("body" not in issue for issue in view["issues"])
            assert view["trials"]["verified_count"] == 2
            assert view["trials"]["build"]["issue_numbers"] == [17]

        with pytest.raises(ProductError, match="Pause trial"):
            await request("pm-bot", "revise", version=second)
        await request("pm-bot", "pause_candidate", build_id=build_a["id"])
        revised = await request("pm-bot", "revise", version=second)
        version_b = next(version for version in revised["versions"] if version["version"] == second)
        assert version_b["revision"] == 2 and version_b["project_id"] != project_a
        assert version_b["history"][-1]["project_id"] == project_a
        assert version_b["history"][-1]["build_id"] == build_a["id"]
        with pytest.raises(ProductError, match="PM acceptance"):
            await request("pm-bot", "candidate", version=second)
        await request("chief-bot", "start", version=second, human=True)
        planning.complete(second, ["核对标题", "核对资料来源", "为图片补充说明"])
        candidate_b = await request("pm-bot", "candidate", version=second)
        build_b = candidate_b["trials"]["build"]
        assert candidate_b["trials"]["verified_count"] == 0
        assert build_a["version"] == build_b["version"] == second
        assert build_a["id"] != build_b["id"] and build_b["issue_numbers"] == [17]
        assert build_a["dag_sha256"] != build_b["dag_sha256"]
        history = trial_progress(service.store.product(service.store.read(), REPOSITORY["id"]), build_a["id"])
        assert history["status"] == "paused" and history["verified_count"] == 2
        assert (await idea("默认仍是上一版", "normal-during-b"))["build"] == stable
        for index, text in enumerate(("街拍作品发布", "市集攻略图片说明", "徒步相册复核")):
            if index == 2:
                await request("pm-bot", "pause_candidate", build_id=build_b["id"])
                resumed = await request("pm-bot", "resume_candidate", build_id=build_b["id"])
                assert resumed["trials"]["verified_count"] == 2
            run = await idea(text, f"b-user-turn-{index}", build_b["id"])
            entry = output(run)
            assert text in entry and "为图片补充说明" in entry
            await accept(build_b["id"], run)
        published = await request("pm-bot", "publish", build_id=build_b["id"])
        assert published["delivery"]["current"][TARGET] == build_b
        assert set(github.releases) == {first, second}
        assert github.releases[second]["id"] == build_b["id"]
        assert github.issues[17]["state"] == "closed" and github.issues[18]["state"] == "open"
        current = await idea("默认入口改用维护版", "normal-b")
        assert current["build"] == build_b and "为图片补充说明" in output(current)

        restored = await request("pm-bot", "rollback", build_id=stable["id"])
        assert restored["delivery"]["current"][TARGET] == stable
        assert {operation["request"]["kind"] for operation in restored["delivery"]["completed"].values()} == {
            "release", "rollback",
        }
        assert set(github.releases) == {first, second}
        recovered = await idea("回退后使用原来的清单", "normal-restored")
        assert recovered["build"] == stable and "为图片补充说明" not in output(recovered)
        await request("pm-bot", "reopen", number=17)
        fixed_next = await request("pm-bot", "plan", version=next_version, base=first, issue_numbers=[17])
        assert github.issues[17]["state"] == "open"
        assert len({version["project_id"] for version in fixed_next["versions"]}) == 3
        assert planning.prepared[next_version]["base"] == first

    asyncio.run(lifecycle())
