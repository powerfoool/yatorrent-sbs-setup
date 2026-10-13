#!/usr/bin/env node
// Fixed prompt reader. No arbitrary paths, execution, network or writes.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const names = Object.freeze({foreman: 'foreman.md', scout: 'scout.md'});
const maxBytes = 1024 * 1024;

export function readPrompt(role, cwd = process.cwd()) {
  if (!Object.hasOwn(names, role)) throw new Error('Role must be foreman or scout');
  const root = fs.realpathSync(cwd);
  const dir = path.join(root, 'prompts');
  const dirInfo = fs.lstatSync(dir);
  if (!dirInfo.isDirectory() || dirInfo.isSymbolicLink()) throw new Error('prompts must be a real directory in the job workspace');
  const target = path.join(dir, names[role]);
  if (fs.realpathSync(target) !== target) throw new Error('Prompt symlinks are not permitted');
  const fd = fs.openSync(target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size > maxBytes) throw new Error('Prompt must be a regular UTF-8 file of at most 1 MiB');
    const buffer = Buffer.alloc(maxBytes + 1);
    let count = 0;
    while (count < buffer.length) {
      const read = fs.readSync(fd, buffer, count, buffer.length - count, null);
      if (!read) break;
      count += read;
    }
    if (count > maxBytes) throw new Error('Prompt is larger than 1 MiB');
    return new TextDecoder('utf-8', {fatal: true}).decode(buffer.subarray(0, count));
  } finally {
    fs.closeSync(fd);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 3) throw new Error('Usage: fixed-reader foreman|scout');
    process.stdout.write(readPrompt(process.argv[2]));
  } catch (error) {
    // Errors contain no prompt contents or credential data.
    process.stderr.write('Prompt read failed: ' + error.message + '\n');
    process.exitCode = 1;
  }
}
