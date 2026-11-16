// Is a service of the running demo stack an older build than the code in this checkout?
//
//   node e2e/stale-images.mjs          from the repository root; exits 1 and names each image that is behind its sources
//
// `docker compose up` without `--build` reuses an image of any age. When the web UI then reads a field that an older
// orchestrator does not send, the page is blank and nothing says why. This says it first: for each service of the stack
// that is up, the image's build time against the last commit (and any uncommitted file) among the sources it is built from.
//
// CI builds every image from the commit it tests, so it is never behind. On a machine whose image builds cannot reach the
// internet, run the stack with the sources mounted over the images (see ui/e2e/README.md) and set VT_ALLOW_STALE_IMAGES=1.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// The two places a stack is started from, each a Compose project of its own (the demo's, and the regression's on top of it).
const STACKS = [path.join(root, 'demo'), path.join(root, 'e2e')];

// What each service's image is built from (the Dockerfile's own COPY lines, as paths from the repository root).
const GO = ['libs/go', 'proto', 'buf.yaml', 'buf.lock', 'buf.gen.yaml'];
const ORCHESTRATOR = ['orchestrator-service/app', 'orchestrator-service/docker', 'orchestrator-service/requirements.txt', 'orchestrator-service/main.py', 'orchestrator-service/pyproject.toml', ...GO];
export const SOURCES = {
  'orchestrator-api': ORCHESTRATOR,
  'orchestrator-worker': ORCHESTRATOR,
  gateway: ['gateway-service', ...GO],
  watcher: ['watcher-service', ...GO],
  ui: ['ui/src', 'ui/index.html', 'ui/package.json', 'ui/package-lock.json', 'ui/vite.config.ts', 'ui/tsconfig.json', 'ui/nginx.conf.template', 'ui/Dockerfile'],
  'demo-seed': ['demo/seed'],
  'e2e-regression': ['e2e/init', 'demo/seed/requirements.txt', 'demo/seed/grant.py'],
  gitea: ['demo/gitea'],
};

function run(command, args, cwd = root) {
  const r = spawnSync(command, args, { cwd, encoding: 'utf8', shell: false });
  if (r.error || r.status !== 0) throw new Error(`${command} ${args.join(' ')}: ${r.error?.message ?? r.stderr.trim()}`);
  return r.stdout.trim();
}

/** The newest change among `paths`, in seconds: the last commit, or a file edited since. 0 when there is none. */
export function newestChange(paths) {
  const existing = paths.filter(p => fs.existsSync(path.join(root, p)));
  const committed = Number(run('git', ['log', '-1', '--format=%ct', '--', ...existing]) || 0);
  const edited = run('git', ['status', '--porcelain', '--', ...existing])
    .split('\n')
    .filter(Boolean)
    .map(line => line.slice(3).trim())
    .map(file => {
      try {
        return Math.floor(fs.statSync(path.join(root, file)).mtimeMs / 1000);
      } catch {
        return 0;
      }
    });
  return Math.max(committed, ...edited);
}

function stackServices() {
  const found = new Map();
  for (const dir of STACKS) {
    const out = run('docker', ['compose', 'ps', '--format', 'json'], dir);
    for (const line of out.split('\n').filter(Boolean)) {
      const c = JSON.parse(line);
      found.set(`${c.Project ?? dir}/${c.Service}`, { service: c.Service, image: c.Image });
    }
  }
  return [...found.values()];
}

const behind = [];
let services;
try {
  services = stackServices();
} catch (error) {
  console.error(`could not list the stack's services: ${error.message}`);
  process.exit(2);
}
if (services.length === 0) {
  console.log('no service of the stack is up: nothing to compare');
  process.exit(0);
}

for (const { service, image } of services) {
  const sources = SOURCES[service];
  if (!sources) continue; // an image pulled from a registry (redis, mongo, ...) has no sources here
  const built = Math.floor(Date.parse(run('docker', ['image', 'inspect', '-f', '{{.Created}}', image])) / 1000);
  const changed = newestChange(sources);
  if (changed > built) {
    const fmt = s => new Date(s * 1000).toISOString().slice(0, 16).replace('T', ' ');
    behind.push(`  ${service}: image ${image} built ${fmt(built)}, its sources changed ${fmt(changed)}`);
  }
}

if (behind.length === 0) {
  console.log('every image of the stack is at least as new as its sources');
  process.exit(0);
}
console.error('these images are older than the code in this checkout, so a page can show less than the code says:');
console.error(behind.join('\n'));
console.error('\nRebuild them: cd demo (or e2e, whichever started the stack) && docker compose build <service>.');
console.error('If image builds cannot reach the internet on this machine, run the stack with the sources mounted over the images');
console.error('(ui/e2e/README.md, "Run the stack on a machine that cannot build the images") and set VT_ALLOW_STALE_IMAGES=1.');
process.exit(1);
