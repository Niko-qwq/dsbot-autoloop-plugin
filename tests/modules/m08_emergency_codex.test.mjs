import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { startEmergencyRepair } from '../../runtime/emergency_codex.mjs';

async function fixture(context, instructions) {
  const root = await mkdtemp(join(tmpdir(), 'autoloop-emergency-codex-'));
  context.after(() => rm(root, { recursive: true, force: true }));
  const sourceDirectory = join(root, 'source');
  const executable = join(root, 'codex-fixture');
  await mkdir(sourceDirectory);
  const git = (...args) => execFileSync('git', ['-C', sourceDirectory, ...args], { encoding: 'utf8' }).trim();
  git('init', '--quiet');
  await writeFile(join(sourceDirectory, 'tool.js'), 'export const healthy = false;\n');
  git('add', 'tool.js');
  git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '--message=fixture');
  await writeFile(executable, `#!${process.execPath}\nconst fs = require('node:fs');
    const input = fs.readFileSync(0, 'utf8');
    const marker = input.match(/AUTOLOOP_ACK [A-Za-z0-9_-]+ [a-f0-9]{40}/)[0];
    const send = event => process.stdout.write(JSON.stringify(event) + '\\n');
    send({ type: 'thread.started', thread_id: 'this-is-a-real-cli-thread' });
    send({ type: 'turn.started' });
    ${instructions}
  `);
  await chmod(executable, 0o700);
  return { executable, sourceDirectory, sourceCommit: git('rev-parse', 'HEAD'),
    recoveryDirectory: join(root, 'recovery'), operationId: 'test-operation', current: 'v2', rollback: 'v1' };
}

test('Codex repair waits for a genuine acknowledgement and retains a verified isolated proposal', async context => {
  const acknowledgements = [];
  const configuration = await fixture(context, `
    if (input.includes('ghp_private123') || input.includes('http://private.example.invalid')) process.exit(4);
    if (!process.argv.includes('sandbox_workspace_write.network_access=false')) process.exit(5);
    fs.writeFileSync('.autoloop-emergency-ack', marker + '\\n');
    send({ type: 'item.completed', item: { type: 'command_execution', exit_code: 0 } });
    send({ type: 'item.completed', item: { type: 'agent_message', text: marker } });
    fs.writeFileSync('tool.js', 'export const healthy = true;\\n');
    send({ type: 'turn.completed' });
  `);
  const attempt = await startEmergencyRepair({ ...configuration,
    detail: 'Check ghp_private123 at http://private.example.invalid?q=secret', onAcknowledged: event => acknowledgements.push(event) });
  const result = await attempt.result;
  assert.equal(result.status, 'ready');
  assert.deepEqual(acknowledgements, [{ operationId: 'test-operation', workspace: result.workspace, threadId: 'this-is-a-real-cli-thread' }]);
  assert.match(await readFile(join(result.workspace, 'tool.js'), 'utf8'), /true/);
  assert.match(await readFile(join(configuration.sourceDirectory, 'tool.js'), 'utf8'), /false/);
  assert.equal(execFileSync('git', ['-C', result.workspace, 'remote'], { encoding: 'utf8' }), '');
});

test('a started CLI turn or an unrelated model response is never an acknowledgement', async context => {
  const configuration = await fixture(context, `
    send({ type: 'item.completed', item: { type: 'agent_message', text: 'Unrelated model response.' } });
    send({ type: 'item.completed', item: { type: 'command_execution', exit_code: 0 } });
  `);
  const attempt = await startEmergencyRepair({ ...configuration, onAcknowledged: () => assert.fail('No real acknowledgement') });
  const result = await attempt.result;
  assert.equal(result.status, 'failed');
  assert.equal(result.acknowledged, false);
  assert.match(result.detail, /did not acknowledge/);
});

test('stopping the repair suppresses late acknowledgements', async context => {
  const configuration = await fixture(context, `
    setTimeout(() => send({type: 'item.completed', item: { type: 'agent_message', text: marker }}), 300);
  `);
  let callbacks = 0;
  const controller = new AbortController();
  const attempt = await startEmergencyRepair({ ...configuration, signal: controller.signal, onAcknowledged: () => callbacks++ });
  controller.abort();
  await attempt.stop();
  assert.equal((await attempt.result).acknowledged, false);
  assert.equal(callbacks, 0);
});
