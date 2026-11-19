"""A resumable release and rollback journal for one product.

The host authorizes requests and persists each transition under its writer lock.
Adapters reconcile repeated calls with the same operation ID after a lost reply.
"""

from copy import deepcopy
from typing import Protocol

from runtime.product_trials import (
    finish_candidate_release,
    frozen_build,
    lock_candidate_for_release,
    require_product_build,
    required_text,
)


class GitHubReleasePort(Protocol):
    async def ensure_release(self, repository_id: int, build: dict, operation_id: str) -> dict: ...

    async def close_issue(self, repository_id: int, number: int, operation_id: str) -> None: ...


class DefaultEntryPort(Protocol):
    async def read_identity(self, target: str) -> dict | None: ...

    async def compare_and_switch(self, target: str, expected: dict | None, desired: dict,
                                 operation_id: str) -> None: ...

    async def verify(self, target: str, desired: dict) -> str: ...


def _empty() -> dict:
    return {"current": {}, "pending": None, "completed": {}, "releases": {}, "interrupted": {}}


def delivery_projection(product: dict) -> dict:
    return deepcopy(product.get("delivery", _empty()))


def current_deployed_identity(product: dict, target: str) -> dict | None:
    """Return the last independently verified identity for this entry target."""
    return deepcopy(product.get("delivery", {}).get("current", {}).get(target))


def _prior_operation(state: dict, request: dict, *, allow_pending: bool = False) -> dict | None:
    operation_id = request["id"]
    prior = state["completed"].get(operation_id) or state.get("interrupted", {}).get(operation_id)
    pending = state["pending"]
    if prior is None and pending is not None:
        if pending["request"]["id"] == operation_id:
            prior = pending
        elif not allow_pending:
            raise ValueError("Finish the pending delivery operation first")
    if prior is not None and prior["request"] != request:
        raise ValueError("This operation ID is already bound to a different request")
    return prior


def _record(product: dict, state: dict, request: dict, expected: dict | None,
            desired: dict, stage: str) -> dict:
    operation = {"request": request, "expected": expected, "build": desired, "stage": stage,
                 "github_release": None, "entry_evidence": None, "closed_issues": []}
    state["pending"] = operation
    product["delivery"] = state
    return deepcopy(operation)


def begin_release(product: dict, candidate: dict, *, operation_id: str, target: str) -> dict:
    """Journal the already authorized release of the active, fully trialed build."""
    build = require_product_build(product, candidate)
    target = required_text(target, "entry target")
    request = {"id": required_text(operation_id, "operation ID"), "kind": "release", "target": target,
               "build": build}
    state = product.get("delivery", _empty())
    prior = _prior_operation(state, request)
    if prior is not None:
        return deepcopy(prior)
    published = (*state["releases"].values(), *state.get("interrupted", {}).values())
    if any(release["build"]["version"] == build["version"] for release in published):
        raise ValueError("This version already has an immutable GitHub Release")
    lock_candidate_for_release(product, build)
    return _record(product, state, request, current_deployed_identity(product, target), build, "github_release")


def begin_rollback(product: dict, *, operation_id: str, target: str, expected_build_id: str,
                   restore_build_id: str, compatibility_ref: str) -> dict:
    """Journal the already authorized rollback to a compatible prior verified build."""
    target = required_text(target, "entry target")
    request = {"id": required_text(operation_id, "operation ID"), "kind": "rollback", "target": target,
               "expected_build_id": required_text(expected_build_id, "expected build ID"),
               "restore_build_id": required_text(restore_build_id, "restore build ID"),
               "compatibility_ref": required_text(compatibility_ref, "data compatibility evidence")}
    state = product.get("delivery", _empty())
    prior = _prior_operation(state, request, allow_pending=True)
    if prior is not None:
        return deepcopy(prior)
    current = current_deployed_identity(product, target)
    pending = state["pending"]
    if pending is not None:
        if (pending["request"]["kind"] != "release" or pending["stage"] != "verify_entry"
                or pending["request"]["target"] != target or pending["build"]["id"] != expected_build_id
                or current is None or current["id"] != restore_build_id or pending["expected"] != current):
            raise ValueError("Pending delivery cannot be recovered to this build")
        finish_candidate_release(product, expected_build_id, interrupted=True)
        pending["stage"] = "interrupted"
        pending["recovery_operation_id"] = operation_id
        state.setdefault("interrupted", {})[pending["request"]["id"]] = pending
        return _record(product, state, request, pending["build"], current, "switch_entry")
    if current is None or current["id"] != expected_build_id:
        raise ValueError("The recorded deployment has changed")
    source = state["releases"].get(expected_build_id)
    destination = state["releases"].get(restore_build_id)
    if (source is None or destination is None or source["previous_build_id"] != restore_build_id
            or destination["target"] != target or expected_build_id == restore_build_id):
        raise ValueError("Rollback requires the immediately previous verified release for this target")
    desired = require_product_build(product, destination["build"])
    return _record(product, state, request, current, desired, "switch_entry")


