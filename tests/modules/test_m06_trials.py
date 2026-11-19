import pytest

from runtime.product_trials import (
    activate_candidate,
    pause_candidate,
    record_trial,
    trial_progress,
    verify_trial,
)


def build(build_id="A", artifact="a", repo=17):
    return {"id": build_id, "repository": repo, "version": "v1.1", "commit_sha": "c" * 40,
            "artifact_sha256": artifact * 64, "dag_sha256": "d" * 64, "issue_numbers": [17]}


def record(product, candidate, run_id, **overrides):
    receipt = {"run_id": run_id, "artifact_sha256": candidate["artifact_sha256"],
               "workload_ref": f"workload/{run_id}", "evidence_refs": [f"results/{run_id}"]}
    return record_trial(product, candidate["id"], **(receipt | overrides))


def verify(product, candidate, run_id, **overrides):
    decision = {"run_id": run_id, "verifier_kind": "rule", "verdict": "pass",
                "evidence_refs": [f"checks/{run_id}"], "verifier_id": "artifact-check"}
    return verify_trial(product, candidate["id"], **(decision | overrides))


def test_distinct_verified_runs_count_and_retransmission_is_idempotent():
    product, candidate = {"repository_id": 17}, build()
    assert activate_candidate(product, candidate)["verified_count"] == 0
    record(product, candidate, "run-1")
    record(product, candidate, "run-2")
    record(product, candidate, "run-3")
    record(product, candidate, "run-4")
    assert trial_progress(product)["verified_count"] == 0
    with pytest.raises(ValueError, match="independent"):
        verify(product, candidate, "run-4", verifier_kind="self")
    verify(product, candidate, "run-4", verdict="fail")
    verified = verify(product, candidate, "run-1")
    assert record(product, candidate, "run-1") == verified
    assert verify(product, candidate, "run-1") == verified
    with pytest.raises(ValueError, match="immutable"):
        verify(product, candidate, "run-1", verdict="fail")
    verify(product, candidate, "run-2", verifier_kind="human", verifier_id="owner")
    assert trial_progress(product)["verified_count"] == 2
    assert not trial_progress(product)["eligible"]
    verify(product, candidate, "run-3")
    assert trial_progress(product)["verified_count"] == 3
    assert trial_progress(product)["eligible"]


def test_pause_replacement_retains_history_and_resume_requires_exact_build():
    product, first, second = {"repository_id": 17}, build(), build("B", "b")
    activate_candidate(product, first)
    for run_id in ("a-1", "a-2"):
        record(product, first, run_id)
        verify(product, first, run_id)
    with pytest.raises(ValueError, match="Pause the current"):
        activate_candidate(product, second)
    assert pause_candidate(product, "A")["verified_count"] == 2
    assert trial_progress(product) is None
    with pytest.raises(ValueError, match="not active"):
        record(product, first, "a-late")
    assert activate_candidate(product, second)["verified_count"] == 0
    with pytest.raises(ValueError, match="already bound"):
        record(product, second, "a-1")
    pause_candidate(product, "B")
    with pytest.raises(ValueError, match="identity has changed"):
        activate_candidate(product, first | {"artifact_sha256": "f" * 64})
    assert activate_candidate(product, first)["verified_count"] == 2
    record(product, first, "a-3", mode="dry_run")
    verify(product, first, "a-3")
    assert trial_progress(product)["eligible"]
    assert trial_progress(product, "B")["verified_count"] == 0


def test_refuses_unscoped_and_unsafe_receipts_and_projects_copies():
    product, candidate = {"repository_id": 17}, build()
    with pytest.raises(ValueError, match="another product"):
        activate_candidate(product, build(repo=18))
    activate_candidate(product, candidate)
    with pytest.raises(ValueError, match="different artifact"):
        record(product, candidate, "bad-artifact", artifact_sha256="f" * 64)
    with pytest.raises(ValueError, match="read-only"):
        record(product, candidate, "real-write", mode="real_write")
    with pytest.raises(ValueError, match="references"):
        record(product, candidate, "missing-evidence", evidence_refs=[])
    assert trial_progress(product)["runs"] == []
    result = trial_progress(product)
    result["build"]["issue_numbers"].append(42)
    assert trial_progress(product)["build"]["issue_numbers"] == [17]
