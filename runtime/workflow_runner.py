"""Generate and verify run-local small tools from frozen workflow definitions."""

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, ValidationError

from core.schema import StrictModel
from runtime.workflow_native import NATIVE_ARCHIVE, render_native_archive, validate_native_archive
from runtime.workflow_package import (
    COMMIT, DIGEST, STAGES, Label, LoadedWorkflow, ToolDefinition, WorkflowPackageError,
    deterministic_zip, json_bytes, json_object, read_regular, sha256_hex,
)
from runtime.workflow_tool import SCRIPT, STYLE, render_tool


ENTRY = "index.html"
RUN_MANIFEST = "manifest.json"
ZIP = "tool.zip"
GENERATED = {ENTRY, "tool.css", "tool.js"}
KIT = GENERATED | {"README.md", "materials.json"}
BROWSER_ARTIFACTS = KIT | {ZIP}
RUN_ARTIFACTS = BROWSER_ARTIFACTS | {NATIVE_ARCHIVE}
ARTIFACT_LIMIT = 2 * 1024 * 1024
DigestText = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class WorkflowRunError(ValueError):
    pass


class ToolBrief(ToolDefinition):
    title: Label
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class RunArtifact(StrictModel):
    model_config = ConfigDict(frozen=True)

    path: Literal["index.html", "tool.js", "tool.css", "README.md", "materials.json", "tool.zip", "xiaohongshu-miniapp.zip"]
    sha256: DigestText
    bytes: Annotated[int, Field(ge=1, le=ARTIFACT_LIMIT)]


class RunManifest(StrictModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = Field(alias="schema")
    entry: Literal["index.html"]
    workflow_digest: DigestText
    commit: Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    dag_digest: DigestText
    idea_digest: DigestText
    artifacts: Annotated[tuple[RunArtifact, ...], Field(min_length=6, max_length=7)]


@dataclass(frozen=True)
class WorkflowRunResult:
    workflow_digest: str
    manifest_digest: str
    artifacts: tuple[RunArtifact, ...]
    entry: str


class _ToolMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}
        self.scripts = []
        self.styles = []
        self.outputs = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "input":
            self.inputs[attrs.get("id")] = attrs.get("type")
        elif tag == "output":
            self.outputs += 1
        elif tag == "script":
            self.scripts.append(attrs.get("src"))
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.styles.append(attrs.get("href"))


def _parse_brief(workflow: LoadedWorkflow, idea: str | ToolBrief | Mapping) -> ToolBrief:
    try:
        if isinstance(idea, str):
            idea = idea.strip()
            if not idea:
                raise WorkflowRunError("A tool idea is required")
            idea = {
                **workflow.manifest.tool.model_dump(mode="json"),
                "title": idea.splitlines()[0][:120],
                "description": idea,
            }
        brief = idea if isinstance(idea, ToolBrief) else ToolBrief.model_validate_json(json_bytes(idea))
        if "\0" in brief.title or "\0" in brief.description or any("\0" in label for label in brief.labels):
            raise WorkflowRunError("Title, description and tool labels may not contain NUL characters")
    except (ValidationError, TypeError, UnicodeError) as error:
        raise WorkflowRunError(f"Invalid tool idea: {error}") from error
    if brief.kind not in (workflow.manifest.allowed_kinds or (workflow.manifest.tool.kind,)):
        raise WorkflowRunError(f"This workflow does not support the {brief.kind} tool kind")
    return brief


def _research(workflow: LoadedWorkflow, brief: ToolBrief) -> bytes:
    return json_bytes({
        "schema": 1,
        "workflow": workflow.manifest.title,
        "brief": brief.model_dump(mode="json"),
        "references": [
            {"path": name, "sha256": sha256_hex(content), "text": content.decode("utf-8")}
            for name, content in sorted(workflow.resources.items())
        ],
    })


def _validate(files: Mapping[str, bytes], kind: str, labels: tuple[str, ...]) -> None:
    if files.keys() != GENERATED or files["tool.js"] != SCRIPT or files["tool.css"] != STYLE:
        raise WorkflowRunError("The generated tool is missing its offline browser runtime")
    markup = _ToolMarkup()
    markup.feed(files[ENTRY].decode("utf-8"))
    input_type = {"checklist": "checkbox", "calculator": "number", "template": "text"}[kind]
    if markup.inputs != {f"field-{index}": input_type for index in range(1, len(labels) + 1)} or (
        markup.outputs != 1 or markup.scripts != ["tool.js"] or markup.styles != ["tool.css"]
    ):
        raise WorkflowRunError("The tool entry does not match its requested interface")


