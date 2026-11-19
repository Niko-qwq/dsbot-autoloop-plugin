import json
import shutil
import subprocess
from zipfile import ZipFile

import pytest

from runtime.workflow_native import NATIVE_ARCHIVE, NATIVE_FILES, NATIVE_ROUTE, validate_native_archive
from runtime.workflow_package import WorkflowPackageError, deterministic_zip, freeze_workflow, json_bytes, load_workflow, sha256_hex
from runtime.workflow_runner import WorkflowRunError, run_workflow, verify_run_artifacts


def freeze(tmp_path, name, kind, labels, **tool_options):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "workflow.json").write_text(json.dumps({
        "schema": 1,
        "title": name,
        "steps": ["research", "generate", "validate", "package"],
        "tool": {"kind": kind, "labels": labels, **tool_options},
    }), encoding="utf-8")
    frozen = freeze_workflow(directory, commit="a" * 40, dag_snapshot=b'{"tasks":[]}')
    shutil.rmtree(directory)
    return load_workflow(frozen.payload, expected_digest=frozen.digest)


def execute_browser_script(script, dataset, fields, *, reset=False):
    """Run the generated JS against the same form and output surface used by the entry."""
    harness = """
const [script, dataset, fields, reset] = process.argv.slice(1);
const values = JSON.parse(fields);
const handlers = {};
const form = {
  dataset: JSON.parse(dataset),
  querySelectorAll: () => values,
  addEventListener: (name, callback) => { handlers[name] = callback; },
};
const output = { textContent: '' };
globalThis.document = { querySelector: tag => tag === 'form' ? form : output };
await import('data:text/javascript;base64,' + Buffer.from(script).toString('base64'));
if (form.dataset.kind === 'checklist') values[0].checked = true;
handlers.input();
if (reset === 'true') {
  handlers.reset();
  for (const value of values) value.checked = false;
  await new Promise(resolve => setTimeout(resolve, 0));
}
process.stdout.write(output.textContent);
"""
    return subprocess.run([
        "node", "--input-type=module", "-e", harness, script, json.dumps(dataset), json.dumps(fields), json.dumps(reset),
    ], check=True, capture_output=True, text=True).stdout


def execute_native_page(package, actions):
    harness = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const [appScript, pageScript, markup, actions] = process.argv.slice(1);
