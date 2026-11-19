import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const source = fileURLToPath(new URL('..', import.meta.url));
const output = resolve(process.argv[2] ?? join(source, 'dist'));
const scratch = await mkdtemp(join(tmpdir(), 'autoloop-package-'));
const stage = join(scratch, 'package');
const cache = join(scratch, 'npm-cache');

function npm(args, cwd = source, stdio) {
  return execFileSync('npm', args, { cwd, encoding: 'utf8', stdio });
}

function packPath(root, path) {
  const filename = resolve(root, path);
  const local = relative(root, filename);
  if (!local || local === '..' || local.startsWith('..' + sep)) throw new Error('Unexpected npm package path');
  return filename;
}

async function sameBytes(a, b) {
  return (await readFile(a)).equals(await readFile(b));
}

async function copyReleaseFile(sourcePath, outputPath) {
  try {
    await copyFile(sourcePath, outputPath, constants.COPYFILE_EXCL);
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if (!(await sameBytes(sourcePath, outputPath))) {
      throw new Error('Output already contains different bytes for this version. Choose a new version or output directory.');
    }
  }
}

try {
  const [{ files }] = JSON.parse(npm(['pack', '--dry-run', '--json', '--ignore-scripts', '--cache', cache]));
  for (const file of files) {
    const destination = packPath(stage, file.path);
    await mkdir(dirname(destination), { recursive: true });
    await copyFile(packPath(source, file.path), destination);
  }
  npm(['run', 'build', '--', '--outfile=' + join(stage, 'client.js')], source, 'inherit');
  const [{ filename }] = JSON.parse(npm(['pack', '--json', '--ignore-scripts', '--cache', cache, '--pack-destination', scratch], stage));
  if (basename(filename) !== filename || !filename.endsWith('.tgz')) throw new Error('Unexpected npm package filename');
  const packaged = join(scratch, filename);
  const digest = createHash('sha256').update(await readFile(packaged)).digest('hex');
  const checksum = packaged + '.sha256';
  await writeFile(checksum, digest + '  ' + filename + '\n');
  await mkdir(output, { recursive: true });
  await copyReleaseFile(packaged, join(output, filename));
  await copyReleaseFile(checksum, join(output, basename(checksum)));
  process.stdout.write(JSON.stringify({ package: join(output, filename), checksum: join(output, basename(checksum)), sha256: digest }) + '\n');
} finally {
  await rm(scratch, { recursive: true, force: true });
}
