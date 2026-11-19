from copy import deepcopy

import pytest

from runtime.workflow_package import freeze_workflow, json_bytes, load_workflow, sha256_hex
from runtime.workflow_rule_verify import WorkflowRuleError, verify_generated_tool
from runtime.workflow_runner import run_workflow


def generate(tmp_path, kind, **options):
    labels = ["核对原始引文 & 链接", "披露利益关系"]
    (tmp_path / "workflow.json").write_bytes(json_bytes({
        "schema": 1, "title": "小红书内容工具", "steps": ["research", "generate", "validate", "package"],
        "tool": {"kind": kind, "labels": labels, **options},
    }))
    frozen = freeze_workflow(tmp_path, commit="a" * 40, dag_snapshot=b'{}')
    loaded = load_workflow(frozen.payload, expected_digest=frozen.digest)
    result = run_workflow(loaded, idea="合作笔记发布前检查", output_dir=tmp_path / "tool")
    identity = {"expected_manifest_digest": result.manifest_digest, "expected_workflow_digest": frozen.digest}
    return tmp_path / "tool", identity, labels


@pytest.mark.parametrize("kind,options,cases", [
    ("checklist", {}, [
        {"inputs": [True, False], "output": "已完成 1 / 2"}, {"inputs": [True, True], "output": "已完成 2 / 2"},
    ]),
    ("calculator", {"operation": "product"}, [
        {"inputs": ["3", "4"], "output": "12"}, {"inputs": ["", "4"], "output": "填写所有数字后即可计算"},
    ]),
    ("template", {"template": "引文 {1} · 合作说明 {2}"}, [
        {"inputs": ["https://example.invalid/a", "合作赠品"], "output": "引文 https://example.invalid/a · 合作说明 合作赠品"},
    ]),
])
def test_verifier_executes_the_actual_built_in_script_and_records_observation(tmp_path, kind, options, cases):
    directory, identity, labels = generate(tmp_path, kind, **options)
    expectation = {"kind": kind, "labels": labels, "cases": cases}
    evidence = verify_generated_tool(directory, expectation, **identity)
    assert evidence["observed"] == expectation
    assert evidence["workflow_sha256"] == identity["expected_workflow_digest"]
    assert evidence["artifact_manifest_sha256"] == identity["expected_manifest_digest"]
    assert evidence["sha256"] == sha256_hex(json_bytes({key: value for key, value in evidence.items() if key != "sha256"}))


def test_wrong_labels_and_script_behavior_cannot_pass(tmp_path):
    directory, identity, labels = generate(tmp_path, "checklist")
    expectation = {"kind": "checklist", "labels": labels, "cases": [{"inputs": [False, True], "output": "已完成 1 / 2"}]}
    wrong = deepcopy(expectation)
    wrong["labels"].reverse()
    with pytest.raises(WorkflowRuleError, match="labels differ"):
        verify_generated_tool(directory, wrong, **identity)
    wrong = deepcopy(expectation)
    wrong["cases"][0]["output"] = "已完成 2 / 2"
    with pytest.raises(WorkflowRuleError, match="expected output for case 1"):
        verify_generated_tool(directory, wrong, **identity)
    with pytest.raises(WorkflowRuleError, match="between one and twelve"):
        verify_generated_tool(directory, {**expectation, "cases": []}, **identity)
    (directory / "tool.js").write_text("Never execute modified tool code", encoding="utf-8")
    with pytest.raises(WorkflowRuleError, match="tool.js"):
        verify_generated_tool(directory, expectation, **identity)


def test_rejects_values_browser_number_input_would_not_accept(tmp_path):
    directory, identity, labels = generate(tmp_path, "calculator")
    with pytest.raises(WorkflowRuleError, match="does not match its browser field"):
        verify_generated_tool(directory, {"kind": "calculator", "labels": labels, "cases": [
            {"inputs": ["0x10", "1"], "output": "17"},
        ]}, **identity)