def _readme(brief: ToolBrief, workflow: LoadedWorkflow) -> bytes:
    instruction = {
        "checklist": "逐项勾选检查项，下方会即时显示完成进度。",
        "calculator": "填写所有数字，下方会即时显示计算结果。",
        "template": "填写表单，复制下方实时生成的文本。",
    }[brief.kind]
    return (
        f"# {brief.title}\n\n"
        "将 `tool.zip` 解压到一个目录，用浏览器打开 `index.html`。工具在本地运行。"
        f"{instruction}\n\n"
        f"`{NATIVE_ARCHIVE}` 提供小红书小程序源码。解压后在开发者工具中导入含 `app.json` 的目录。\n\n"
        f"工作流制品 SHA-256 `{workflow.digest}`\n"
    ).encode()


def _write_files(output_dir: Path, files: dict[str, bytes]) -> None:
    try:
        parent = output_dir.parent.resolve(strict=True)
        directory = parent / output_dir.name
        if parent != output_dir.parent.absolute() or output_dir.name in {"", ".", ".."}:
            raise WorkflowRunError("The run directory must be under its real, trusted parent")
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise WorkflowRunError(f"Cannot create a new run directory: {error.strerror}") from error
    try:
        for name, content in files.items():
            with (directory / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise WorkflowRunError(f"Cannot save run artifacts: {error.strerror}") from error


def run_workflow(workflow: LoadedWorkflow, *, idea: str | ToolBrief | Mapping, output_dir: Path) -> WorkflowRunResult:
    if tuple(workflow.manifest.steps) != STAGES or not DIGEST.fullmatch(workflow.digest) or (
        not COMMIT.fullmatch(workflow.commit) or not DIGEST.fullmatch(workflow.dag_digest)
    ):
        raise WorkflowRunError("Run a verified frozen workflow package")
    brief = _parse_brief(workflow, idea)
    materials = _research(workflow, brief)
    files = render_tool(brief.title, brief.description, workflow.manifest.title, brief)
    _validate(files, brief.kind, brief.labels)
    files.update({"README.md": _readme(brief, workflow), "materials.json": materials})
    files[ZIP] = deterministic_zip(files)
    native = render_native_archive(brief.title, brief.description, workflow.manifest.title, brief)
    validate_native_archive(native)
    files[NATIVE_ARCHIVE] = native
    artifacts = tuple(
        RunArtifact(path=name, sha256=sha256_hex(content), bytes=len(content))
        for name, content in sorted(files.items())
    )
    manifest = RunManifest.model_validate_json(json_bytes({
        "schema": 1,
        "entry": ENTRY,
        "workflow_digest": workflow.digest,
        "commit": workflow.commit,
        "dag_digest": workflow.dag_digest,
        "idea_digest": sha256_hex(json_bytes(brief.model_dump(mode="json"))),
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
    }))
    manifest_bytes = json_bytes(manifest.model_dump(mode="json", by_alias=True))
    files[RUN_MANIFEST] = manifest_bytes
    _write_files(output_dir, files)
    return WorkflowRunResult(workflow.digest, sha256_hex(manifest_bytes), artifacts, ENTRY)


def verify_run_artifacts(
    output_dir: Path, *, expected_manifest_digest: str, expected_workflow_digest: str,
) -> WorkflowRunResult:
    """Check independent readback of the run receipt and every delivered artifact."""
    try:
        raw = read_regular(output_dir, RUN_MANIFEST, 16 * 1024)
        if sha256_hex(raw) != expected_manifest_digest:
            raise WorkflowRunError("Run artifact manifest digest does not match")
        json_object(raw, RUN_MANIFEST)
        manifest = RunManifest.model_validate_json(raw)
        paths = {artifact.path for artifact in manifest.artifacts}
        if (manifest.workflow_digest != expected_workflow_digest or len(paths) != len(manifest.artifacts)
                or paths not in (BROWSER_ARTIFACTS, RUN_ARTIFACTS)):
            raise WorkflowRunError("Run artifact manifest refers to a different workflow or invalid artifacts")
        files = {}
        for artifact in manifest.artifacts:
            content = read_regular(output_dir, artifact.path, artifact.bytes)
            if len(content) != artifact.bytes or sha256_hex(content) != artifact.sha256:
                raise WorkflowRunError(f"Run artifact no longer matches: {artifact.path}")
            files[artifact.path] = content
        bundle = files.pop(ZIP)
        native = files.pop(NATIVE_ARCHIVE, None)
        if deterministic_zip(files) != bundle:
            raise WorkflowRunError("Run ZIP does not match the delivered offline tool")
        if native is not None:
            validate_native_archive(native)
    except (ValidationError, WorkflowPackageError) as error:
        raise WorkflowRunError(str(error)) from error
    return WorkflowRunResult(expected_workflow_digest, expected_manifest_digest, manifest.artifacts, manifest.entry)
