// Installer-only inspector; it is not an agent allow-rule entry point.
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
try {
  const args = process.argv.slice(2);
  if (args.length !== 2) throw new Error('Invalid inspection arguments');
  const {inspectRuntimeConfig} = require(args[0]);
  const inspected = await inspectRuntimeConfig(fs.readFileSync(args[1], 'utf8'));
  if (inspected.config.codex.required_contract !== 'sbs-codex-v1' || !inspected.descriptor) throw new Error('SBS strict contract missing');
  console.log(JSON.stringify({ok: true, runtime_config_hash: inspected.hash,
    launcher_identity: inspected.launcher_identity, descriptor: inspected.descriptor}));
} catch {
  process.stderr.write('Native SBS contract inspection failed; private diagnostics withheld\n');
  process.exitCode = 1;
}
