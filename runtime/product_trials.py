"""Frozen candidate and trial transitions for the product store's single writer."""

import re
from copy import deepcopy


REQUIRED_TRIALS = 3
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRIAL_MODES = ("read_only", "dry_run", "isolated")


def required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a nonempty, trimmed string")
    return value


def _digest(value: object, pattern: re.Pattern, field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Invalid {field}")
    return value


def frozen_build(candidate: dict) -> dict:
    """Return the immutable identity shared by trials, GitHub and the default entry."""
    repository = candidate.get("repository")
    if type(repository) is not int or repository < 1:
        raise ValueError("Candidate repository must be a GitHub repository ID")
    issues = candidate.get("issue_numbers")
    if (not isinstance(issues, (list, tuple))
            or any(type(number) is not int or number < 1 for number in issues)
            or len(set(issues)) != len(issues)):
        raise ValueError("Candidate issues must be distinct positive issue numbers")
    return {
        "id": required_text(candidate.get("id"), "build ID"),
        "version": required_text(candidate.get("version"), "version"),
        "repository": repository,
        "commit_sha": _digest(candidate.get("commit_sha"), _COMMIT, "commit SHA"),
        "artifact_sha256": _digest(candidate.get("artifact_sha256"), _SHA256, "artifact SHA-256"),
        "dag_sha256": _digest(candidate.get("dag_sha256"), _SHA256, "DAG SHA-256"),
        "issue_numbers": sorted(issues),
    }


def require_product_build(product: dict, candidate: dict) -> dict:
    build = frozen_build(candidate)
    repository = product.get("repository_id")
    if repository is not None and build["repository"] != repository:
        raise ValueError("Candidate belongs to another product repository")
    return build


def _state(product: dict) -> dict:
    return product.setdefault("trials", {"active_build_id": None, "builds": {}, "runs": {}})


def _references(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("Evidence references are required")
    return [required_text(reference, "evidence reference") for reference in value]


def _active(product: dict, build_id: str) -> dict:
    state = product.get("trials")
    if not state or state["active_build_id"] != build_id or state["builds"][build_id]["status"] != "active":
        raise ValueError("The build is not active for trials")
    return state["builds"][build_id]


def activate_candidate(product: dict, candidate: dict) -> dict:
    build = require_product_build(product, candidate)
    build_id = build["id"]
    state = _state(product)
    existing = state["builds"].get(build_id)
    if existing is not None and existing["build"] != build:
        raise ValueError("Frozen candidate identity has changed")
    if state["active_build_id"] not in (None, build_id):
        raise ValueError("Pause the current candidate before selecting another build")
    if existing is not None and existing["status"] not in ("active", "paused"):
        raise ValueError("The candidate has entered release")
    state["builds"][build_id] = {"build": build, "status": "active"}
    state["active_build_id"] = build_id
    return trial_progress(product, build_id)


def pause_candidate(product: dict, build_id: str) -> dict:
    prior = trial_progress(product, build_id)
    if prior is None or prior["status"] != "paused":
        _active(product, build_id)["status"] = "paused"
        product["trials"]["active_build_id"] = None
    return trial_progress(product, build_id)


def record_trial(product: dict, build_id: str, *, run_id: str, artifact_sha256: str,
                 workload_ref: str, evidence_refs: list[str], mode: str = "read_only") -> dict:
    """Persist an actual run attested by the host, pending independent verification."""
    build_id = required_text(build_id, "build ID")
    run_id = required_text(run_id, "run ID")
    if mode not in _TRIAL_MODES:
        raise ValueError("Trial execution must be read-only, a dry run or isolated")
    receipt = {
        "id": run_id,
        "build_id": build_id,
        "artifact_sha256": _digest(artifact_sha256, _SHA256, "artifact SHA-256"),
        "workload_ref": required_text(workload_ref, "workload reference"),
        "evidence_refs": _references(evidence_refs),
        "mode": mode,
    }
    prior = product.get("trials", {}).get("runs", {}).get(run_id)
    if prior is not None:
        if {key: prior[key] for key in receipt} != receipt:
            raise ValueError("This run ID is already bound to a different trial receipt")
        return deepcopy(prior)
    build = _active(product, build_id)["build"]
    if build["artifact_sha256"] != artifact_sha256:
        raise ValueError("Trial ran a different artifact from the frozen candidate")
    receipt["verification"] = None
    product["trials"]["runs"][run_id] = receipt
    return deepcopy(receipt)


def verify_trial(product: dict, build_id: str, *, run_id: str, verifier_kind: str,
                 verdict: str, evidence_refs: list[str], verifier_id: str) -> dict:
    """Record the host's objective verdict or an authenticated human decision."""
    if verifier_kind not in ("rule", "human") or verdict not in ("pass", "fail"):
        raise ValueError("A trial needs an independent rule or a human verdict")
    verification = {"kind": verifier_kind, "id": required_text(verifier_id, "verifier ID"),
                    "verdict": verdict, "evidence_refs": _references(evidence_refs)}
    run = product.get("trials", {}).get("runs", {}).get(run_id)
    if run is None or run["build_id"] != build_id:
        raise ValueError("Unknown trial run for this candidate")
    if run["verification"] is not None:
        if run["verification"] != verification:
            raise ValueError("A recorded trial verdict is immutable")
        return deepcopy(run)
    _active(product, build_id)
    run["verification"] = verification
    return deepcopy(run)


def trial_progress(product: dict, build_id: str | None = None) -> dict | None:
    state = product.get("trials")
    if state is None:
        return None
    build = state["builds"].get(state["active_build_id"] if build_id is None else build_id)
    if build is None:
        return None
    runs = [run for run in state["runs"].values() if run["build_id"] == build["build"]["id"]]
    count = sum(run["verification"] is not None and run["verification"]["verdict"] == "pass"
                for run in runs)
    return deepcopy({**build, "runs": runs, "verified_count": count, "required_count": REQUIRED_TRIALS,
                     "eligible": build["status"] == "active" and count >= REQUIRED_TRIALS})


def lock_candidate_for_release(product: dict, candidate: dict) -> None:
    build = require_product_build(product, candidate)
    progress = trial_progress(product, build["id"])
    if progress is None or progress["build"] != build or not progress["eligible"]:
        raise ValueError("Release requires three verified trials on the active frozen build")
    _active(product, build["id"])["status"] = "releasing"


def finish_candidate_release(product: dict, build_id: str, *, interrupted: bool = False) -> None:
    state = product["trials"]
    if state["active_build_id"] != build_id or state["builds"][build_id]["status"] != "releasing":
        raise ValueError("This candidate is not locked for release")
    state["builds"][build_id]["status"] = "release_interrupted" if interrupted else "released"
    state["active_build_id"] = None
