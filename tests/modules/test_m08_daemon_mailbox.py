import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest


PLUGIN = Path(__file__).resolve().parents[2] / "plugin_entry.py"


def write_request(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def wait_for(process, pending, description):
    deadline = monotonic() + 6
    while monotonic() < deadline:
        if process.poll() is not None:
            _, error = process.communicate()
            pytest.fail(f"Plugin exited while waiting for {description}: {error}")
        found = pending()
        if found is not None:
            return found
        sleep(0.025)
    pytest.fail(f"Plugin did not produce {description}")


def test_fresh_daemon_answers_notices_while_product_awaits_github_mailbox(tmp_path):
    data_dir, mailbox = tmp_path / "data", tmp_path / "mailbox"
    mailbox.mkdir()
    process = subprocess.Popen([
        sys.executable, "-B", str(PLUGIN), "--data-dir", str(data_dir), "--mailbox", str(mailbox),
        "--parent", str(os.getpid()), "--codex", "/usr/bin/false", "--codex-network", "disabled",
    ], cwd=PLUGIN.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def read_reply(path):
        def read_if_ready():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None

        return wait_for(process, read_if_ready, path.name)

    def submit(name, *, path="/api/autoloop/product", body=None, **identity):
        write_request(mailbox / f"ui-{name}.request.json", {
            "method": "POST" if body is not None else "GET", "path": path,
            "body": json.dumps(body) if body is not None else None, **identity,
        })
        return mailbox / f"ui-{name}.response.json"

    try:
        assert read_reply(mailbox / "ready.json")["ok"] is True
        repo = {"id": 821, "owner": "example", "name": "private-workflow", "fullName": "example/private-workflow",
                "defaultBranch": "main", "cloneUrl": "https://github.com/example/private-workflow.git",
                "visibility": "private", "installationId": 72}
        attach = submit("attach", actor_id="chief", actor_chief=True, actor_human=True, body={
            "operation": "attach", "confirmed": True, "owner": repo["owner"], "name": repo["name"],
            "title": "Offline mailbox fixture", "pm": "pm", "usage": "usage", "budget": 1, "rollback": True,
        })
        github = wait_for(process, lambda: next(mailbox.glob("github-*.request.json"), None), "GitHub request")
        assert json.loads(github.read_text(encoding="utf-8")) == {
            "operation": "verify", "owner": repo["owner"], "name": repo["name"],
        }

        notices = submit("notices", path="/api/autoloop/notices")
        assert read_reply(notices)["status"] == 200
        assert not attach.exists()

        response = github.with_name(github.name.replace(".request.json", ".response.json"))
        write_request(response, {"value": repo})
        attached = read_reply(attach)
        assert attached["status"] == 200 and attached["data"]["repository_id"] == repo["id"]
        query = {"operation": "status", "repository_id": repo["id"]}
        assert read_reply(submit("assigned", actor_id="usage", body=query))["status"] == 200
        assert read_reply(submit("unassigned", actor_id="other", body=query))["status"] == 403
    finally:
        process.terminate()
        try:
            process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=2)
