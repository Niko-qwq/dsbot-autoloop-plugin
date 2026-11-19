"""Built-in tool behavior and the offline HTML renderer."""

from html import escape

from runtime.workflow_package import ToolDefinition


RESULT_FUNCTION = r"""function toolResult(kind, fields, operation, template) {
  if (kind === 'checklist') {
    const completed = fields.filter(Boolean).length;
    return `已完成 ${completed} / ${fields.length}`;
  }

  const values = fields.map(value => value.trim());
  if (kind === 'template') {
    return template.replace(/\{([0-9]{1,2})\}/g,
      (_, number) => values[Number(number) - 1] || '［待填写］');
  }

  const numbers = values.map(Number);
  if (values.some(value => value === '') || numbers.some(number => !Number.isFinite(number))) {
    return '填写所有数字后即可计算';
  }
  const sum = numbers.reduce((total, number) => total + number, 0);
  const operations = {
    sum,
    average: sum / numbers.length,
    product: numbers.reduce((total, number) => total * number, 1),
  };
  const answer = operations[operation];
  if (!Number.isFinite(answer)) return '结果超出可计算范围';
  if (typeof Intl !== 'undefined' && typeof Intl.NumberFormat === 'function') {
    return new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 12 }).format(answer);
  }
  return String(Number(answer.toPrecision(12)));
}
"""

SCRIPT = ("'use strict';\n" + RESULT_FUNCTION + """
const form = document.querySelector('form');
const result = document.querySelector('output');
const fields = Array.from(form.querySelectorAll('input'));

function update() {
  const { kind, operation, template } = form.dataset;
  const values = fields.map(field => kind === 'checklist' ? field.checked : field.value);
  result.textContent = toolResult(kind, values, operation, template);
}

form.addEventListener('input', update);
form.addEventListener('submit', event => event.preventDefault());
form.addEventListener('reset', () => setTimeout(update, 0));
update();
""").encode()

STYLE = b"""* { box-sizing: border-box; }
:root { color: #241b24; background: #fcf9f7; font: 16px/1.55 system-ui, sans-serif; }
body { margin: 0; }
main { width: min(100% - 2.5rem, 38rem); margin: 5rem auto; }
.workflow { margin: 0 0 .65rem; color: #8d2748; font-size: .875rem; font-weight: 650; }
h1 { margin: 0; font-size: clamp(1.75rem, 6vw, 3.25rem); line-height: 1.15; overflow-wrap: anywhere; }
.description { margin: 1rem 0 2rem; color: #685c65; white-space: pre-wrap; overflow-wrap: anywhere; }
form { margin-top: 2rem; padding: 1.25rem; border: 1px solid #e7dce2; border-radius: 1rem; background: #fff; }
ul { list-style: none; padding: 0; margin: 0; }
li + li { margin-top: 1rem; }
label { display: flex; align-items: center; gap: .65rem; overflow-wrap: anywhere; cursor: pointer; }
input { font: inherit; }
input[type=checkbox] { accent-color: #a32d56; inline-size: 1.1rem; block-size: 1.1rem; }
input:not([type=checkbox]) { display: block; width: 100%; padding: .55rem; border: 1px solid #a89aa3; border-radius: .4rem; }
output { display: block; margin-top: 1.3rem; padding: .9rem; border-radius: .55rem; background: #f8eaf0; white-space: pre-wrap; overflow-wrap: anywhere; }
button { margin-top: 1rem; padding: .5rem .9rem; border: 1px solid #b68c9f; border-radius: .45rem; color: #74213c; background: #fff; font: inherit; cursor: pointer; }
:focus-visible { outline: 2px solid #a32d56; outline-offset: 3px; }
@media (max-width: 35rem) { main { margin: 2.75rem auto; } }
"""


def tool_options(tool: ToolDefinition) -> tuple[str, str]:
    template = tool.template or "\n".join(f"{label} · {{{index}}}" for index, label in enumerate(tool.labels, 1))
    return tool.operation or "sum", template


def render_tool(title: str, description: str, workflow_title: str, tool: ToolDefinition) -> dict[str, bytes]:
    input_type = {"checklist": "checkbox", "calculator": "number", "template": "text"}[tool.kind]
    rows = []
    controls = {"number": ' step="any"', "text": ' maxlength="500"', "checkbox": ""}[input_type]
    for index, label in enumerate(tool.labels, 1):
        field_id = f"field-{index}"
        field = f'<input type="{input_type}" id="{field_id}"{controls}>'
        if input_type == "checkbox":
            rows.append(f'<li><label for="{field_id}">{field}<span>{escape(label)}</span></label></li>')
        else:
            rows.append(f'<li><label for="{field_id}">{escape(label)}</label>{field}</li>')
    operation, template = tool_options(tool)
    description_html = f'<p class="description">{escape(description)}</p>' if description != title else ""
    inputs = "\n".join(rows)
    html = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' file:; style-src 'self' file:; base-uri 'none'; form-action 'none'">
<title>{escape(title)}</title>
<link rel="stylesheet" href="tool.css">
<script defer src="tool.js"></script>
<main>
  <p class="workflow">{escape(workflow_title)}</p>
  <h1>{escape(title)}</h1>
  {description_html}
  <form data-kind="{tool.kind}" data-operation="{operation}" data-template="{escape(template, quote=True)}" autocomplete="off">
    <ul>
      {inputs}
    </ul>
    <output aria-live="polite"></output>
    <button type="reset">重置</button>
  </form>
</main>
</html>
"""
    return {"index.html": html.encode(), "tool.css": STYLE, "tool.js": SCRIPT}
