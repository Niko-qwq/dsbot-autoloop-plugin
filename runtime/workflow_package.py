"""Identity and immutable packaging for declarative Autoloop workflows."""

import io
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from pydantic import ConfigDict, Field, StringConstraints, ValidationError, model_validator

from core.schema import StrictModel


COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RESOURCE = re.compile(r"resources/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:txt|md|json)\Z")
STAGES = ("research", "generate", "validate", "package")
MANIFEST_LIMIT = 32 * 1024
DAG_LIMIT = 2 * 1024 * 1024
RESOURCE_LIMIT = 64 * 1024
PACKAGE_LIMIT = DAG_LIMIT + MANIFEST_LIMIT + 12 * RESOURCE_LIMIT + 16 * 1024
METADATA = "identity.json"
MANIFEST = "workflow.json"
DAG = "TASK_DAG.json"

ToolKind = Literal["checklist", "calculator", "template"]
Operation = Literal["sum", "average", "product"]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Labels = Annotated[tuple[Label, ...], Field(min_length=1, max_length=20)]


class WorkflowPackageError(ValueError):
    pass


class ToolDefinition(StrictModel):
    model_config = ConfigDict(frozen=True)

    kind: ToolKind
    labels: Labels
    operation: Operation | None = None
    template: Annotated[str, Field(min_length=1, max_length=3000)] | None = None

    @model_validator(mode="after")
    def validate_options(self):
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("Tool labels must be distinct")
        if self.operation is not None and self.kind != "calculator":
            raise ValueError("Only calculators declare an arithmetic operation")
        if self.template is not None and self.kind != "template":
            raise ValueError("Only template tools declare a text template")
        if self.template is not None:
            fields = {int(field) for field in re.findall(r"\{([0-9]{1,2})\}", self.template)}
            if not fields or not fields.issubset(range(1, len(self.labels) + 1)):
                raise ValueError("Use placeholders such as {1} that match the declared template labels")
        return self


