"""Independent readback and behavior checks for generated browser tools."""

import json
import math
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from runtime.workflow_package import json_bytes, read_regular, sha256_hex
from runtime.workflow_runner import verify_run_artifacts
from runtime.workflow_tool import SCRIPT


KINDS = {"checklist": "checkbox", "calculator": "number", "template": "text"}
NUMBER = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
NODE = r"""
const { readFileSync } = require('node:fs');
const { Script, createContext } = require('node:vm');
const { script, dataset, cases } = JSON.parse(readFileSync(0, 'utf8'));
const program = new Script(`
  const box = { textContent: '' };
  const listeners = {};
  const controls = Array.from(inputs, () => ({ checked: false, value: '' }));
  const page = { dataset, querySelectorAll: () => controls,
    addEventListener: (name, handler) => { listeners[name] = handler; } };
  const document = { querySelector: name => name === 'form' ? page : box };
  ${script}
  for (const [index, value] of Array.from(inputs).entries()) {
    if (dataset.kind === 'checklist') controls[index].checked = value;
    else controls[index].value = value;
  }
  listeners.input();
  box.textContent;
`, { filename: 'generated-tool.js' });
const results = cases.map(inputs => program.runInContext(createContext({ dataset, inputs }, {
  codeGeneration: { strings: false, wasm: false },
}), { timeout: 250 }));
if (results.some(value => typeof value !== 'string' || value.length > 32000)) {
  throw new Error('Generated output is not bounded text');
}
process.stdout.write(JSON.stringify(results));
"""


class WorkflowRuleError(ValueError):
    pass


class _Form(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.inputs = []
        self.labels = []
        self.scripts = []
        self.in_form = False
        self.label = None
        self.outputs = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.forms.append({name.removeprefix("data-"): attrs.get(name) for name in
                               ("data-kind", "data-operation", "data-template")})
            self.in_form = True
        elif tag == "script":
            self.scripts.append(attrs.get("src"))
        elif self.in_form:
            if tag == "input":
                self.inputs.append((attrs.get("id"), attrs.get("type")))
            elif tag == "label":
                self.label = []
                self.labels.append((attrs.get("for"), self.label))
            elif tag == "output":
                self.outputs += 1

    def handle_data(self, data):
        if self.label is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "label":
            self.label = None
        elif tag == "form":
            self.in_form = False


def _expectation(value):
    if not isinstance(value, dict) or set(value) != {"kind", "labels", "cases"}:
        raise WorkflowRuleError("Specify the expected tool kind, labels, and input/output cases")
    kind, labels, cases = value["kind"], value["labels"], value["cases"]
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(labels, list) or not 1 <= len(labels) <= 20 or any(
        not isinstance(label, str) or not label.strip() or len(label) > 120 for label in labels
    ):
        raise WorkflowRuleError("Provide a supported tool kind and up to 20 expected labels")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise WorkflowRuleError("Provide between one and twelve objective tool cases")
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"inputs", "output"} or not isinstance(case["output"], str) or len(case["output"]) > 16000:
            raise WorkflowRuleError("Each tool case requires inputs and a bounded expected text result")
        inputs = case["inputs"]
        if not isinstance(inputs, list) or len(inputs) != len(labels):
            raise WorkflowRuleError("Each tool case must supply one input per label")
        for item in inputs:
            if kind == "checklist" and type(item) is bool:
                continue
            if kind != "checklist" and isinstance(item, str) and len(item) <= 500 and not any(c in item for c in "\0\r\n"):
                if kind == "template" or item == "" or NUMBER.fullmatch(item) and math.isfinite(float(item)):
                    continue
            raise WorkflowRuleError("Tool case input does not match its browser field")
    return kind, labels, cases


def _read_verified(output_dir: Path, artifacts, name: str) -> bytes:
    item = artifacts[name]
    try:
        content = read_regular(output_dir, name, item.bytes)
    except ValueError as error:
        raise WorkflowRuleError(str(error)) from error
    if sha256_hex(content) != item.sha256:
        raise WorkflowRuleError(f"Tool file changed after verification: {name}")
    return content


def verify_generated_tool(output_dir: Path, expectation, *, expected_manifest_digest: str, expected_workflow_digest: str) -> dict:
    """Exercise the pinned browser script against its actual form and declared cases."""
    kind, labels, cases = _expectation(expectation)
    try:
        verified = verify_run_artifacts(output_dir, expected_manifest_digest=expected_manifest_digest,
                                        expected_workflow_digest=expected_workflow_digest)
    except ValueError as error:
        raise WorkflowRuleError(str(error)) from error
    artifacts = {artifact.path: artifact for artifact in verified.artifacts}
    script = _read_verified(output_dir, artifacts, "tool.js")
    if script != SCRIPT:
        raise WorkflowRuleError("The generated browser script differs from the bundled implementation")
    entry = _read_verified(output_dir, artifacts, verified.entry)
    form = _Form()
    try:
        form.feed(entry.decode("utf-8"))
        form.close()
    except (UnicodeError, ValueError) as error:
        raise WorkflowRuleError("The generated entry is not valid UTF-8 HTML") from error
    if len(form.forms) != 1 or form.forms[0]["kind"] != kind or form.scripts != ["tool.js"] or form.outputs != 1:
        raise WorkflowRuleError("The generated form does not match the expected tool kind")
    expected_ids = [f"field-{index}" for index in range(1, len(labels) + 1)]
    if form.inputs != [(item, KINDS[kind]) for item in expected_ids] or [item for item, _ in form.labels] != expected_ids:
        raise WorkflowRuleError("The generated form does not contain the expected fields")
    actual_labels = ["".join(parts) for _, parts in form.labels]
    if actual_labels != labels:
        raise WorkflowRuleError("The generated tool labels differ from the expected labels")
    try:
        result = subprocess.run(["node", "--input-type=commonjs", "-e", NODE], input=json_bytes({
            "script": script.decode("utf-8"), "dataset": form.forms[0], "cases": [case["inputs"] for case in cases],
        }), check=True, capture_output=True, timeout=6, env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"})
        outcomes = json.loads(result.stdout)
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        raise WorkflowRuleError("The generated tool could not be verified with the local Node runtime") from error
    if not isinstance(outcomes, list) or len(outcomes) != len(cases) or not all(isinstance(item, str) for item in outcomes):
        raise WorkflowRuleError("The local Node runtime did not return one text result per tool case")
    for index, (case, output) in enumerate(zip(cases, outcomes, strict=True), 1):
        if output != case["output"]:
            raise WorkflowRuleError(f"Tool behavior did not match expected output for case {index}")
    observed = {"kind": kind, "labels": actual_labels, "cases": [
        {"inputs": case["inputs"].copy(), "output": output} for case, output in zip(cases, outcomes, strict=True)
    ]}
    evidence = {"observed": observed, "artifact_manifest_sha256": verified.manifest_digest,
                "workflow_sha256": verified.workflow_digest, "script_sha256": artifacts["tool.js"].sha256,
                "entry_sha256": artifacts[verified.entry].sha256}
    return {**evidence, "sha256": sha256_hex(json_bytes(evidence))}
