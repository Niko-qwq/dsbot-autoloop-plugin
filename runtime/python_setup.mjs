import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { basename, isAbsolute, join } from 'node:path';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const pending = new Map();
const PROBE = [
  'import json, re, sys',
  'ready = False',
  'try:',
  '    import pydantic',
  '    match = re.match(r"^(\\d+)\\.(\\d+)", pydantic.__version__)',
  '    ready = bool(match and int(match[1]) == 2 and int(match[2]) >= 12)',
  'except (ImportError, AttributeError):',
  '    pass',
  'print(json.dumps({"executable": sys.executable, "version": list(sys.version_info[:3]), "ready": ready}))',
].join('\n');

function interpreter(directory) {
  return process.platform === 'win32' ? join(directory, 'Scripts', 'python.exe') : join(directory, 'bin', 'python');
}

async function probe(run, python) {
  try {
    const { stdout } = await run(python, ['-I', '-B', '-c', PROBE], { timeout: 15_000 });
    const value = JSON.parse(stdout.trim());
    if (typeof value.executable === 'string' && Array.isArray(value.version) &&
      value.version.length === 3 && value.version.every(Number.isInteger) && typeof value.ready === 'boolean') {
      return value;
    }
  } catch {
    return null;
  }
  return null;
}

function supported(probed) {
  return probed?.version[0] === 3 && probed.version[1] >= 11;
}

async function buildEnvironment({ python, root, locator, requirementsPath, run }) {
  await mkdir(root, { recursive: true, mode: 0o700 });
  const active = await readFile(locator, 'utf8').catch(error => {
    if (error.code === 'ENOENT') return '';
    throw error;
  });
  if (/^env-[a-z\d]{6}$/iu.test(active)) {
    const existing = interpreter(join(root, active));
    const probed = await probe(run, existing);
    if (supported(probed) && probed.ready) return existing;
  }
  const directory = await mkdtemp(join(root, 'env-'));
  const newPython = interpreter(directory);
  const temporaryLocator = locator + '.' + randomUUID() + '.tmp';
  try {
    await run(python, ['-I', '-B', '-m', 'venv', directory], { timeout: 120_000 });
    await run(newPython, ['-I', '-B', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-r', requirementsPath],
      { timeout: 180_000, maxBuffer: 1_048_576 });
    const probed = await probe(run, newPython);
    if (!supported(probed) || !probed.ready) throw new Error('Python dependencies remain unavailable');
    await writeFile(temporaryLocator, basename(directory), { mode: 0o600, flag: 'wx' });
    await rename(temporaryLocator, locator);
    return newPython;
  } catch {
    await Promise.allSettled([
      rm(directory, { recursive: true, force: true }),
      rm(temporaryLocator, { force: true }),
    ]);
    throw new Error('Autoloop could not prepare Python dependencies. Check Python venv support and the configured Python package index.');
  }
}

export async function preparePython({ python = 'python3', dataDir, requirementsPath, run = execute }) {
  if (typeof python !== 'string' || !python || typeof dataDir !== 'string' || !isAbsolute(dataDir) ||
    typeof requirementsPath !== 'string' || !isAbsolute(requirementsPath)) {
    throw new TypeError('Autoloop requires a Python executable and absolute data and requirements paths');
  }
  const selected = await probe(run, python);
  if (!supported(selected)) throw new Error('Autoloop requires a working Python 3.11 or newer executable');
  if (selected.ready) return python;

  const requirements = await readFile(requirementsPath);
  const fingerprint = createHash('sha256').update(JSON.stringify([selected.executable, selected.version])).update(requirements).digest('hex');
  const root = join(dataDir, 'python');
  const locator = join(root, fingerprint);
  if (!pending.has(locator)) {
    pending.set(locator, buildEnvironment({ python, root, locator, requirementsPath, run }).finally(() => pending.delete(locator)));
  }
  return pending.get(locator);
}
