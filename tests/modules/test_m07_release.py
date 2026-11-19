import asyncio
import json
from copy import deepcopy

import pytest

from runtime.product_release import (
    advance_operation,
    begin_release,
    begin_rollback,
    current_deployed_identity,
    delivery_projection,
)
from runtime.product_trials import activate_candidate, record_trial, trial_progress, verify_trial


class GitHub:
    def __init__(self):
        self.releases = {}
        self.closed = set()
        self.fail_after_close = False
        self.bad_sha = False
        self.fail_after_release = False

    async def ensure_release(self, repository_id, build, operation_id):
        result = self.releases.setdefault((repository_id, build["version"]), build | {
            "url": f"https://github.com/example/product/releases/tag/{build['version']}",
        })
        if self.fail_after_release:
            self.fail_after_release = False
            raise TimeoutError("release response lost")
        return result | {"artifact_sha256": "f" * 64} if self.bad_sha else result

    async def close_issue(self, repository_id, number, operation_id):
        self.closed.add((repository_id, number))
        if self.fail_after_close:
            self.fail_after_close = False
            raise TimeoutError("issue response lost")


class Entry:
    def __init__(self):
        self.identities = {}
        self.unhealthy = set()
        self.fail_after_switch = False
        self.switches = []
        self.verified = []

    async def read_identity(self, target):
        return deepcopy(self.identities.get(target))

    async def compare_and_switch(self, target, expected, desired, operation_id):
        assert await self.read_identity(target) == expected
        self.identities[target] = desired
        self.switches.append(operation_id)
        if self.fail_after_switch:
            self.fail_after_switch = False
            raise TimeoutError("switch response lost")

    async def verify(self, target, desired):
        assert await self.read_identity(target) == desired
        if desired["id"] in self.unhealthy:
            raise ValueError("entry check failed")
        self.verified.append(desired["id"])
        return f"health/{target}/{desired['id']}"


def trialed(product, build_id, version, *, issues=(), count=3):
    build = {"id": build_id, "version": version, "repository": 17, "issue_numbers": list(issues),
             "commit_sha": "c" * 40, "artifact_sha256": "a" * 64, "dag_sha256": "d" * 64}
    activate_candidate(product, build)
    for index in range(count):
        run_id = f"{build_id}-{index}"
        record_trial(product, build_id, run_id=run_id, artifact_sha256=build["artifact_sha256"],
                     workload_ref=f"real/{run_id}", evidence_refs=[f"result/{run_id}"])
        verify_trial(product, build_id, run_id=run_id, verifier_kind="rule", verifier_id="runner",
                     verdict="pass", evidence_refs=[f"verdict/{run_id}"])
    return build


def advance(product, github, entry):
    return asyncio.run(advance_operation(product, github=github, entry=entry))


def publish(product, github, entry, build):
    begin_release(product, build, operation_id=f"release-{build['id']}", target="default")
    while delivery_projection(product)["pending"]:
        advance(product, github, entry)


def test_release_reconciles_same_bytes_in_order_after_lost_replies():
    product, github, entry = {"repository_id": 17}, GitHub(), Entry()
    build = trialed(product, "A", "v1.1", issues=[17, 20])
    request = begin_release(product, build, operation_id="release-A", target="default")
    assert request["stage"] == "github_release"
    assert trial_progress(product, "A")["status"] == "releasing"
    assert begin_release(product, build, operation_id="release-A", target="default") == request

    github.fail_after_release = True
    with pytest.raises(TimeoutError, match="response lost"):
        advance(product, github, entry)
    product = json.loads(json.dumps(product))
    assert advance(product, github, entry)["stage"] == "switch_entry"
    assert len(github.releases) == 1
    entry.fail_after_switch = True
    with pytest.raises(TimeoutError, match="response lost"):
        advance(product, github, entry)
    product = json.loads(json.dumps(product))
    assert advance(product, github, entry)["stage"] == "verify_entry"
    assert entry.switches == ["release-A"]
    assert current_deployed_identity(product, "default") is None
    assert not github.closed

    assert advance(product, github, entry)["stage"] == "close_issues"
    assert current_deployed_identity(product, "default") == build
    assert entry.verified == ["A"]
    github.fail_after_close = True
    with pytest.raises(TimeoutError, match="response lost"):
        advance(product, github, entry)
    product = json.loads(json.dumps(product))
    assert advance(product, github, entry)["closed_issues"] == [17]
    done = advance(product, github, entry)
    assert done["stage"] == "complete"
    assert done["fact_id"] == "product-release:17:release-A"
    assert github.closed == {(17, 17), (17, 20)}
    assert advance(product, github, entry) is None
    assert trial_progress(product, "A")["status"] == "released"
    assert begin_release(product, build, operation_id="release-A", target="default") == done