def _require_same_release(build: dict, remote: dict) -> str:
    keys = ("version", "commit_sha", "artifact_sha256", "dag_sha256")
    if not isinstance(remote, dict) or any(remote.get(key) != build[key] for key in keys):
        raise ValueError("GitHub returned a release for a different frozen build")
    return required_text(remote.get("url"), "GitHub release URL")


async def _read_entry(entry: DefaultEntryPort, target: str) -> dict | None:
    actual = await entry.read_identity(target)
    return None if actual is None else frozen_build(actual)


async def _require_entry(entry: DefaultEntryPort, target: str, build: dict) -> None:
    if await _read_entry(entry, target) != build:
        raise ValueError("Default entry does not serve the expected frozen build")


def _complete(product: dict, state: dict, operation: dict) -> None:
    request, build = operation["request"], operation["build"]
    kind, target, operation_id = request["kind"], request["target"], request["id"]
    if kind == "release":
        finish_candidate_release(product, build["id"])
        expected = operation["expected"]
        state["releases"][build["id"]] = {"target": target, "build": build, "operation_id": operation_id,
                                         "previous_build_id": expected["id"] if expected else None,
                                         "github_release": operation["github_release"],
                                         "entry_evidence": operation["entry_evidence"]}
    operation["stage"] = "complete"
    operation["fact_id"] = f"product-{kind}:{build['repository']}:{operation_id}"
    state["completed"][operation_id] = operation
    state["pending"] = None


async def advance_operation(product: dict, *, github: GitHubReleasePort,
                            entry: DefaultEntryPort) -> dict | None:
    """Advance one journal stage; the caller commits the new state before advancing again."""
    state = product.get("delivery")
    if state is None or state["pending"] is None:
        return None
    operation = state["pending"]
    request, build, stage = operation["request"], operation["build"], operation["stage"]
    target, operation_id, repository = request["target"], request["id"], build["repository"]

    if stage == "github_release":
        remote = await github.ensure_release(repository, deepcopy(build), operation_id)
        operation["github_release"] = _require_same_release(build, remote)
        operation["stage"] = "switch_entry"
    elif stage == "switch_entry":
        actual = await _read_entry(entry, target)
        if actual != build:
            expected = operation["expected"]
            if actual != expected:
                raise ValueError("Default entry identity differs from the recorded deployment")
            await entry.compare_and_switch(target, deepcopy(expected), deepcopy(build), operation_id)
        operation["stage"] = "verify_entry"
    elif stage == "verify_entry":
        await _require_entry(entry, target, build)
        evidence = required_text(await entry.verify(target, deepcopy(build)), "entry verification evidence")
        await _require_entry(entry, target, build)
        operation["entry_evidence"] = evidence
        state["current"][target] = deepcopy(build)
        if request["kind"] == "release" and build["issue_numbers"]:
            operation["stage"] = "close_issues"
        else:
            _complete(product, state, operation)
    elif stage == "close_issues":
        await _require_entry(entry, target, build)
        closed, numbers = operation["closed_issues"], build["issue_numbers"]
        number = numbers[len(closed)]
        await github.close_issue(repository, number, operation_id)
        closed.append(number)
        if len(closed) == len(numbers):
            _complete(product, state, operation)
    else:
        raise ValueError(f"Unknown delivery journal stage: {stage}")
    return deepcopy(operation)
