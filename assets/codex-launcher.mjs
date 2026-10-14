#!/usr/bin/env node
// Isolate worker session state without copying or inspecting credentials.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {spawn, spawnSync} from 'node:child_process';
import crypto from 'node:crypto';
import {parseTaskCarrier, applyTaskInstructions} from './sbs-task-instructions.mjs';

const assetDir = path.dirname(fileURLToPath(import.meta.url));
const valueFlags = new Set(['--config', '-c', '--sandbox', '-s', '--cd', '-C', '--model', '-m', '--output-schema', '--output-last-message', '-o', '--image', '-i', '--profile', '-p', '--color', '--local-provider', '--add-dir', '--thread-source', '--cyber-access-program', '--enable', '--disable', '--ask-for-approval', '-a']);
const forbiddenFlags = new Set(['--dangerously-bypass-approvals-and-sandbox', '--yolo', '--dangerously-bypass-hook-trust', '--ignore-rules', '--full-auto', '--worktree']);
const protectedKeys = new Set(['agents', 'developer_instructions', 'model_instructions_file', 'instructions', 'project_doc_max_bytes', 'sqlite_home', 'codex_home', 'auth_home', 'cli_home', 'sandbox_mode', 'sandbox_permissions', 'permissions', 'permission_profile', 'cli_auth_credentials_store', 'execpolicy', 'rules', 'state', 'state_dir', 'session_dir', 'sessions_dir', 'rollout_dir']);

