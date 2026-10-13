// Reviewed desktop adapter: patch once before starting the preserved worker.
// This adapter never starts broker intake on its own and never prints config/env.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

const assetDir = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
try {
  const setup = JSON.parse(fs.readFileSync(path.join(assetDir, 'install-config.json'), 'utf8'));
  if (!path.isAbsolute(setup.real_worker) || !path.isAbsolute(setup.auth_home) || !path.isAbsolute(setup.codex_wrapper)) throw new Error('Invalid fixed installation paths');
  if (setup.worker_timeout_seconds !== 1800 || !Number.isInteger(setup.worker_concurrency) || setup.worker_concurrency < 1 || setup.worker_concurrency > 16) throw new Error('Unsupported adapter limits');
  const original = fs.lstatSync(setup.real_worker);
  if (!original.isFile() || original.isSymbolicLink()) throw new Error('Preserved worker is unavailable');
  const digest = crypto.createHash('sha256').update(fs.readFileSync(setup.real_worker)).digest('hex');
  if (digest !== setup.real_worker_sha256) throw new Error('Preserved worker changed; re-check adapter after app update');
  if (args[0] === 'desktop' && !args.includes('--help') && !args.includes('-h')) {
    const flags = new Map();
    for (let i = 1; i < args.length; i += 2) {
      if (!['--config', '--state-dir', '--port'].includes(args[i]) || flags.has(args[i]) || args[i + 1] === undefined) throw new Error('Unexpected desktop arguments');
      flags.set(args[i], args[i + 1]);
    }
    if (flags.size !== 3 || !path.isAbsolute(flags.get('--config')) || !path.isAbsolute(flags.get('--state-dir')) || !/^\d+$/.test(flags.get('--port'))) throw new Error('Invalid desktop arguments');
    const configFile = flags.get('--config');
    const stat = fs.lstatSync(configFile);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.uid !== process.getuid() || stat.nlink !== 1 || stat.size > 4 * 1024 * 1024) throw new Error('Unsafe worker config');
    const config = JSON.parse(fs.readFileSync(configFile, 'utf8'));
    if (!config || typeof config !== 'object' || Array.isArray(config) || !config.executors?.codex || typeof config.executors.codex !== 'object' || Array.isArray(config.executors.codex)) throw new Error('Desktop Codex executor is missing');
    config.executors.codex.binary = setup.codex_wrapper;
    config.executors.codex.codex_home = setup.auth_home;
    config.executors.codex.timeout_seconds = 1800;
    config.executors.codex.max_output_bytes = 8 * 1024 * 1024;
    config.concurrency = setup.worker_concurrency;
    const temporary = `${configFile}.sbs-${process.pid}-${crypto.randomBytes(6).toString('hex')}.tmp`;
    let fd;
    try {
      fd = fs.openSync(temporary, 'wx', 0o600);
      fs.writeFileSync(fd, JSON.stringify(config));
      fs.fsyncSync(fd);
      fs.closeSync(fd); fd = undefined;
      fs.renameSync(temporary, configFile);
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
      try { fs.unlinkSync(temporary); } catch (e) { if (e.code !== 'ENOENT') throw e; }
    }
    // Public launch evidence ties the exact config consumed at exec to the PID
    // retained by execve. It never contains config data, environment or tokens.
    const stateDir = flags.get('--state-dir');
    fs.mkdirSync(stateDir, {recursive: true, mode: 0o700});
    const parent = fs.lstatSync(stateDir);
    if (!parent.isDirectory() || parent.isSymbolicLink() || parent.uid !== process.getuid() || (parent.mode & 0o022)) throw new Error('Unsafe launch evidence directory');
    const receipt = path.join(stateDir, 'sbs-worker-launch.json');
    try {
      const old = fs.lstatSync(receipt);
      if (!old.isFile() || old.isSymbolicLink() || old.uid !== process.getuid() || old.nlink !== 1) throw new Error('Unsafe launch receipt');
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
    }
    const receiptTemp = `${receipt}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
    try {
      const evidence = {schema_version: 1, pid: process.pid, started_at: Date.now() / 1000,
        real_worker: setup.real_worker, real_worker_sha256: digest,
        config_file: configFile, state_dir: stateDir,
        config_sha256: crypto.createHash('sha256').update(fs.readFileSync(configFile)).digest('hex')};
      const receiptFd = fs.openSync(receiptTemp, 'wx', 0o600);
      try {fs.writeFileSync(receiptFd, JSON.stringify(evidence)); fs.fsyncSync(receiptFd);} finally {fs.closeSync(receiptFd);}
      fs.renameSync(receiptTemp, receipt);
    } finally {
      try {fs.unlinkSync(receiptTemp);} catch (e) {if (e.code !== 'ENOENT') throw e;}
    }
  }
  // DesktopWorker requires service.json.pid === its spawned PID. execve replaces
  // this process rather than spawning a grandchild, retaining PID, env and fd 0-2.
  if (typeof process.execve !== 'function') throw new Error('Pinned Node execve is required');
  process.execve(setup.real_worker, [setup.real_worker, ...args], process.env);
} catch (_) {
  // Config and environment can contain credentials. Never serialize errors or data.
  process.stderr.write('SBS worker adapter refused startup; check fixed paths, preserved worker digest and desktop config.\n');
  process.exitCode = 1;
}
