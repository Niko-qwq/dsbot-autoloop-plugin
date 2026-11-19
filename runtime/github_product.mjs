import { createHash } from 'node:crypto';
import { GitHubAppError } from './github_app.mjs';

const API = 'https://api.github.com';
const ISSUE_READ = { issues: 'read' };
const ISSUE_WRITE = { issues: 'write' };
const CONTENT_READ = { contents: 'read' };
const CONTENT_WRITE = { contents: 'write' };
const PULL_READ = { pull_requests: 'read' };
const PULL_WRITE = { pull_requests: 'write' };
const SHA = /^[a-f0-9]{40}$/u;
const VERSION = /^v\d+\.\d+\.\d+(?:-[a-z\d.-]+)?$/iu;
const UTC = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/u;
const RESOURCE = /^resources\/[A-Za-z\d][A-Za-z\d_-]{0,79}\.(?:txt|md|json)$/u;
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });

function check(condition, message) {
  if (!condition) throw new GitHubAppError('PRODUCT_GITHUB_CONFLICT', message);
}

function positive(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function hash(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function versionName(value) {
  check(typeof value === 'string' && VERSION.test(value), 'Invalid product GitHub version');
  return value;
}

function numbered(path, number) {
  check(positive(number), 'Invalid GitHub issue or pull request number');
  return path + '/' + number;
}

function query(parameters) {
  return '?' + new URLSearchParams(parameters).toString();
}

function request(client, repository, permissions, path, options = {}) {
  return client.requestRepository({ repository, permissions, path, ...options });
}

async function get(client, repository, permissions, path, options) {
  return (await request(client, repository, permissions, path, options)).data;
}

async function optional(client, repository, permissions, path) {
  try { return await get(client, repository, permissions, path); } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

async function list(client, repository, permissions, path) {
  const all = [];
  const base = '/repos/' + repository.owner + '/' + repository.name;
  const suffix = path.split('?')[0];
  const endpoints = new Set([base + suffix, '/repositories/' + repository.id + suffix]);
  const seen = new Set();
  while (path) {
    check(seen.size < 50 && !seen.has(path), 'GitHub pagination exceeded its read limit');
    seen.add(path);
    const page = await request(client, repository, permissions, path);
    check(Array.isArray(page.data), 'GitHub returned an invalid collection');
    all.push(...page.data);
    const next = /<([^>]+)>\s*;\s*rel="next"/u.exec(page.link ?? '')?.[1];
    if (!next) break;
    const url = new URL(next);
    check(url.origin === API && endpoints.has(url.pathname) && !url.username && !url.password && !url.hash,
      'GitHub pagination did not match the bound repository endpoint');
    path = suffix + url.search;
  }
  return all;
}

function issueMetadata(issue) {
  check(positive(issue?.number) && (issue.state === 'open' || issue.state === 'closed') &&
    typeof issue.updated_at === 'string' && UTC.test(issue.updated_at), 'GitHub returned an invalid issue');
  return { number: issue.number, title: String(issue.title ?? '').slice(0, 200), state: issue.state, updated_at: issue.updated_at };
}

async function issue(client, repository, number) {
  const item = await get(client, repository, ISSUE_READ, numbered('/issues', number));
  check(item.number === number && !item.pull_request, 'GitHub issue number identifies a pull request');
  return item;
}

function comments(client, repository, number) {
  return list(client, repository, ISSUE_READ, numbered('/issues', number) + '/comments?per_page=100');
}

function ownRecord(entry, app) {
  return entry.performed_via_github_app?.id === app.appId || entry.user?.login?.toLowerCase() === app.slug.toLowerCase() + '[bot]';
}

function markerLine(kind, marker) {
  check(typeof marker === 'string' && /^[a-z\d_-]{1,128}$/iu.test(marker), 'Invalid GitHub operation marker');
  return '<!-- autoloop-' + kind + ':' + marker + ' -->';
}

async function createIssue(client, repository, { title, body, marker }) {
  check(typeof title === 'string' && title.trim() && title.length <= 200 && typeof body === 'string' && body.length <= 16_000,
    'Invalid product issue title or body');
  const line = markerLine('operation', marker);
  const app = await client.inspect();
  const entries = await list(client, repository, ISSUE_READ, '/issues?state=all&sort=created&direction=desc&per_page=100');
  const known = entries.filter(entry => !entry.pull_request && ownRecord(entry, app) && entry.body?.split(/\r?\n/u).includes(line));
  check(known.length <= 1, 'GitHub has multiple issues for this Autoloop operation');
  if (known.length) return issueMetadata(known[0]);
  const text = body.split(/\r?\n/u).includes(line) ? body : body + '\n\n' + line;
  const created = await get(client, repository, ISSUE_WRITE, '/issues', { method: 'POST', body: { title, body: text } });
  return issueMetadata(created);
}

async function triage(client, repository, { number, decision, duplicate, body, marker }) {
  check(['accepted', 'deferred', 'merged'].includes(decision), 'Invalid PM issue triage decision');
  if (decision === 'merged') check(positive(duplicate) && duplicate !== number, 'Invalid duplicate issue');
  const path = numbered('/issues', number) + '/comments';
  const text = body ?? ('Autoloop PM decision: ' + decision + (decision === 'merged' ? ' into #' + duplicate : '') + '.');
  check(typeof text === 'string' && text.trim() && text.length <= 4_000, 'Invalid PM triage text');
  const line = markerLine('triage', marker ?? hash(JSON.stringify([number, decision, duplicate ?? null, text])).slice(0, 32));
  const app = await client.inspect();
  await issue(client, repository, number);
  const history = (await comments(client, repository, number)).filter(entry => ownRecord(entry, app));
  const ownTriage = history.filter(entry => /<!-- autoloop-triage:[a-z\d_-]+ -->/iu.test(entry.body ?? ''));
  const prior = marker ? ownTriage : ownTriage.slice(-1);
  if (!prior.some(entry => entry.body?.split(/\r?\n/u).includes(line))) {
    await get(client, repository, ISSUE_WRITE, path, { method: 'POST', body: { body: text + '\n\n' + line } });
  }
  return { number, decision };
}

async function setIssueState(client, repository, number, state) {
  const before = await issue(client, repository, number);
  if (before.state === state) return issueMetadata(before);
  const updated = await get(client, repository, ISSUE_WRITE, numbered('/issues', number), { method: 'PATCH', body: { state } });
  check(updated.number === number && updated.state === state && !updated.pull_request, 'GitHub did not confirm the issue state change');
  return issueMetadata(updated);
}

function refPath(kind, name) {
  check((kind === 'heads' || kind === 'tags') && typeof name === 'string' && name.length <= 240 &&
    name.split('/').every(part => /^[a-z\d_][a-z\d_.-]*$/iu.test(part) && !part.includes('..') && !part.endsWith('.') && !part.endsWith('.lock')),
  'Invalid GitHub reference');
  return '/git/ref/' + kind + '/' + name;
}

async function resolveRef(client, repository, kind, name) {
  const result = await optional(client, repository, CONTENT_READ, refPath(kind, name));
  if (result === null) return null;
  let object = result.object;
  check(result.ref === 'refs/' + kind + '/' + name && SHA.test(object?.sha), 'GitHub returned an unexpected reference');
  for (let depth = 0; object.type === 'tag' && depth < 4; depth++) {
    const tag = await get(client, repository, CONTENT_READ, '/git/tags/' + object.sha);
    object = tag?.object;
    check(SHA.test(object?.sha), 'GitHub tag does not identify a commit');
  }
  check(object.type === 'commit', 'GitHub reference does not identify a commit');
  return object.sha;
}

async function ensureRef(client, repository, kind, name, sha) {
  let found = await resolveRef(client, repository, kind, name);
  if (found === null) {
    try {
      await get(client, repository, CONTENT_WRITE, '/git/refs', {
        method: 'POST', body: { ref: 'refs/' + kind + '/' + name, sha },
      });
    } catch (error) {
      if (error.status !== 422 && error.status !== 409) throw error;
    }
    found = await resolveRef(client, repository, kind, name);
  }
  check(found === sha, 'Existing GitHub reference points at a different commit');
}

function workflowPaths(files) {
  const paths = Array.isArray(files) ? files : Object.keys(files ?? {});
  check(paths.length >= 1 && paths.length <= 13 && paths.includes('workflow.json') &&
    paths.every(path => typeof path === 'string' && (path === 'workflow.json' || RESOURCE.test(path))) &&
    new Set(paths.map(path => path.toLowerCase())).size === paths.length,
  'Invalid set of tracked workflow files');
  return [...paths].sort();
}

function expectedFiles(files) {
  const names = workflowPaths(files);
  check(files && !Array.isArray(files) && typeof files === 'object' && names.every(path => typeof files[path] === 'string' &&
    Buffer.byteLength(files[path]) <= (path === 'workflow.json' ? 32_768 : 65_536)),
  'Tracked workflow files must contain bounded UTF-8 text');
  let manifest;
  try { manifest = JSON.parse(files['workflow.json']); } catch {
    throw new GitHubAppError('PRODUCT_GITHUB_CONFLICT', 'Invalid tracked workflow manifest');
  }
  const resources = manifest?.resources ?? [];
  check(manifest && typeof manifest === 'object' && !Array.isArray(manifest) &&
    Array.isArray(resources) && resources.length === names.length - 1 &&
    new Set(resources).size === resources.length && resources.every(path => path !== 'workflow.json' && names.includes(path)),
  'Tracked workflow files do not match the manifest resources');
  return names;
}

function pullIdentity(pr, repository, branch, base, marker) {
  check(pr && positive(pr.number) && SHA.test(pr.head?.sha) && pr.head.ref === branch && pr.base?.ref === base &&
    pr.head.repo?.id === repository.id && pr.base.repo?.id === repository.id &&
    pr.body?.split(/\r?\n/u).includes(marker), 'Existing GitHub pull request does not match the approved workflow');
  return { number: pr.number, head: pr.head.sha, base: pr.base.ref, state: pr.state, url: pr.html_url };
}

async function gitCommit(client, repository, sha) {
  const commit = await get(client, repository, CONTENT_READ, '/git/commits/' + sha);
  check(commit?.sha === sha && SHA.test(commit.tree?.sha), 'GitHub did not confirm the workflow commit');
  return commit;
}

function matchesCommit(commit, base, tree) {
  return commit.tree.sha === tree && commit.parents?.length === 1 && commit.parents[0].sha === base;
}

async function submitWorkflow(client, repository, { version, expected_base: baseSha, files, marker }) {
  const paths = expectedFiles(files);
  check(SHA.test(baseSha), 'Invalid approved base commit');
  const entries = paths.map(path => ({ path, mode: '100644', type: 'blob', content: files[path] }));
  const identity = hash(JSON.stringify([baseSha, entries.map(entry => [entry.path, entry.content]), marker ?? null]));
  const line = markerLine('workflow', identity);
  const base = 'autoloop/version/' + versionName(version);
  const branch = 'autoloop/work/' + version + '/' + identity.slice(0, 32);
  const pullQuery = '/pulls' + query({ state: 'all', head: repository.owner + ':' + branch, base, per_page: '100' });
  const current = await list(client, repository, PULL_READ, pullQuery);
  check(current.length < 2, 'GitHub has multiple pull requests for this workflow');
  const parent = await gitCommit(client, repository, baseSha);
  const tree = await get(client, repository, CONTENT_WRITE, '/git/trees', {
    method: 'POST', body: { base_tree: parent.tree.sha, tree: entries },
  });
  check(SHA.test(tree?.sha), 'GitHub did not confirm the approved workflow tree');

  if (current.length) {
    const result = pullIdentity(current[0], repository, branch, base, line);
    check(matchesCommit(await gitCommit(client, repository, result.head), baseSha, tree.sha),
      'GitHub pull request head does not match the approved workflow');
    return result;
  }

  await ensureRef(client, repository, 'heads', base, baseSha);
  let head = await resolveRef(client, repository, 'heads', branch);
  if (head === null) {
    const commit = await get(client, repository, CONTENT_WRITE, '/git/commits', {
      method: 'POST', body: { message: 'Autoloop workflow ' + version, tree: tree.sha, parents: [baseSha] },
    });
    check(SHA.test(commit?.sha), 'GitHub did not confirm the approved workflow commit');
    head = commit.sha;
    await ensureRef(client, repository, 'heads', branch, head);
  }
  check(matchesCommit(await gitCommit(client, repository, head), baseSha, tree.sha),
    'GitHub workflow branch already contains a different commit');

  let pr;
  try {
    // GitHub also checks access to both refs when opening a PR in a private repository.
    pr = await get(client, repository, { ...CONTENT_READ, ...PULL_WRITE }, '/pulls', {
      method: 'POST', body: { title: 'Autoloop workflow ' + version, body: line, head: branch, base },
    });
  } catch (error) {
    if (error.status !== 422 && error.status !== 409) throw error;
    const found = await list(client, repository, PULL_READ, pullQuery);
    if (found.length !== 1) throw error;
    pr = found[0];
  }
  const result = pullIdentity(pr, repository, branch, base, line);
  check(result.head === head, 'GitHub created the workflow pull request with a different head');
  return result;
}

async function workflowAt(client, repository, sha, paths, commit = null) {
  commit ??= await gitCommit(client, repository, sha);
  const tree = await get(client, repository, CONTENT_READ, '/git/trees/' + commit.tree.sha + '?recursive=1');
  check(Array.isArray(tree?.tree) && !tree.truncated, 'Cannot inspect the exact GitHub workflow tree');
  const entries = new Map(tree.tree.map(entry => [entry.path, entry]));
  const found = [];
  for (const path of paths) {
    const entry = entries.get(path);
    check(entry?.type === 'blob' && entry.mode === '100644' && SHA.test(entry.sha), 'Workflow path is not a tracked regular file');
    const blob = await get(client, repository, CONTENT_READ, '/git/blobs/' + entry.sha);
    check(blob?.sha === entry.sha && blob.encoding === 'base64' && typeof blob.content === 'string' &&
      blob.content.length <= 1_400_000, 'GitHub did not return the exact bounded workflow text');
    found.push([path, decoder.decode(Buffer.from(blob.content, 'base64'))]);
  }
  return Object.fromEntries(found);
}

function compareFiles(actual, expected, paths) {
  if (Array.isArray(expected)) return;
  check(paths.every(path => actual[path] === expected[path]), 'GitHub workflow bytes differ from the approved files');
}

async function mergeWorkflow(client, repository, { number, head, files, expected_base: expectedBase }) {
  const paths = Array.isArray(files) ? workflowPaths(files) : expectedFiles(files);
  const path = numbered('/pulls', number);
  const before = await get(client, repository, PULL_READ, path);
  check(SHA.test(head) && before?.number === number && before.head?.sha === head &&
    before.head.repo?.id === repository.id && before.base?.repo?.id === repository.id &&
    before.head.ref?.startsWith('autoloop/work/') && before.base.ref?.startsWith('autoloop/version/'),
  'GitHub workflow pull request does not match the approved head');
  compareFiles(await workflowAt(client, repository, head, paths), files, paths);
  let sha = before.merge_commit_sha;
  if (!before.merged) {
    if (expectedBase) {
      const current = await resolveRef(client, repository, 'heads', before.base.ref);
      check(SHA.test(expectedBase) && current === expectedBase, 'GitHub version branch moved before the approved merge');
    }
    const merged = await get(client, repository, { ...CONTENT_WRITE, ...PULL_WRITE }, path + '/merge', {
      method: 'PUT', body: { sha: head, merge_method: 'merge' },
    });
    check(merged?.merged === true, 'GitHub has not merged the approved workflow pull request');
    sha = merged.sha;
  }
  check(SHA.test(sha), 'GitHub did not confirm the merge commit');
  const commit = await gitCommit(client, repository, sha);
  if (expectedBase) {
    check(commit.parents?.[0]?.sha === expectedBase &&
      (commit.parents.length === 1 || (commit.parents.length === 2 && commit.parents[1]?.sha === head)),
    'GitHub merged the workflow against a different base or head');
  }
  const workflowFiles = await workflowAt(client, repository, sha, paths, commit);
  compareFiles(workflowFiles, files, paths);
  return { sha, workflow_files: workflowFiles };
}

function releaseFiles(build, payload, dag, repository) {
  check(build?.repository === repository.id && SHA.test(build.commit_sha), 'Frozen release does not match the bound GitHub repository');
  const files = [
    { name: 'workflow.zip', encoded: payload, digest: build.artifact_sha256 },
    { name: 'TASK_DAG.json', encoded: dag, digest: build.dag_sha256 },
  ];
  return files.map(({ name, encoded, digest }) => {
    check(typeof encoded === 'string' && encoded.length <= 12_000_000, 'Invalid frozen GitHub release asset');
    const bytes = Buffer.from(encoded, 'base64');
    check(bytes.toString('base64') === encoded && hash(bytes) === digest, 'Release asset digest does not match the frozen build');
    return { name, bytes, digest };
  });
}

function releaseIdentity(entry, tag, text) {
  check(positive(entry?.id) && entry.tag_name === tag && entry.body === text && typeof entry.html_url === 'string',
    'Existing GitHub Release does not match the frozen build');
  return entry;
}

async function releasesForTag(client, repository, tag) {
  const releases = await list(client, repository, CONTENT_WRITE, '/releases?per_page=100');
  const matching = releases.filter(release => release.tag_name === tag);
  check(matching.length < 2, 'GitHub has multiple releases for the same version tag');
  return matching[0] ?? null;
}

async function releaseAssets(client, repository, id, permissions = CONTENT_READ) {
  return list(client, repository, permissions, '/releases/' + id + '/assets?per_page=100');
}

function assertAsset(asset, file) {
  check(asset?.name === file.name && asset.state === 'uploaded' && asset.size === file.bytes.length &&
    asset.digest === 'sha256:' + file.digest, 'GitHub Release asset does not match the frozen digest');
}

async function ensureRelease(client, repository, { build, operation_id: operationId, payload, dag }) {
  const files = releaseFiles(build, payload, dag, repository);
  const tag = versionName(build.version);
  check(typeof operationId === 'string' && operationId.length > 0 && operationId.length <= 200, 'Invalid release operation identifier');
  const identity = { version: tag, commit_sha: build.commit_sha, artifact_sha256: build.artifact_sha256, dag_sha256: build.dag_sha256 };
  const marker = markerLine('release', hash(JSON.stringify([operationId, identity])));
  const text = 'Autoloop frozen workflow release ' + tag + '\n\n' +
    'Commit: ' + identity.commit_sha + '\n' +
    'Workflow SHA-256: ' + identity.artifact_sha256 + '\n' +
    'DAG SHA-256: ' + identity.dag_sha256 + '\n\n' + marker;

  await ensureRef(client, repository, 'tags', tag, build.commit_sha);
  let draft = await releasesForTag(client, repository, tag);
  if (draft === null) {
    try {
      draft = await get(client, repository, CONTENT_WRITE, '/releases', {
        method: 'POST', body: { tag_name: tag, target_commitish: build.commit_sha, name: tag, body: text, draft: true },
      });
    } catch (error) {
      if (error.status !== 422 && error.status !== 409) throw error;
      draft = await releasesForTag(client, repository, tag);
      if (draft === null) throw error;
    }
  }
  releaseIdentity(draft, tag, text);
  const assets = await releaseAssets(client, repository, draft.id, CONTENT_WRITE);
  for (const file of files) {
    const matches = assets.filter(asset => asset.name === file.name);
    check(matches.length < 2, 'GitHub Release contains duplicate asset names');
    const existing = matches[0];
    if (existing?.state === 'starter' && draft.draft) {
      check(positive(existing.id), 'GitHub Release has an invalid pending asset');
      await get(client, repository, CONTENT_WRITE, '/releases/assets/' + existing.id, { method: 'DELETE' });
    } else if (existing) {
      assertAsset(existing, file);
      continue;
    }
    check(draft.draft, 'Published GitHub Release is missing frozen assets');
    try {
      const uploaded = await client.uploadReleaseAsset({ repository, releaseId: draft.id, name: file.name, bytes: file.bytes });
      assertAsset(uploaded.data, file);
    } catch (error) {
      if (error.status !== 422 && error.status !== 409) throw error;
      const refreshed = await releaseAssets(client, repository, draft.id, CONTENT_WRITE);
      assertAsset(refreshed.find(asset => asset.name === file.name), file);
    }
  }
  if (draft.draft) {
    await get(client, repository, CONTENT_WRITE, '/releases/' + draft.id, { method: 'PATCH', body: { draft: false } });
  }
  const observed = releaseIdentity(await get(client, repository, CONTENT_READ, '/releases/' + draft.id), tag, text);
  check(observed.draft === false && await resolveRef(client, repository, 'tags', tag) === build.commit_sha,
    'GitHub did not confirm the published frozen version and exact tag');
  const observedAssets = await releaseAssets(client, repository, observed.id);
  for (const file of files) assertAsset(observedAssets.find(asset => asset.name === file.name), file);
  return { ...identity, url: observed.html_url };
}

export async function dispatchGitHubProduct(client, args) {
  const { operation, repository } = args ?? {};
  if (operation === 'verify') return client.verifyRepository({ owner: args.owner, repo: args.repo ?? args.name });
  if (operation === 'provision') return client.provisionPrivateRepository({
    owner: args.owner, name: args.name, accountType: args.accountType === 'personal' ? 'user' : args.accountType,
  });
  check(positive(repository?.id) && repository.visibility === 'private', 'Product GitHub operation requires a private repository binding');
  switch (operation) {
    case 'issues': {
      const filters = { state: 'all', sort: 'updated', direction: 'asc', per_page: '100' };
      if (args.since != null) {
        check(typeof args.since === 'string' && UTC.test(args.since), 'Invalid GitHub history cursor');
        filters.since = args.since;
      }
      return (await list(client, repository, ISSUE_READ, '/issues' + query(filters))).filter(entry => !entry.pull_request).map(issueMetadata);
    }
    case 'issue': {
      const remote = await issue(client, repository, args.number);
      return { ...issueMetadata(remote), body: String(remote.body ?? '').slice(0, 64_000), url: remote.html_url,
        comments: (await comments(client, repository, args.number)).slice(-100).map(comment => ({
          body: String(comment.body ?? '').slice(0, 16_000), author: comment.user?.login, url: comment.html_url,
        })) };
    }
    case 'create_issue': return createIssue(client, repository, args);
    case 'triage': return triage(client, repository, args);
    case 'reopen': return setIssueState(client, repository, args.number, 'open');
    case 'close_issue': return setIssueState(client, repository, args.number, 'closed');
    case 'ref': {
      const ref = args.ref ?? repository.defaultBranch;
      const kind = args.kind ?? (ref === repository.defaultBranch || !VERSION.test(ref) ? 'branch' : 'tag');
      check(kind === 'branch' || kind === 'tag', 'Invalid GitHub reference type');
      const sha = await resolveRef(client, repository, kind === 'tag' ? 'tags' : 'heads', ref);
      check(sha !== null, 'Requested product GitHub reference was not found');
      return { sha };
    }
    case 'submit_workflow': return submitWorkflow(client, repository, args);
    case 'merge_workflow': return mergeWorkflow(client, repository, args);
    case 'ensure_release': return ensureRelease(client, repository, args);
    default: throw new GitHubAppError('UNKNOWN_OPERATION', 'Unknown product GitHub operation');
  }
}