function assertOverride(value) {
  if (typeof value !== 'string' || !value.includes('=')) throw new Error('Config overrides require key=value');
  // Quoted TOML path segments cannot disguise a protected key.
  const key = value.slice(0, value.indexOf('=')).trim().replace(/["'\s]/g, '');
  const root = key.split('.')[0];
  if (root === 'sandbox_workspace_write') {
    const setting = value.slice(value.indexOf('=') + 1).trim();
    if (key !== 'sandbox_workspace_write.network_access' || !['true', 'false'].includes(setting)) throw new Error('Only the network_access boolean may be set within sandbox_workspace_write');
  }
  if (protectedKeys.has(root) || /^(features\.(multi_agent|collaboration_modes)|profiles)(\.|$)/.test(key)) {
    throw new Error(`Launcher-managed configuration cannot be overridden: ${key}`);
  }
}

/** Pure argument transform: does not read credentials or launch Codex. */
export function transformArgs(input, {sessionHome, cwd = process.cwd(), scoutThreads = 10} = {}) {
  if (!Array.isArray(input) || input.some(a => typeof a !== 'string')) throw new Error('Expected string arguments');
  if (scoutThreads !== 10) throw new Error('This setup requires ten scout threads');
  let commandIndex = -1;
  for (let i = 0; i < input.length; i++) {
    const a = input[i], flag = a.split('=')[0];
    if (a === '--') break;
    if (valueFlags.has(flag) && !a.includes('=')) { i++; continue; }
    if (/^-[csmCopai].+/.test(a)) continue;
    if (!a.startsWith('-')) { commandIndex = i; break; }
  }
  if (commandIndex < 0 || !['exec', 'e'].includes(input[commandIndex])) return {isExec: false, args: [...input], cwd};
  if (!sessionHome || !path.isAbsolute(sessionHome)) throw new Error('An absolute sessionHome is required');
  const args = [];
  let outputCommandIndex;
  let sandboxSeen = false, effectiveCwd = cwd, positional = false;
  for (let i = 0; i < input.length; i++) {
    const a = input[i];
    if (i === commandIndex) { outputCommandIndex = args.length; args.push(a); continue; }
    if (positional || a === '--') { args.push(a); positional = true; continue; }
    const flag = a.split('=')[0];
    if (forbiddenFlags.has(flag)) throw new Error(`Unsupported worker option: ${flag}`);
    if (flag === '--ephemeral') continue;
    let opt = flag, value, consumed = false;
    if (valueFlags.has(flag)) {
      value = a.includes('=') ? a.slice(a.indexOf('=') + 1) : input[++i];
      consumed = !a.includes('=');
      if (value === undefined) throw new Error(`Missing value for ${flag}`);
    } else if (/^-[csCmopai].+/.test(a)) {
      opt = a.slice(0, 2); value = a.slice(2).replace(/^=/, '');
    }
    if (opt === '-c' || opt === '--config') assertOverride(value);
    if (opt === '-s' || opt === '--sandbox') {
      if (value !== 'workspace-write') throw new Error('Worker sandbox must be explicit workspace-write');
      sandboxSeen = true;
    }
    if (opt === '-p' || opt === '--profile') throw new Error('Profiles may override isolated worker state; use explicit worker options');
    if ((opt === '--enable' || opt === '--disable') && ['multi_agent', 'collaboration_modes'].includes(value)) throw new Error('Agent state is managed by the launcher');
    if (opt === '-C' || opt === '--cd') effectiveCwd = path.resolve(cwd, value);
    args.push(a);
    if (consumed) args.push(value);
  }
  if (!sandboxSeen) throw new Error('Workers require --sandbox workspace-write');
  // Exec owns its overrides. Top-level -c values are discarded by exec's
  // --ignore-user-config path in pinned Codex 0.162.1. Keep these after exec/e,
  // before any prompt or '--', so the worker's stdin prompt remains untouched.
  args.splice(outputCommandIndex + 1, 0,
    '-c', 'agents.enabled=true',
    '-c', 'agents.max_concurrent_threads_per_session=10',
    '-c', 'project_doc_max_bytes=0',
    '-c', `sqlite_home=${JSON.stringify(sessionHome)}`,
    '-c', 'cli_auth_credentials_store="file"');
  return {isExec: true, args, cwd, stateCwd: effectiveCwd};
}

export function loadInstallConfig(configPath = path.join(assetDir, 'install-config.json')) {
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  const config = {};
  for (const key of ['real_codex', 'auth_home', 'rule_dir', 'node_bin']) {
    if (typeof raw[key] !== 'string' || !path.isAbsolute(raw[key])) throw new Error(`install-config.json requires absolute ${key}`);
    config[key] = raw[key];
  }
  if (raw.scout_threads !== 10) throw new Error('install-config.json requires scout_threads=10');
  config.scout_threads = 10;
  return config;
}

export function printConfig(config) {
  const root = path.dirname(assetDir);
  const entryPoints = {browser: path.join(root, 'bin/browser'), foreman_reader: path.join(root, 'bin/foreman-reader'), scout_reader: path.join(root, 'bin/scout-reader')};
  const files = [config.node_bin, config.real_codex, path.join(assetDir, 'codex-launcher.mjs'),
    path.join(assetDir, 'sbs-auth-guard.mjs'), path.join(assetDir, 'sbs-task-instructions.mjs'), path.join(assetDir, 'sbs-url-budget.mjs'), path.join(assetDir, 'sbs-chromium.mjs'), path.join(assetDir, 'sbs-read-prompt.mjs'),
    path.join(assetDir, 'runtime-config.json'), path.join(assetDir, 'install-config.json'), path.join(root, 'bin/codex'),
    path.join(root, 'codex/node_modules/@openai/codex/bin/codex.js'),
    ...Object.values(entryPoints), path.join(config.rule_dir, 'yatorrent-sbs-chromium.rules')];
  const hashes = files.map(file => {
    const real = fs.realpathSync(file), st = fs.statSync(real);
    if (!st.isFile() || (st.mode & 0o022) || (st.uid !== 0 && st.uid !== process.getuid())) throw new Error('Descriptor dependency is unavailable or untrusted');
    return [file, crypto.createHash('sha256').update(fs.readFileSync(real)).digest('hex')];
  });
  for (const file of Object.values(entryPoints)) fs.accessSync(file, fs.constants.X_OK);
  const env = {PATH: path.dirname(config.node_bin), HOME: process.env.HOME || os.homedir()};
  const result = spawnSync(config.real_codex, ['--version'], {env, encoding: 'utf8', timeout: 5000, maxBuffer: 16384});
  const version = result.status === 0 && /^codex-cli (0\.162\.1)\s*$/.exec(result.stdout || '');
  if (!version) throw new Error('Actual Codex version differs from the pinned CLI');
  return {schema_version: 1, contract: 'sbs-codex-v1', launcher_version: '7.0.0',
    real_codex: {binary: fs.realpathSync(config.real_codex), version: 'codex-cli ' + version[1]},
    config_fingerprint: crypto.createHash('sha256').update(JSON.stringify(hashes)).digest('hex'),
    total_agent_slots: 11, sandbox: 'workspace-write', network_access: true, entry_points: entryPoints};
}

export async function runLauncher(input, config, {cwd = process.cwd(), env = process.env, stdin = process.stdin} = {}) {
  const diagnostic = input.includes('--sbs-diagnostic');
  input = input.filter(x => x !== '--sbs-diagnostic');
  // First validate the invocation; invalid arguments do not allocate state.
  const preview = transformArgs(input, {sessionHome: path.join(cwd, '.sbs-cli-preview'), cwd});
  if (preview.isExec && !diagnostic && input.at(-1) !== '-') throw new Error('SBS jobs require coordinator input_data on stdin');
  let stdinText = null, task = null;
  if (preview.isExec && input.at(-1) === '-') {
    const chunks = []; let bytes = 0;
    for await (const chunk of stdin) {
      bytes += Buffer.byteLength(chunk);
      if (bytes > 1024 * 1024) throw new Error('Worker stdin exceeds the launcher budget');
      chunks.push(Buffer.from(chunk));
    }
    stdinText = new TextDecoder('utf-8', {fatal: true}).decode(Buffer.concat(chunks));
    task = parseTaskCarrier(stdinText);
    if (!task && !diagnostic) throw new Error('Coordinator instruction carrier required; worker defaults are forbidden');
    if (task && diagnostic) throw new Error('Assessment tasks cannot use the diagnostic bypass');
  }
  const childEnv = {...env, PATH: `${path.dirname(config.node_bin)}${path.delimiter}${env.PATH || ''}`};
  let home, authLink, guard;
  if (preview.isExec) {
    const authSource = path.join(config.auth_home, 'auth.json');
    if (!fs.existsSync(authSource)) throw new Error('File authentication is required for isolated workers. Use the real Codex CLI with cli_auth_credentials_store="file" to log in; the launcher will not read or copy keyring credentials.');
    if (!fs.statSync(config.rule_dir).isDirectory()) throw new Error('User rules directory is unavailable');
    const stateCwd = fs.realpathSync(preview.stateCwd);
    home = fs.mkdtempSync(path.join(stateCwd, '.sbs-cli-'));
    fs.chmodSync(home, 0o700);
    authLink = path.join(home, 'auth.json');
    try {
      fs.symlinkSync(config.rule_dir, path.join(home, 'rules'), 'dir');
      fs.symlinkSync(authSource, authLink, 'file');
    } catch (error) {
      fs.rmSync(authLink, {force: true});
      throw error;
    }
    childEnv.CODEX_HOME = home;
    childEnv.CODEX_SQLITE_HOME = home;
    childEnv.CLI_HOME = home;
  } else {
    childEnv.CODEX_HOME = config.auth_home;
    delete childEnv.CODEX_SQLITE_HOME;
    delete childEnv.CLI_HOME;
  }
  const transformed = preview.isExec ? transformArgs(input, {sessionHome: home, cwd}) : preview;
  try {
    if (task) {
      childEnv.SBS_JOB_HOME = home;
      const prepared = applyTaskInstructions(task, home, {browserCommand: config.node_bin + ' ' + path.join(assetDir, 'sbs-chromium.mjs')});
      const index = transformed.args.findIndex(x => x === 'exec' || x === 'e');
      transformed.args.splice(index + 1, 0, ...prepared.overrides);
      stdinText = prepared.modelInput;
    }
    if (authLink) {
      guard = spawn(config.node_bin, [path.join(assetDir, 'sbs-auth-guard.mjs'), authLink], {
        detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
        env: {PATH: path.dirname(config.node_bin)}
      });
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => {done(); reject(new Error('Auth cleanup guard readiness timed out'));}, 5000);
        const failed = () => {done(); reject(new Error('Auth cleanup guard failed'));};
        const ready = value => {if (value?.ready === true) {done(); resolve();}};
        const done = () => {clearTimeout(timer); guard.off('message', ready); guard.off('error', failed); guard.off('exit', failed);};
        guard.on('message', ready); guard.once('error', failed); guard.once('exit', failed);
      });
      guard.unref();
      guard.channel?.unref();
    }
    return await new Promise((resolve, reject) => {
      const child = spawn(config.real_codex, transformed.args, {cwd, env: childEnv, stdio: [stdinText === null ? 'inherit' : 'pipe', 'inherit', 'inherit']});
      if (stdinText !== null) {
        child.stdin.on('error', () => {});
        child.stdin.end(stdinText);
      }
      const handlers = new Map();
      for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
        const handler = () => { if (!child.killed) child.kill(signal); };
        handlers.set(signal, handler); process.on(signal, handler);
      }
      const cleanup = () => { for (const [signal, handler] of handlers) process.off(signal, handler); };
      child.once('error', error => {cleanup(); reject(error);});
      child.once('exit', (code, signal) => {cleanup(); resolve(code ?? (128 + (os.constants.signals[signal] || 1)));});
    });
  } finally {
    // Normal cleanup; detached IPC guard also covers worker killpg(SIGKILL).
    if (authLink) fs.rmSync(authLink, {force: true});
    if (guard?.connected) guard.disconnect();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const config = loadInstallConfig();
    const input = process.argv.slice(2);
    if (input.length === 1 && input[0] === '--sbs-print-config') console.log(JSON.stringify(printConfig(config), null, 2));
    else process.exitCode = await runLauncher(input, config);
  } catch (error) {
    // Arguments and prompt text are intentionally excluded from diagnostics.
    process.stderr.write(`SBS Codex launcher: ${error.message}\n`);
    process.exitCode = 1;
  }
}