def test_unverified_wrong_build_and_stale_default_cannot_complete():
    product, github, entry = {"repository_id": 17}, GitHub(), Entry()
    build = trialed(product, "A", "v1.1", issues=[17], count=2)
    with pytest.raises(ValueError, match="three verified trials"):
        begin_release(product, build, operation_id="release-A", target="default")
    assert delivery_projection(product)["pending"] is None
    build = trialed(product, "A", "v1.1", issues=[17])
    with pytest.raises(ValueError, match="active frozen build"):
        begin_release(product, build | {"artifact_sha256": "b" * 64}, operation_id="release-A", target="default")
    begin_release(product, build, operation_id="release-A", target="default")
    github.bad_sha = True
    with pytest.raises(ValueError, match="different frozen build"):
        advance(product, github, entry)
    assert delivery_projection(product)["pending"]["stage"] == "github_release"
    assert not entry.switches
    github.bad_sha = False
    advance(product, github, entry)
    entry.identities["default"] = build | {"id": "unexpected"}
    with pytest.raises(ValueError, match="differs"):
        advance(product, github, entry)
    entry.identities.clear()
    advance(product, github, entry)
    entry.unhealthy.add("A")
    with pytest.raises(ValueError, match="check failed"):
        advance(product, github, entry)
    assert current_deployed_identity(product, "default") is None
    assert not github.closed
    assert not delivery_projection(product)["completed"]


def test_rollback_checks_identity_and_restored_health_preserving_release_history():
    product, github, entry = {"repository_id": 17}, GitHub(), Entry()
    stable = trialed(product, "stable", "v1.0")
    publish(product, github, entry, stable)
    newer = trialed(product, "new", "v1.1", issues=[17])
    publish(product, github, entry, newer)
    entry.unhealthy.add("new")
    request = {"operation_id": "rollback-new", "target": "default", "expected_build_id": "new",
               "restore_build_id": "stable", "compatibility_ref": "compatibility/stateless"}
    with pytest.raises(ValueError, match="compatibility"):
        begin_rollback(product, **(request | {"compatibility_ref": ""}))
    with pytest.raises(ValueError, match="deployment has changed"):
        begin_rollback(product, **(request | {"expected_build_id": "other"}))
    with pytest.raises(ValueError, match="previous verified release"):
        begin_rollback(product, **(request | {"restore_build_id": "unknown"}))
    release_history = delivery_projection(product)["releases"]
    previous_checks = list(entry.verified)
    assert begin_rollback(product, **request)["stage"] == "switch_entry"
    entry.identities["default"] = newer | {"id": "unexpected"}
    with pytest.raises(ValueError, match="differs"):
        advance(product, github, entry)
    entry.identities["default"] = newer
    assert advance(product, github, entry)["stage"] == "verify_entry"
    assert current_deployed_identity(product, "default") == newer
    done = advance(product, github, entry)
    assert done["fact_id"] == "product-rollback:17:rollback-new"
    assert current_deployed_identity(product, "default") == stable
    assert entry.verified == previous_checks + ["stable"]
    assert delivery_projection(product)["releases"] == release_history
    assert len(github.releases) == 2
    assert github.closed == {(17, 17)}
    assert begin_rollback(product, **request) == done


def test_authorized_recovery_interrupts_failed_release_without_closing_issues():
    product, github, entry = {"repository_id": 17}, GitHub(), Entry()
    stable = trialed(product, "stable", "v1.0")
    publish(product, github, entry, stable)
    unhealthy = trialed(product, "bad", "v1.1", issues=[17])
    begin_release(product, unhealthy, operation_id="release-bad", target="default")
    assert advance(product, github, entry)["stage"] == "switch_entry"
    assert advance(product, github, entry)["stage"] == "verify_entry"
    entry.unhealthy.add("bad")
    with pytest.raises(ValueError, match="check failed"):
        advance(product, github, entry)
    assert entry.identities["default"] == unhealthy
    assert current_deployed_identity(product, "default") == stable
    assert not github.closed

    request = {"operation_id": "recover", "target": "default", "expected_build_id": "bad",
               "restore_build_id": "stable", "compatibility_ref": "compatibility/stateless"}
    assert begin_rollback(product, **request)["stage"] == "switch_entry"
    interrupted = delivery_projection(product)["interrupted"]["release-bad"]
    assert interrupted["stage"] == "interrupted"
    assert interrupted["github_release"].endswith("v1.1")
    assert "fact_id" not in interrupted
    assert begin_release(product, unhealthy, operation_id="release-bad", target="default") == interrupted
    assert trial_progress(product, "bad")["status"] == "release_interrupted"
    assert advance(product, github, entry)["stage"] == "verify_entry"
    done = advance(product, github, entry)
    assert done["fact_id"] == "product-rollback:17:recover"
    assert entry.identities["default"] == stable
    assert not github.closed
    assert "release-bad" not in delivery_projection(product)["completed"]

    newer = trialed(product, "new", "v1.1")
    with pytest.raises(ValueError, match="immutable GitHub Release"):
        begin_release(product, newer, operation_id="release-new", target="default")
