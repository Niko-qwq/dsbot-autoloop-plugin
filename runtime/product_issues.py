from datetime import datetime, timedelta

from core.product_state import ProductError


def reconcile_issues(product, remote):
    """Project only metadata. Issue bodies never enter the task command bus."""
    latest = product.get("issue_cursor")
    for entry in remote:
        number = entry.get("number")
        if "pull_request" in entry or type(number) is not int or number < 1:
            continue
        updated_at = str(entry.get("updated_at") or "")
        key = str(number)
        prior = product["issues"].get(key, {})
        if prior.get("updated_at", "") > updated_at:
            continue
        product["issues"][key] = {
            **prior, "number": number, "title": str(entry.get("title") or "")[:200],
            "state": entry.get("state"), "updated_at": updated_at,
            "triage": prior.get("triage", "new"),
        }
        if not prior and entry.get("state") == "open":
            product["issue_notices"][key] = {"issue_number": number}
        if updated_at and (latest is None or updated_at > latest):
            latest = updated_at
    product["issue_cursor"] = latest


def polling_since(product):
    cursor = product.get("issue_cursor")
    if cursor is None:
        return None
    value = datetime.fromisoformat(cursor.replace("Z", "+00:00")) - timedelta(seconds=2)
    return value.isoformat().replace("+00:00", "Z")


def triage_issue(product, number, decision, duplicate=None):
    issue = product["issues"].get(str(number))
    if issue is None:
        raise ProductError("Issue has not been synchronized")
    if decision not in ("accepted", "deferred", "merged"):
        raise ProductError("Choose accept, defer or merge")
    if decision == "merged" and (duplicate == number or str(duplicate) not in product["issues"]):
        raise ProductError("Choose another issue in this product")
    if decision != "merged" and duplicate is not None:
        raise ProductError("A duplicate target applies only to merge")
    issue.update(triage=decision, duplicate=duplicate)
    product["issue_notices"].pop(str(number), None)
    return issue


def issue_body(run, repro, actual, expected, marker):
    values = (repro, actual, expected)
    if any(not isinstance(text, str) or not text.strip() or len(text) > 4000 for text in values):
        raise ProductError("Provide bounded reproduction, actual and expected results")
    return (f"Workflow version: {run['build']['version']}\nRun: {run['id']}\n\n"
            f"## Reproduction\n{repro}\n\n## Actual\n{actual}\n\n## Expected\n{expected}\n\n"
            f"<!-- autoloop-operation:{marker} -->")
