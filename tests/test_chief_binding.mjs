import assert from 'node:assert/strict';
import { test } from 'node:test';
import { anchorNotice, botBinding, chiefSessions, noticeTargets } from '../index.mjs';

const state = {
  mainBotIds: ['chief', 'another-chief'],
  bots: {
    chief: {
      sessionId: 'foreground-next',
      segments: [{ sessionId: 'foreground-original' }],
      openParts: [{ sessionId: 'background-next', segments: [{ sessionId: 'background-original' }] }],
    },
    'another-chief': { sessionId: 'another-session' },
    pm: { sessionId: 'project-pm', instructions: '[Autoloop PM project:project]' },
    unassigned: { sessionId: 'unassigned-pm', instructions: '[Autoloop PM]' },
  },
  rooms: {
    team: { sessions: { chief: 'room-next' }, segments: { chief: [{ sessionId: 'room-original' }] } },
  },
};

function entries(chiefSession, notice = { id: 'completed', type: 'PROJECT_COMPLETED' }) {
  return noticeTargets({ project: { id: 'project', name: 'Project', chief_session: chiefSession }, notices: [notice] }, state);
}

test('project scope follows the same Chief thread through rollover and excludes other threads and Bots', () => {
  for (const thread of ['foreground', 'background', 'room']) {
    const next = `${thread}-next`, original = `${thread}-original`;
    assert.deepEqual(chiefSessions(state, next), [next, original]);
    assert.equal(botBinding(state, next).botId, 'chief');
    assert.equal(botBinding(state, original), undefined);
    assert.equal(entries(original)[0].session_id, next);
  }
  assert.deepEqual(chiefSessions(state, 'foreground-original'), []);
  assert.deepEqual(chiefSessions(state, 'another-session'), ['another-session']);
  assert.deepEqual(chiefSessions(state, 'project-pm'), []);
  assert.deepEqual(entries('unknown-original'), []);
  assert.deepEqual(entries('project-pm'), []);
  assert.equal(botBinding(state, 'project-pm').projectId, 'project');
  assert.equal(botBinding(state, 'unassigned-pm'), undefined);
});

test('a native delivery keeps its own turn across rollover', () => {
  const [entry] = entries('background-original', {
    id: 'completed', type: 'PROJECT_COMPLETED',
    delivery: { status: 'DELIVERED', session_id: 'background-original', turn: 0, message_seq: 8 },
  });
  const original = anchorNotice(entry);
  assert.equal(original.session_id, 'background-original');
  assert.equal(original.current_session_id, 'background-next');
  assert.equal(original.turn, 0);
  assert.ok(!('earlier' in original) && !('chief_session' in original));

  const waiting = anchorNotice(entries('background-original', {
    id: 'new', type: 'ALERT_ESCALATED', delivery: { status: 'SENDING', session_id: 'background-next', turn: 3 },
  })[0]);
  assert.equal(waiting.session_id, 'background-next');
  assert.equal(waiting.turn, undefined);
});

test('a native receipt cannot anchor a card to a different Chief thread', () => {
  const anchored = anchorNotice(entries('room-original', {
    id: 'alert', type: 'ALERT_ESCALATED', delivery: { status: 'DELIVERED', session_id: 'foreground-next', turn: 2 },
  })[0]);
  assert.equal(anchored.session_id, 'room-next');
  assert.equal(anchored.turn, undefined);
});
