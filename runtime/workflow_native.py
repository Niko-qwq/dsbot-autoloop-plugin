"""Local source packages for the Xiaohongshu miniapp runtime."""

import io
import json
from zipfile import BadZipFile, ZipFile

from runtime.workflow_package import ToolDefinition, WorkflowPackageError, deterministic_zip, json_bytes, json_object
from runtime.workflow_tool import RESULT_FUNCTION, tool_options


NATIVE_ARCHIVE = "xiaohongshu-miniapp.zip"
NATIVE_ROUTE = "pages/index/index"
APP_SOURCE = b"App({});\n"
NATIVE_FILES = {"app.js", "app.json", "app.css", *(f"{NATIVE_ROUTE}.{ext}" for ext in ("js", "json", "xhsml"))}

MARKUP = """<view class="shell">
  <text class="workflow">{{workflowTitle}}</text>
  <text class="heading">{{title}}</text>
  <text class="description" xhs:if="{{description}}">{{description}}</text>
  <view class="card">
    <view class="row" xhs:for="{{fields}}" xhs:key="id">
      <button class="choice" xhs:if="{{isChecklist}}" data-index="{{index}}" bindtap="toggleField">{{item.choice}}</button>
      <view xhs:if="{{hasInputs}}">
        <text class="label">{{item.label}}</text>
        <input class="field" type="text" maxlength="500" value="{{item.value}}" data-index="{{index}}" bindinput="editField" />
      </view>
    </view>
    <text class="result">{{result}}</text>
    <button class="reset" bindtap="reset">重置</button>
  </view>
</view>
""".encode()

STYLE = b"""page { background: #fcf9f7; color: #241b24; }
.shell { padding: 48rpx 32rpx; }
.workflow, .heading, .description, .label, .result { display: block; overflow-wrap: break-word; }
.workflow { color: #8d2748; font-size: 26rpx; }
.heading { margin-top: 12rpx; font-size: 46rpx; font-weight: 700; }
.description { margin-top: 18rpx; color: #685c65; white-space: pre-wrap; }
.card { margin-top: 32rpx; padding: 28rpx; border: 1px solid #e7dce2; border-radius: 24rpx; background: #fff; }
.row + .row { margin-top: 20rpx; }
.field, .choice { min-height: 68rpx; padding: 12rpx 18rpx; border: 1px solid #a89aa3; border-radius: 12rpx; }
.field { margin-top: 8rpx; }
.choice, .reset { color: #74213c; background: #fff; font-size: 28rpx; text-align: left; }
.result { margin-top: 24rpx; padding: 22rpx; border-radius: 14rpx; background: #f8eaf0; white-space: pre-wrap; }
.reset { margin-top: 18rpx; }
"""

PAGE = """
function makeField(label, index, value) {
  return { id: String(index + 1), label, value, choice: `${value ? '已完成' : '待完成'} · ${label}` };
}

function emptyFields() {
  return config.labels.map((label, index) => makeField(label, index, config.isChecklist ? false : ''));
}

function resultFor(fields) {
  return toolResult(config.kind, fields.map(field => field.value), config.operation, config.template);
}

function changeField(page, event, update) {
  const index = Number(event.currentTarget.dataset.index);
  if (!Number.isInteger(index) || index < 0 || index >= config.labels.length) return;
  const fields = page.data.fields.slice();
  fields[index] = makeField(config.labels[index], index, update(fields[index].value));
  page.setData({ fields, result: resultFor(fields) });
}

const initial = emptyFields();
Page({
  data: {
    title: config.title,
    workflowTitle: config.workflowTitle,
    description: config.description,
    isChecklist: config.isChecklist,
    hasInputs: !config.isChecklist,
    fields: initial,
    result: resultFor(initial),
  },

  toggleField(event) {
    if (config.isChecklist) changeField(this, event, value => !value);
  },

  editField(event) {
    if (!config.isChecklist && typeof event.detail.value === 'string') {
      changeField(this, event, () => event.detail.value.slice(0, 500));
    }
  },

  reset() {
    const fields = emptyFields();
    this.setData({ fields, result: resultFor(fields) });
  },
});
"""


def render_native_archive(title: str, description: str, workflow_title: str, tool: ToolDefinition) -> bytes:
    operation, template = tool_options(tool)
    config = json.dumps({
        "title": title,
        "description": description if description != title else "",
        "workflowTitle": workflow_title,
        "kind": tool.kind,
        "isChecklist": tool.kind == "checklist",
        "labels": tool.labels,
        "operation": operation,
        "template": template,
    }, ensure_ascii=True, sort_keys=True)
    script = ("'use strict';\nconst config = " + config + ";\n" + RESULT_FUNCTION + PAGE).encode()
    return deterministic_zip({
        "app.js": APP_SOURCE,
        "app.json": json_bytes({"pages": [NATIVE_ROUTE], "window": {"navigationBarTitleText": "小工具"}}),
        "app.css": STYLE,
        f"{NATIVE_ROUTE}.js": script,
        f"{NATIVE_ROUTE}.json": json_bytes({}),
        f"{NATIVE_ROUTE}.xhsml": MARKUP,
    })


def validate_native_archive(payload: bytes) -> None:
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if (len(entries) != len(NATIVE_FILES) or {entry.filename for entry in entries} != NATIVE_FILES
                    or any(entry.file_size < 1 or entry.file_size > 64 * 1024 for entry in entries)):
                raise WorkflowPackageError("Native tool archive has unexpected or oversized source files")
            files = {entry.filename: archive.read(entry) for entry in entries}
    except (BadZipFile, RuntimeError, NotImplementedError) as error:
        raise WorkflowPackageError("Native tool archive is not a readable ZIP") from error
    if deterministic_zip(files) != payload:
        raise WorkflowPackageError("Native tool archive does not match the canonical source format")
    app = json_object(files["app.json"], "app.json")
    json_object(files[f"{NATIVE_ROUTE}.json"], f"{NATIVE_ROUTE}.json")
    if app.get("pages") != [NATIVE_ROUTE] or files["app.js"] != APP_SOURCE:
        raise WorkflowPackageError("Native tool archive does not declare its app and page entry")
