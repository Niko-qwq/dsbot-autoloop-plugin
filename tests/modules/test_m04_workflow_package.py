import io
import json
import subprocess
from zipfile import ZipFile

import pytest

from runtime.workflow_package import (
    WorkflowPackageError, deterministic_zip, freeze_commit_workflow, freeze_workflow, freeze_workflow_files,
    load_workflow, sha256_hex,
)


COMMIT = "a" * 40
DAG = b'{"project_id":"maker","tasks":[{"task_id":"F17"}]}\n'


def write_manifest(directory, **fields):
    manifest = {
        "schema": 1,
        "title": "小红书创作检查工具工厂",
        "steps": ["research", "generate", "validate", "package"],
        "tool": {"kind": "checklist", "labels": ["确认目标受众", "核对参考来源"]},
        **fields,
    }
    (directory / "workflow.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def test_identical_inputs_freeze_same_bytes_and_preserve_identity(tmp_path):
    write_manifest(tmp_path, resources=["resources/checks.md"])
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "checks.md").write_text("检查声明的依据。", encoding="utf-8")
    (tmp_path / "untracked-secret.txt").write_text("excluded", encoding="utf-8")
    first = freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=DAG)
    assert freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=DAG) == first
    loaded = load_workflow(
        first.payload, expected_digest=first.digest, expected_commit=COMMIT, expected_dag_digest=sha256_hex(DAG),
    )
    assert loaded.manifest.tool.labels == ("确认目标受众", "核对参考来源")
    assert set(loaded.resources) == {"resources/checks.md"}
    with ZipFile(io.BytesIO(first.payload)) as archive:
        assert set(archive.namelist()) == {"workflow.json", "identity.json", "TASK_DAG.json", "resources/checks.md"}
        assert archive.read("TASK_DAG.json") == DAG


def test_freeze_reads_actual_commit_even_when_checkout_is_replaced(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True).stdout.decode().strip()

    git("init", "-q")
    write_manifest(tmp_path, title="已验收的提交")
    git("add", "workflow.json")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "accepted")
    commit = git("rev-parse", "HEAD")
    write_manifest(tmp_path, title="工作区中的无关更改")
    frozen = freeze_commit_workflow(tmp_path, commit=commit, dag_snapshot=DAG)
    assert load_workflow(frozen.payload, expected_digest=frozen.digest).manifest.title == "已验收的提交"
    assert freeze_workflow(tmp_path, commit=commit, dag_snapshot=DAG).digest != frozen.digest
    with pytest.raises(WorkflowPackageError, match="full Git commit SHA"):
        freeze_commit_workflow(tmp_path, commit="HEAD", dag_snapshot=DAG)


def test_freeze_can_pin_a_verified_remote_integration_to_already_captured_source(tmp_path):
    write_manifest(tmp_path, title="提交前已验收")
    preview = freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=DAG)
    verified = load_workflow(preview.payload, expected_digest=preview.digest)
    write_manifest(tmp_path, title="新的未验收改动")
    actual_commit = "b" * 40
    frozen = freeze_workflow_files(verified.source_files(), commit=actual_commit, dag_snapshot=DAG)
    assert load_workflow(frozen.payload, expected_digest=frozen.digest, expected_commit=actual_commit).manifest.title == "提交前已验收"


@pytest.mark.parametrize("fields", [
    {"resources": ["resources/../secrets.txt"]},
    {"resources": ["resources/A.md", "resources/a.md"]},
    {"steps": ["research", "arbitrary-shell", "validate", "package"]},
    {"steps": ["generate", "research", "validate", "package"]},
    {"tool": {"kind": "checklist", "labels": ["一", "一"]}},
])
def test_rejects_unlisted_capabilities_or_unsafe_source_paths(tmp_path, fields):
    write_manifest(tmp_path, **fields)
    with pytest.raises(WorkflowPackageError, match="Invalid workflow.json"):
        freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=DAG)


def test_rejects_symlinks_in_source_and_invalid_snapshot(tmp_path):
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "notes.md").write_text("private", encoding="utf-8")
    (tmp_path / "resources").symlink_to(outside, target_is_directory=True)
    write_manifest(tmp_path, resources=["resources/notes.md"])
    with pytest.raises(WorkflowPackageError, match="Could not read regular file"):
        freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=DAG)
    with pytest.raises(WorkflowPackageError, match="must be a JSON object"):
        freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=b"[]")


def test_load_rejects_changed_bytes_wrong_provenance_and_unlisted_archive_paths(tmp_path):
    write_manifest(tmp_path)
    frozen = freeze_workflow(tmp_path, commit=COMMIT, dag_snapshot=DAG)
    for mismatch in ({"expected_commit": "b" * 40}, {"expected_dag_digest": "0" * 64}):
        with pytest.raises(WorkflowPackageError, match="commit or DAG digest"):
            load_workflow(frozen.payload, expected_digest=frozen.digest, **mismatch)
    with pytest.raises(WorkflowPackageError, match="package digest"):
        load_workflow(frozen.payload + b"tampered", expected_digest=frozen.digest)
    with ZipFile(io.BytesIO(frozen.payload)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    forged = deterministic_zip({**files, "../escape.txt": b"unexpected"})
    with pytest.raises(WorkflowPackageError, match="outside its manifest"):
        load_workflow(forged, expected_digest=sha256_hex(forged))
    forged = deterministic_zip({**files, "TASK_DAG.json": b'{"tasks":[]}'})
    with pytest.raises(WorkflowPackageError, match="commit or DAG digest"):
        load_workflow(forged, expected_digest=sha256_hex(forged))
