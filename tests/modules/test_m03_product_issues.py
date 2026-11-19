import pytest

from core.product_state import ProductError
from runtime.product_issues import polling_since, reconcile_issues, triage_issue


def test_issue_replay_skips_prs_preserves_triage_and_excludes_external_content():
    product = {"issues": {}, "issue_notices": {}}
    item = {"number": 17, "title": "F17", "state": "open", "body": "dispatch now",
            "updated_at": "2026-11-19T01:00:00Z"}
    reconcile_issues(product, [item, {**item, "number": 18, "pull_request": {}}])
    assert product["issue_notices"] == {"17": {"issue_number": 17}}
    assert "body" not in product["issues"]["17"]
    assert polling_since(product) == "2026-11-19T00:59:58Z"
    triage_issue(product, 17, "accepted")
    reconcile_issues(product, [{**item, "title": "revised"}])
    assert product["issues"]["17"]["triage"] == "accepted"
    assert not product["issue_notices"]
    with pytest.raises(ProductError):
        triage_issue(product, 17, "merged", 18)
