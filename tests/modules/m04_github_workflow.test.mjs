import assert from 'node:assert/strict';
import { test } from 'node:test';
import { clientFor, notFound, repository, result } from '../support/github_product_fixture.mjs';

const BASE = 'a'.repeat(40);
const MAIN = 'b'.repeat(40);
const HEAD = 'c'.repeat(40);
const MERGE = 'd'.repeat(40);
const BASE_TREE = 'e'.repeat(40);
const TREE = 'f'.repeat(40);
const BLOB = '1'.repeat(40);
const VERSION_BRANCH = 'autoloop/version/v1.2.0';

function workflowGitHub() {
  const refs = new Map([['refs/heads/main', MAIN]]);
  const files = { 'workflow.json': '{"name":"example"}\n' };
  const remote = { pr: null, readSha: null, files, refs };
  return {
    remote,
    ...clientFor(request => {
      const path = request.path;
      if (path.startsWith('/git/ref/')) {
        const ref = path.replace('/git/ref/', 'refs/');
        if (!refs.has(ref)) throw notFound();
        return result({ ref, object: { type: 'commit', sha: refs.get(ref) } });
      }
      if (path === '/git/refs') {
        refs.set(request.body.ref, request.body.sha);
        return result({ ref: request.body.ref });
      }
      if (path.startsWith('/pulls?')) return result(remote.pr ? [remote.pr] : []);
      if (path === '/git/trees') {
        assert.equal(request.body.base_tree, BASE_TREE);
        assert.deepEqual(request.body.tree, [{ path: 'workflow.json', type: 'blob', mode: '100644', content: files['workflow.json'] }]);
        return result({ sha: TREE });
      }
      if (path === '/git/commits') {
        assert.deepEqual(request.body.parents, [BASE]);
        return result({ sha: HEAD });
      }
      if (path.startsWith('/git/commits/')) {
        const sha = path.split('/').at(-1);
        remote.readSha = sha;
        return result({ sha, tree: { sha: sha === BASE ? BASE_TREE : TREE }, parents: [{ sha: BASE }] });
      }
      if (path === '/pulls') {
        assert.deepEqual(request.permissions, { contents: 'read', pull_requests: 'write' });
        const endpoint = ref => ({ ref, repo: { id: repository.id } });
        remote.pr = { number: 27, state: 'open', body: request.body.body, html_url: 'https://github.com/owner/workflow/pull/27',
          head: { ...endpoint(request.body.head), sha: HEAD }, base: endpoint(request.body.base), merged: false };
        return result(remote.pr);
      }
      if (path === '/pulls/27') return result(remote.pr);
      if (path === '/pulls/27/merge') {
        assert.deepEqual(request.body, { sha: HEAD, merge_method: 'merge' });
        remote.pr = { ...remote.pr, state: 'closed', merged: true, merge_commit_sha: MERGE };
        refs.set('refs/heads/' + VERSION_BRANCH, MERGE);
        return result({ merged: true, sha: MERGE });
      }
      if (path.startsWith('/git/trees/' + TREE)) {
        return result({ truncated: false, tree: [{ path: 'workflow.json', type: 'blob', mode: '100644', sha: BLOB }] });
      }
      if (path === '/git/blobs/' + BLOB) {
        const content = remote.changedAt === remote.readSha ? 'unexpected' : files['workflow.json'];
        return result({ encoding: 'base64', sha: BLOB, content: Buffer.from(content).toString('base64') });
      }
      assert.fail('Unexpected GitHub request ' + path);
    }),
  };
}

test('M04 stages the approved rollback baseline and reconciles the exact PR and merge commit', async () => {
  const { run, remote, calls } = workflowGitHub();
  const request = { version: 'v1.2.0', expected_base: BASE, files: remote.files, marker: 'approved' };
  assert.deepEqual(await run('ref'), { sha: MAIN });
  const first = await run('submit_workflow', request);
  assert.equal(first.number, 27);
  assert.equal(first.head, HEAD);
  assert.equal(remote.refs.get('refs/heads/' + VERSION_BRANCH), BASE);
  const mergeRequest = { number: first.number, head: first.head, expected_base: BASE, files: remote.files };
  assert.deepEqual(await run('merge_workflow', mergeRequest), { sha: MERGE, workflow_files: remote.files });
  assert.deepEqual(await run('merge_workflow', mergeRequest), { sha: MERGE, workflow_files: remote.files });
  assert.equal((await run('submit_workflow', request)).head, HEAD);
  assert.equal(calls.filter(call => call.path === '/pulls' && call.method === 'POST').length, 1);
  assert.equal(calls.filter(call => call.path === '/pulls/27/merge').length, 1);
});

test('M04 rejects changed GitHub workflow bytes before merge', async () => {
  const { run, remote, calls } = workflowGitHub();
  const first = await run('submit_workflow', { version: 'v1.2.0', expected_base: BASE, files: remote.files });
  remote.changedAt = HEAD;
  await assert.rejects(run('merge_workflow', { number: first.number, head: first.head, files: remote.files }), /differ from the approved/);
  assert.equal(calls.filter(call => call.path === '/pulls/27/merge').length, 0);
});

test('M04 rejects repository writes beyond the manifest and its declared resources', async () => {
  const { run, remote, calls } = workflowGitHub();
  const plan = { version: 'v1.2.0', expected_base: BASE };
  await assert.rejects(run('submit_workflow', { ...plan, files: { ...remote.files, 'README.md': 'overwrite' } }),
    /Invalid set of tracked workflow files/);
  await assert.rejects(run('submit_workflow', { ...plan, files: { ...remote.files, 'resources/unlisted.txt': 'overwrite' } }),
    /do not match the manifest resources/);
  assert.equal(calls.length, 0);
});
