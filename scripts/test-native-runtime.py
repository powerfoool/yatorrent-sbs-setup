"""SBS descriptor integration with the actual installed native Electron validator."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install
import releases
import runtime_contract

APP = Path(os.environ.get('YATORRENT_TEST_APP', '/Applications/YaTorrent.app'))
NODE = os.environ.get('SBS_TEST_NODE') or shutil.which('node')


@unittest.skipUnless(NODE and (APP / 'Contents/Resources/app.asar').exists(), 'Installed native app and Node required for vendor integration')
class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sbs-native-contract-')
        self.root = Path(self.temp.name).resolve()
        self.user = self.root / 'user'
        self.auth = self.root / 'auth'
        self.node = self.root / 'node/bin/node'
        self.node.parent.mkdir(parents=True)
        try:
            os.link(Path(NODE).resolve(), self.node)
        except OSError:
            shutil.copy2(NODE, self.node)
        for name in ['lib', 'bin', 'auth/rules', 'user', 'codex/node_modules/@openai/codex/bin']:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for name in ['codex-launcher.mjs', 'sbs-auth-guard.mjs', 'sbs-check-contract.mjs', 'sbs-task-instructions.mjs', 'sbs-url-budget.mjs', 'sbs-chromium.mjs', 'sbs-read-prompt.mjs']:
            shutil.copyfile(install.ASSETS / name, self.root / 'lib' / name)
        self.js = self.root / 'codex/node_modules/@openai/codex/bin/codex.js'
        self.js.write_text("if(process.argv[2]==='--version') console.log('codex-cli 0.162.1'); else {console.error('fixture does not execute Codex'); process.exitCode=1;}\n")
        self.wrapper(self.root / 'bin/codex-real', self.js)
        self.wrapper(self.root / 'bin/codex', self.root / 'lib/codex-launcher.mjs')
        self.wrapper(self.root / 'bin/browser', self.root / 'lib/sbs-chromium.mjs')
        for role in ['foreman', 'scout']:
            self.wrapper(self.root / 'bin' / (role + '-reader'), self.root / 'lib/sbs-read-prompt.mjs', role)
        install.write_json(self.root / 'lib/install-config.json', {'real_codex': str(self.root / 'bin/codex-real'), 'auth_home': str(self.auth),
             'rule_dir': str(self.auth / 'rules'), 'node_bin': str(self.node), 'private_node': str(self.node), 'scout_threads': 10}, self.root)
        install.write_json(self.root / 'lib/runtime-config.json', {'runtime': str(self.root / 'browser-runtime'), 'browsers_path': str(self.root / 'browsers')}, self.root)
        install.write_owned(self.auth / 'rules/yatorrent-sbs-chromium.rules', install.rule_text(self.node, self.root / 'lib/sbs-chromium.mjs'), self.root)
        runtime_contract.configure(self.root, self.user)

    def tearDown(self):
        self.temp.cleanup()

    def wrapper(self, target, script, role=None):
        install.write_owned(target, install.shell_wrapper(self.node, script, role), self.root, 0o755)

    def descriptor(self, env=None):
        raw = subprocess.check_output([self.root / 'bin/codex', '--sbs-print-config'], env=env, text=True)
        return json.loads(raw)

    def test_real_r6_descriptor_accepted_by_exact_vendor_validator(self):
        value = runtime_contract.inspect(self.root, APP, self.user)
        self.assertTrue(value['ok'])
        self.assertEqual(value['descriptor']['real_codex']['version'], 'codex-cli 0.162.1')
        self.assertEqual(set(value['descriptor']), {'schema_version','contract','launcher_version','real_codex','config_fingerprint','total_agent_slots','sandbox','network_access','entry_points'})
        self.assertEqual(value['runtime_config_hash'], runtime_contract.fingerprint(runtime_contract.expected(self.root)))

    def test_descriptor_is_independent_of_auth_contents_and_codex_home_environment(self):
        (self.auth / 'auth.json').write_text('SYNTHETIC_AUTH_FIRST')
        a = self.descriptor()
        (self.auth / 'auth.json').write_text('SYNTHETIC_AUTH_SECOND')
        b = self.descriptor(dict(os.environ, CODEX_HOME=str(self.root / 'unrelated-home')))
        self.assertEqual(a, b)
        self.assertNotIn('SYNTHETIC_AUTH', json.dumps(b))

    def test_fingerprint_changes_for_effective_dependency_change(self):
        before = self.descriptor()['config_fingerprint']
        reader = self.root / 'bin/scout-reader'
        reader.write_text(reader.read_text() + '# fixture update\n')
        self.assertNotEqual(before, self.descriptor()['config_fingerprint'])

    def test_wrong_actual_codex_version_is_rejected(self):
        self.js.write_text("console.log('codex-cli 0.160.1');\n")
        with self.assertRaises(install.SetupError):
            runtime_contract.inspect(self.root, APP, self.user)

    def test_missing_entrypoint_and_extra_descriptor_field_are_rejected(self):
        file = self.root / 'bin/scout-reader'
        file.unlink()
        with self.assertRaises(install.SetupError): runtime_contract.inspect(self.root, APP, self.user)
        self.wrapper(file, self.root / 'lib/sbs-read-prompt.mjs', 'scout')
        launcher = self.root / 'lib/codex-launcher.mjs'
        launcher.write_text(launcher.read_text().replace("return {schema_version: 1, contract:", "return {fixture_extra: true, schema_version: 1, contract:"))
        with self.assertRaises(install.SetupError): runtime_contract.inspect(self.root, APP, self.user)

    def test_fixed_reader_wrappers_read_correct_roles_and_reject_extra_path(self):
        prompts = self.root / 'prompts'; prompts.mkdir()
        for role in ['foreman', 'scout']:
            (prompts / (role + '.md')).write_text('ROLE_' + role)
            result = subprocess.run([self.root / 'bin' / (role + '-reader')], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, 'ROLE_' + role)
            invalid = subprocess.run([self.root / 'bin' / (role + '-reader'), '/arbitrary/path'], cwd=self.root, capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)

    def test_sidecar_survives_real_profile_import_and_controls_generated_config(self):
        before = (self.user / 'executor-runtime.json').read_bytes()
        module = self.root / 'vendor-profile.cjs'
        module.write_bytes(releases.extract_module(APP, 'lib/profile.cjs'))
        code = "const fs=require('node:fs');const m=require(process.argv[1]);const p=m.createProfileStore({directory:process.argv[2],safeStorage:{isEncryptionAvailable:()=>true,encryptString:x=>Buffer.from(x),decryptString:x=>x.toString()}});p.save({version:1,broker:{url:'https://broker.example',protocol_version:2,payload_version:2},executors:{codex:{model:'fixture-model'}}});const runtime=JSON.parse(fs.readFileSync(process.argv[3]));const r=p.buildRuntime({stateDir:process.argv[2],workerId:'fixture-worker',codexBinary:runtime.codex.binary,codexHome:process.argv[2],settings:{consume_codex:true},executorRuntime:{...runtime.codex,config_hash:'fixture'}});console.log(JSON.stringify({timeout:r.config.executors.codex.timeout_seconds,binary:r.config.executors.codex.binary,contract:r.config.executors.codex.required_contract}));"
        result = json.loads(subprocess.check_output([self.node, '-e', code, module, self.user, self.user / 'executor-runtime.json'], text=True))
        self.assertEqual(result, {'timeout':1800,'binary':str(self.root / 'bin/codex'),'contract':'sbs-codex-v1'})
        self.assertEqual(before, (self.user / 'executor-runtime.json').read_bytes())

    def test_native_validator_reads_application_without_modifying_it(self):
        resources = [APP / 'Contents/Resources/app.asar', APP / 'Contents/Resources/worker-runtime/aim2-worker']
        before = [install.digest(p) for p in resources]
        runtime_contract.inspect(self.root, APP, self.user)
        self.assertEqual(before, [install.digest(p) for p in resources])


if __name__ == '__main__':
    unittest.main(verbosity=2)