class WorkflowManifest(StrictModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = Field(alias="schema")
    title: Label
    steps: tuple[Literal["research"], Literal["generate"], Literal["validate"], Literal["package"]]
    tool: ToolDefinition
    allowed_kinds: Annotated[tuple[ToolKind, ...], Field(max_length=3)] = ()
    resources: Annotated[tuple[str, ...], Field(max_length=12)] = ()

    @model_validator(mode="after")
    def validate_names(self):
        if self.allowed_kinds and (
            self.tool.kind not in self.allowed_kinds or len(set(self.allowed_kinds)) != len(self.allowed_kinds)
        ):
            raise ValueError("Allowed tool kinds must be distinct and include the default tool kind")
        if any(not RESOURCE.fullmatch(resource) for resource in self.resources):
            raise ValueError("Workflow resources must be named text files under resources/")
        if len({resource.lower() for resource in self.resources}) != len(self.resources):
            raise ValueError("Workflow resource paths must be distinct")
        return self


class PackageIdentity(StrictModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = Field(alias="schema")
    commit: Annotated[str, Field(pattern=COMMIT.pattern.removesuffix(r"\Z") + "$")]
    dag_digest: Annotated[str, Field(pattern=DIGEST.pattern.removesuffix(r"\Z") + "$")]


@dataclass(frozen=True)
class FrozenWorkflow:
    payload: bytes
    digest: str
    commit: str
    dag_digest: str


@dataclass(frozen=True)
class LoadedWorkflow:
    digest: str
    commit: str
    dag_digest: str
    manifest: WorkflowManifest
    resources: Mapping[str, bytes]
    manifest_bytes: bytes

    def source_files(self) -> dict[str, bytes]:
        return {MANIFEST: self.manifest_bytes, **self.resources}


def sha256_hex(value: bytes) -> str:
    return sha256(value).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def json_object(content: bytes, name: str) -> dict:
    def unique(items):
        data = {}
        for key, value in items:
            if key in data:
                raise WorkflowPackageError(f"{name} contains a duplicate JSON key: {key}")
            data[key] = value
        return data

    def invalid_constant(value):
        raise WorkflowPackageError(f"{name} contains an invalid JSON value: {value}")

    try:
        result = json.loads(content.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkflowPackageError(f"{name} must be a UTF-8 JSON object") from error
    if not isinstance(result, dict):
        raise WorkflowPackageError(f"{name} must be a JSON object")
    return result


def deterministic_zip(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for name, data in sorted(files.items()):
            member = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(member, data, compress_type=ZIP_STORED)
    return buffer.getvalue()


def read_regular(root: Path, name: str, limit: int) -> bytes:
    """Read a relative regular file bounded in size, refusing links below root."""
    parts = name.split("/")
    if any(not part or part in {".", ".."} or "\\" in part or "\0" in part for part in parts):
        raise WorkflowPackageError(f"Invalid file path: {name}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    folders = []
    try:
        folders.append(os.open(root, directory_flags))
        for part in parts[:-1]:
            folders.append(os.open(part, directory_flags, dir_fd=folders[-1]))
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(parts[-1], flags, dir_fd=folders[-1])
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise WorkflowPackageError(f"{name} must be a regular file of at most {limit} bytes")
            content = stream.read(limit + 1)
            if len(content) > limit:
                raise WorkflowPackageError(f"{name} exceeds {limit} bytes")
            return content
    except OSError as error:
        raise WorkflowPackageError(f"Could not read regular file {name}: {error.strerror}") from error
    finally:
        for fd in reversed(folders):
            os.close(fd)


def _manifest(raw: bytes) -> WorkflowManifest:
    json_object(raw, MANIFEST)
    try:
        return WorkflowManifest.model_validate_json(raw)
    except ValidationError as error:
        raise WorkflowPackageError(f"Invalid {MANIFEST}: {error}") from error


def _snapshot(raw: bytes) -> str:
    if len(raw) > DAG_LIMIT:
        raise WorkflowPackageError(f"DAG snapshot exceeds {DAG_LIMIT} bytes")
    json_object(raw, DAG)
    return sha256_hex(raw)


def _resource(raw: bytes, name: str) -> None:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowPackageError(f"Workflow resources must be UTF-8: {name}") from error
    if name.endswith(".json"):
        json_object(raw, name)


def _freeze(read: Callable[[str, int], bytes], commit: str, dag_snapshot: bytes) -> FrozenWorkflow:
    if not COMMIT.fullmatch(commit):
        raise WorkflowPackageError("A full Git commit SHA is required")
    dag_digest = _snapshot(dag_snapshot)
    raw = read(MANIFEST, MANIFEST_LIMIT)
    manifest = _manifest(raw)
    files = {MANIFEST: raw, DAG: dag_snapshot, METADATA: json_bytes({
        "schema": 1,
        "commit": commit,
        "dag_digest": dag_digest,
    })}
    for name in manifest.resources:
        resource = read(name, RESOURCE_LIMIT)
        _resource(resource, name)
        files[name] = resource
    payload = deterministic_zip(files)
    return FrozenWorkflow(payload=payload, digest=sha256_hex(payload), commit=commit, dag_digest=dag_digest)


def freeze_workflow(source_dir: Path, *, commit: str, dag_snapshot: bytes) -> FrozenWorkflow:
    return _freeze(lambda name, maximum: read_regular(source_dir, name, maximum), commit, dag_snapshot)


def freeze_workflow_files(sources: Mapping[str, bytes], *, commit: str, dag_snapshot: bytes) -> FrozenWorkflow:
    """Freeze already captured source bytes after the integration commit is known."""
    files = dict(sources)

    def read(name: str, limit: int) -> bytes:
        content = files.pop(name, None)
        if not isinstance(content, bytes) or len(content) > limit:
            raise WorkflowPackageError(f"Invalid or missing source file: {name}")
        return content

    result = _freeze(read, commit, dag_snapshot)
    if files:
        raise WorkflowPackageError("Workflow sources contain files outside workflow.json")
    return result


def freeze_commit_workflow(repository: Path, *, commit: str, dag_snapshot: bytes) -> FrozenWorkflow:
    """Capture the approved commit directly, independent of index and checkout contents."""
    if not COMMIT.fullmatch(commit):
        raise WorkflowPackageError("A full Git commit SHA is required")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "-C", str(repository), *args],
                check=True, capture_output=True, timeout=10, env=env,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkflowPackageError(f"Cannot read verified commit {commit} using git {args[0]}") from error

    if git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip() != commit:
        raise WorkflowPackageError("The selected object must be the exact integration commit")

    def read(name: str, limit: int) -> bytes:
        entry = git("ls-tree", "-z", commit, "--", name)
        match = re.fullmatch(rb"(?:100644|100755) blob ([0-9a-f]{40}|[0-9a-f]{64})\t" + re.escape(name.encode()) + b"\0", entry)
        if match is None:
            raise WorkflowPackageError(f"{name} must be a regular file in the integration commit")
        blob = match[1].decode()
        size = int(git("cat-file", "-s", blob))
        if size > limit:
            raise WorkflowPackageError(f"{name} exceeds {limit} bytes in the integration commit")
        result = git("cat-file", "blob", blob)
        if len(result) != size:
            raise WorkflowPackageError(f"{name} does not match its Git blob size")
        return result

    return _freeze(read, commit, dag_snapshot)


def load_workflow(
    payload: bytes, *, expected_digest: str, expected_commit: str | None = None, expected_dag_digest: str | None = None,
) -> LoadedWorkflow:
    if len(payload) > PACKAGE_LIMIT or not DIGEST.fullmatch(expected_digest) or sha256_hex(payload) != expected_digest:
        raise WorkflowPackageError("Frozen workflow package digest does not match")

    try:
        with ZipFile(io.BytesIO(payload), "r") as archive:
            infos = archive.infolist()
            entries = {info.filename: info for info in infos}
            if len(entries) != len(infos) or not {MANIFEST, METADATA, DAG}.issubset(entries):
                raise WorkflowPackageError("Frozen workflow package is missing entries or contains duplicate paths")

            def read(name: str, limit: int) -> bytes:
                member = entries.get(name)
                if member is None or member.compress_type != ZIP_STORED or member.file_size > limit or (
                    member.file_size != member.compress_size or not stat.S_ISREG(member.external_attr >> 16)
                ):
                    raise WorkflowPackageError(f"Invalid frozen workflow entry: {name}")
                with archive.open(member) as stream:
                    return stream.read(limit + 1)

            manifest_bytes = read(MANIFEST, MANIFEST_LIMIT)
            manifest = _manifest(manifest_bytes)
            if entries.keys() != {MANIFEST, METADATA, DAG, *manifest.resources}:
                raise WorkflowPackageError("Frozen workflow has entries outside its manifest")
            raw_identity = read(METADATA, 1024)
            json_object(raw_identity, METADATA)
            identity = PackageIdentity.model_validate_json(raw_identity)
            dag_digest = _snapshot(read(DAG, DAG_LIMIT))
            if dag_digest != identity.dag_digest or (
                expected_commit is not None and identity.commit != expected_commit
            ) or (expected_dag_digest is not None and dag_digest != expected_dag_digest):
                raise WorkflowPackageError("Frozen workflow commit or DAG digest does not match")
            resources = {name: read(name, RESOURCE_LIMIT) for name in manifest.resources}
            for name, raw in resources.items():
                _resource(raw, name)
    except (BadZipFile, NotImplementedError, RuntimeError, ValidationError) as error:
        raise WorkflowPackageError("Invalid frozen workflow package") from error
    return LoadedWorkflow(expected_digest, identity.commit, dag_digest, manifest, MappingProxyType(resources), manifest_bytes)
