import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { dispatchGitHubProduct } from '../../runtime/github_product.mjs';

const repository = { id: 17, owner: 'owner', name: 'tool', defaultBranch: 'main', visibility: 'private', installationId: 18 };
const zip = Buffer.from('frozen.zip');
const dag = Buffer.from('{}');
const digest = value => createHash('sha256').update(value).digest('hex');
const build = {
  id: digest(zip), version: 'v1.1.0', repository: 17, commit_sha: '1'.repeat(40),
  artifact_sha256: digest(zip), dag_sha256: digest(dag), issue_numbers: [17],
};
const req = { operation: 'ensure_release', repository, build, operation_id: 'same operation on replay',
  payload: zip.toString('base64'), dag: dag.toString('base64') };

function github(t, { tag, loseFirstUploadResponse = false } = {}) {
  let release;
  const assets = [];
  const writes = [];
  const read = data => ({ data, status: 200 });
  const client = {
    async requestRepository({ path, method, body }) {
      if (path === '/git/ref/tags/' + build.version) {
        if (!tag) throw Object.assign(new Error('not found'), { status: 404 });
        return read({ ref: 'refs/tags/' + build.version, object: { type: 'commit', sha: tag } });
      }
      if (path === '/git/refs') {
        assert.equal(body.ref, 'refs/tags/' + build.version);
        writes.push('tag');
        tag = body.sha;
        return read({});
      }
      if (path.startsWith('/releases?')) return read(release ? [release] : []);
      if (path === '/releases' && method === 'POST') {
        assert.equal(body.draft, true);
        assert.equal(body.target_commitish, build.commit_sha);
        writes.push('draft');
        release = { ...body, id: 3, html_url: 'https://github.com/owner/tool/releases/tag/' + build.version };
        return read(release);
      }
      if (path.startsWith('/releases/3/assets?')) return read(assets);
      if (path === '/releases/3' && method === 'PATCH') {
        assert.deepEqual(body, { draft: false });
        assert.equal(assets.length, 2);
        writes.push('publish');
        release = { ...release, ...body };
        return read(release);
      }
      if (path === '/releases/3') return read(release);
      t.diagnostic(path);
      assert.fail('Unexpected GitHub route');
    },
    async uploadReleaseAsset({ name, bytes }) {
      writes.push(name);
      const file = { name, size: bytes.length, digest: 'sha256:' + digest(bytes), state: 'uploaded' };
      assets.push(file);
      if (loseFirstUploadResponse) {
        loseFirstUploadResponse = false;
        throw new Error('mock network lost upload reply after commit');
      }
      return read(file);
    },
  };
  return { client, writes, assets };
}

test('M07 preserves the exact frozen tag and reconciles assets after a lost upload reply', async t => {
  const { client, writes } = github(t, { loseFirstUploadResponse: true });
  await assert.rejects(dispatchGitHubProduct(client, req), /mock network lost/);
  assert.deepEqual(writes, ['tag', 'draft', 'workflow.zip']);
  const observed = await dispatchGitHubProduct(client, req);
  assert.deepEqual(observed, {
    version: build.version, commit_sha: build.commit_sha, artifact_sha256: build.artifact_sha256,
    dag_sha256: build.dag_sha256, url: 'https://github.com/owner/tool/releases/tag/' + build.version,
  });
  assert.deepEqual(await dispatchGitHubProduct(client, req), observed);
  assert.deepEqual(writes, ['tag', 'draft', 'workflow.zip', 'TASK_DAG.json', 'publish']);
});

test('M07 refuses to publish a release on a different tag or with changed bytes', async t => {
  const wrongTag = github(t, { tag: '2'.repeat(40) });
  await assert.rejects(dispatchGitHubProduct(wrongTag.client, req), /different commit/);
  assert.deepEqual(wrongTag.writes, []);
  const wrongAsset = github(t);
  await assert.rejects(dispatchGitHubProduct(wrongAsset.client, { ...req, payload: Buffer.from('changed').toString('base64') }),
    /digest does not match/);
  assert.deepEqual(wrongAsset.writes, []);
});
