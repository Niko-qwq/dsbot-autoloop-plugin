type Bot = { id: string; name: string; instructions?: string };

export type ProjectPM = { id: string; title?: string; nickname?: string | null; create: boolean };

const TEMPORARY_PREFIX = '[Autoloop temporary name: ';
const RESERVED_NAMES = new Set(['all', 'everyone', '所有人', '全体', 'user', 'you']);
const normalize = (name: string) => name.trim().toLowerCase();

function clip(name: string, length: number) {
  return name.slice(0, length).replace(/[\ud800-\udbff]$/, '').trimEnd();
}

function displayText(value?: string | null) {
  return (value ?? '').replace(/[@`\s\u0000-\u001f\u007f]+/g, ' ').trim()
    .replace(/^["'“”「『《]+|["'“”」』》]+$/g, '').trim();
}

function freeName(base: string, title: string, roster: Bot[], current?: Bot) {
  const taken = new Set(roster.filter(bot => bot.id !== current?.id).map(bot => normalize(bot.name)));
  if (!taken.has(normalize(base))) return base;
  const qualified = `${clip(base, 16)} · ${clip(title, 20)}`;
  if (title && !taken.has(normalize(qualified))) return qualified;
  let number = 2;
  while (taken.has(normalize(`${base} ${number}`))) number += 1;
  return `${base} ${number}`;
}

async function botRequest<T>(endpoint: string, payload = {}, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api/bot', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ endpoint, payload }), signal,
  });
  if (!response.ok) throw new Error('DS Bot HTTP ' + response.status);
  const result = await response.json();
  if (!result.ok) throw new Error(result.error.message);
  return result.value as T;
}

export async function syncProjectPMs(targets: ProjectPM[], names: { pending: string; project: string }, signal: AbortSignal) {
  const locks = navigator.locks;
  const sync = async () => {
    const { bots } = await botRequest<{ bots: Bot[] }>('state', {}, signal);
    for (const target of targets) {
      signal.throwIfAborted();
      const marker = `[Autoloop PM project:${target.id}]`;
      const bot = bots.find(item => item.instructions?.split('\n', 1)[0] === marker);
      const title = target.title !== target.id ? displayText(target.title) : '';
      const proposed = clip(displayText(target.nickname), 30);
      const nickname = RESERVED_NAMES.has(normalize(proposed)) ? '' : proposed;
      const pending = target.title === undefined && !nickname;
      const temporary = (name: string) => `${TEMPORARY_PREFIX}${name}]`;
      if (bot) {
        const generated = bot.instructions?.split('\n', 2)[1] === temporary(bot.name);
        if (pending || !generated) continue;
      } else if (!target.create || (pending && !locks)) continue;

      const base = nickname || (title ? `${clip(title, 28)} PM` : pending ? names.pending : names.project);
      const name = freeName(base, nickname ? title : '', bots, bot);
      // A write in flight must settle before another tab acquires the lock.
      // Aborting a fetch only stops listening for the response, never the host mutation.
      if (bot) {
        await botRequest('update-bot', { id: bot.id, name });
        bot.name = name;
      } else {
        const created = await botRequest<Bot>('create-bot', {
          name, role: '项目主管', avatar: { shape: 'cat' },
          brief: '请简短介绍你可以回答本项目的进展，并和用户讨论方案及验收。',
          instructions: marker + (pending ? `\n${temporary(name)}` : '') + '\n你是当前 Autoloop 项目的主管。用 autoloop_status 核对项目和任务，在对话中讨论目标、方案和验收。用户选定方向后，提交可确认的最小计划变更。',
        });
        bots.push(created);
      }
    }
  };
  return locks ? locks.request('autoloop-native-pm-roster', { signal }, sync) : sync();
}