let app, page;
vm.runInNewContext(appScript, { App(definition) { app = definition; } });
assert.ok(app);
vm.runInNewContext(pageScript, { Page(definition) {
  page = definition;
  page.setData = updates => Object.assign(page.data, updates);
} });
for (const [, handler] of markup.matchAll(/bind(?:tap|input)="(\\w+)"/g)) {
  assert.equal(typeof page[handler], 'function', handler);
}
const results = [page.data.result];
for (const [action, index, value] of JSON.parse(actions)) {
  page[action]({ currentTarget: { dataset: { index } }, detail: { value } });
  results.push(page.data.result);
}
process.stdout.write(JSON.stringify({ title: page.data.title, results, fields: page.data.fields }));
"""
    with ZipFile(package) as archive:
        assert set(archive.namelist()) == NATIVE_FILES
        assert json.loads(archive.read("app.json"))["pages"] == [NATIVE_ROUTE]
        assert json.loads(archive.read(NATIVE_ROUTE + ".json")) == {}
        action_args = [archive.read(path).decode() for path in ("app.js", NATIVE_ROUTE + ".js", NATIVE_ROUTE + ".xhsml")]
    result = subprocess.run([
        "node", "--input-type=commonjs", "-e", harness, *action_args, json.dumps(actions),
    ], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_run_uses_pinned_workflow_and_returns_openable_bundle_after_sources_are_deleted(tmp_path):
    a = freeze(tmp_path, "构建 A", "checklist", ["标题有具体对象", "核对资料来源"])
    b = freeze(tmp_path, "构建 B", "checklist", ["核对资料来源", "图片有说明", "提交前检查"])
    idea = "周末咖啡路线选题检查"
    run_a = run_workflow(a, idea=idea, output_dir=tmp_path / "run-a")
    run_b = run_workflow(b, idea=idea, output_dir=tmp_path / "run-b")
    assert run_a.workflow_digest == a.digest
    assert run_b.workflow_digest == b.digest
    assert run_a.manifest_digest != run_b.manifest_digest
    entry_a = (tmp_path / "run-a" / run_a.entry).read_text(encoding="utf-8")
    entry_b = (tmp_path / "run-b" / run_b.entry).read_text(encoding="utf-8")
    assert "构建 A" in entry_a and "标题有具体对象" in entry_a
    assert "构建 B" in entry_b and "图片有说明" in entry_b
    assert idea in entry_a and idea in entry_b
    for result, run in ((run_a, "run-a"), (run_b, "run-b")):
        assert verify_run_artifacts(
            tmp_path / run, expected_manifest_digest=result.manifest_digest, expected_workflow_digest=result.workflow_digest,
        ) == result
    with ZipFile(tmp_path / "run-b" / "tool.zip") as archive:
        assert archive.read("index.html").decode() == entry_b
        assert set(archive.namelist()) == {"index.html", "tool.js", "tool.css", "materials.json", "README.md"}
    script = (tmp_path / "run-b" / "tool.js").read_text(encoding="utf-8")
    assert execute_browser_script(script, {"kind": "checklist"}, [{"checked": False}] * 3) == "已完成 1 / 3"
    assert execute_browser_script(script, {"kind": "checklist"}, [{"checked": False}] * 3, reset=True) == "已完成 0 / 3"
    path = tmp_path / "run-b" / NATIVE_ARCHIVE
    native = execute_native_page(path, [["toggleField", 0], ["toggleField", 2], ["toggleField", 99], ["reset"]])
    assert native["title"] == idea
    assert native["results"] == ["已完成 0 / 3", "已完成 1 / 3", "已完成 2 / 3", "已完成 2 / 3", "已完成 0 / 3"]
    assert [field["label"] for field in native["fields"]] == ["核对资料来源", "图片有说明", "提交前检查"]
    artifact = next(item for item in run_b.artifacts if item.path == NATIVE_ARCHIVE)
    assert artifact.sha256 == sha256_hex(path.read_bytes())


@pytest.mark.parametrize("kind, options, expected", [
    ("calculator", {"operation": "sum"}, "7"),
    ("calculator", {"operation": "average"}, "3.5"),
    ("calculator", {"operation": "product"}, "12"),
    ("template", {"template": "{1} · {2}"}, "3 · 4"),
])
def test_typed_idea_runs_selected_tool_behavior_without_connectors(tmp_path, kind, options, expected):
    workflow = freeze(tmp_path, "工具工作流", kind, ["第一项", "第二项"], **options)
    result = run_workflow(workflow, idea={
        "title": "创作表单", "description": "根据两个字段产生结果。", "kind": kind,
        "labels": ["字段 A", "字段 B"], **options,
    }, output_dir=tmp_path / "run")
    assert result.entry == "index.html"
    script = (tmp_path / "run" / "tool.js").read_text(encoding="utf-8")
    assert execute_browser_script(script, {"kind": kind, **options}, [{"value": "3"}, {"value": "4"}]) == expected
    native = execute_native_page(tmp_path / "run" / NATIVE_ARCHIVE, [
        ["editField", 0, "3"], ["editField", 1, "4"], ["reset"],
    ])
    assert native["results"][2] == expected
    assert native["results"][0] == native["results"][3]


def test_rejects_invalid_or_unapproved_brief_and_escapes_user_input(tmp_path):
    workflow = freeze(tmp_path, "检查流程", "checklist", ["标签"])
    with pytest.raises(WorkflowRunError, match="does not support"):
        run_workflow(workflow, idea={
            "title": "草案", "description": "需要计算", "kind": "calculator", "labels": ["字段"],
        }, output_dir=tmp_path / "unsupported")
    assert not (tmp_path / "unsupported").exists()
    payload = '<img src=x onerror="alert(1)">'
    run_workflow(workflow, idea=payload, output_dir=tmp_path / "safe")
    entry = (tmp_path / "safe" / "index.html").read_text(encoding="utf-8")
    assert payload not in entry
    assert "&lt;img src=x" in entry
    package = tmp_path / "safe" / NATIVE_ARCHIVE
    with ZipFile(package) as archive:
        assert payload not in archive.read(NATIVE_ROUTE + ".xhsml").decode()
    assert execute_native_page(package, [])["title"] == payload


def test_rejects_overwrite_and_content_or_symlink_replacement(tmp_path):
    workflow = freeze(tmp_path, "检查流程", "checklist", ["标签"])
    output = tmp_path / "run"
    result = run_workflow(workflow, idea="测试工具", output_dir=output)
    with pytest.raises(WorkflowRunError, match="new run directory"):
        run_workflow(workflow, idea="其他工具", output_dir=output)
    with pytest.raises(WorkflowRunError, match="different workflow"):
        verify_run_artifacts(output, expected_manifest_digest=result.manifest_digest, expected_workflow_digest="0" * 64)
    entry = output / result.entry
    entry.write_bytes(b"x" * len(entry.read_bytes()))
    with pytest.raises(WorkflowRunError, match="no longer matches"):
        verify_run_artifacts(output, expected_manifest_digest=result.manifest_digest, expected_workflow_digest=workflow.digest)
    entry.unlink()
    entry.symlink_to(output / "README.md")
    with pytest.raises(WorkflowRunError, match="Could not read regular file"):
        verify_run_artifacts(output, expected_manifest_digest=result.manifest_digest, expected_workflow_digest=workflow.digest)


def test_verifies_native_archive_and_preserves_readback_of_earlier_browser_runs(tmp_path):
    workflow = freeze(tmp_path, "标准清单", "checklist", ["标签"])
    output = tmp_path / "run"
    result = run_workflow(workflow, idea="包含原生源码", output_dir=output)
    native = output / NATIVE_ARCHIVE
    with ZipFile(native) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    with pytest.raises(WorkflowPackageError, match="does not declare"):
        validate_native_archive(deterministic_zip(files | {"app.json": json_bytes({"pages": ["bad/page"]})}))
    native.write_bytes(b"x" * native.stat().st_size)
    with pytest.raises(WorkflowRunError, match="no longer matches"):
        verify_run_artifacts(output, expected_manifest_digest=result.manifest_digest, expected_workflow_digest=workflow.digest)

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"] = [item for item in manifest["artifacts"] if item["path"] != NATIVE_ARCHIVE]
    legacy_manifest = json_bytes(manifest)
    manifest_path.write_bytes(legacy_manifest)
    native.unlink()
    legacy = verify_run_artifacts(output, expected_manifest_digest=sha256_hex(legacy_manifest),
                                  expected_workflow_digest=workflow.digest)
    assert len(legacy.artifacts) == 6
