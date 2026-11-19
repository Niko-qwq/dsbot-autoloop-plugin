export function matchesTaskInstruction(text, action, taskId) {
  if (action !== 'RETRY' || typeof text !== 'string' || typeof taskId !== 'string' || !/^[\w.-]{1,64}$/u.test(taskId)) return false;
  const verb = '(?:重试|重跑|再跑一次|retry)';
  const id = taskId.replaceAll('.', '\\.');
  const task = '(?:任务\\s*|task\\s+)?(?:`' + id + '`|' + id + ')';
  const command = new RegExp(
    '^(?:(?:请|麻烦|帮我|先|那就|就|please\\s+)\\s*)*(?:' + verb + '\\s*' + task + '|(?:把\\s*)?' + task + '\\s*' + verb + ')(?:\\s*(?:吧|一下))?[。！!]?$',
    'iu',
  );
  const [first] = text.trim().split(/[\n，,；;]/u, 1);
  return command.test(first.trim());
}

function userText(message) {
  return { id: message.id, text: message.content.filter(block => block.type === 'text').map(block => block.text).join('\n') };
}

function clarifiedIntake(session, message) {
  if (message.source.outcome !== 'answered') return;
  let reply;
  try { reply = JSON.parse(userText(message).text); }
  catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return;
  }
  const detail = reply?.questions?.[0]?.detail;
  if (reply?.tool !== 'ask_user' || !/^(?:立项澄清\s*·|Project clarification\s*·)/iu.test(detail ?? '')) return;
  const history = session.deriveMessages();
  const question = history.findLastIndex(item => item.role === 'assistant' && item.content.some(block =>
    block.type === 'tool-call' && block.name === 'ask_user' && block.id === message.source.callId));
  if (question === -1) return;
  const original = history.slice(0, question).findLast(item => item.role === 'user' && item.source?.kind === 'user');
  return original && userText(original);
}

export function bindChiefTaskActions(ctx, roleOf) {
  const trustedInput = new WeakMap();
  const intakeInput = new WeakMap();
  const stopStep = ctx.on('agent/pre-step', ({ agent, messages }, next) => {
    if (roleOf(agent?.id) === 'chief') {
      const input = messages.findLast(message => ['user', 'user-question-reply'].includes(message.source?.kind));
      if (input?.source.kind === 'user') {
        const evidence = userText(input);
        trustedInput.set(agent.session, evidence);
        intakeInput.set(agent.session, evidence);
      } else if (input) {
        trustedInput.delete(agent.session);
        const original = clarifiedIntake(agent.session, input);
        if (original) intakeInput.set(agent.session, original);
        else intakeInput.delete(agent.session);
      }
    }
    return next();
  });
  const stopTurn = ctx.on('session/event', (session, event) => {
    if (event.type === 'turn/start' || event.type === 'turn/end') {
      trustedInput.delete(session);
      intakeInput.delete(session);
    }
  });
  return {
    intake: agent => intakeInput.get(agent.session),
    authorized: (agent, action, taskId) => matchesTaskInstruction(trustedInput.get(agent.session)?.text, action, taskId),
    dispose() { stopTurn(); stopStep(); },
  };
}
