#!/usr/bin/env python3
"""Synthetic protocol fixtures; never starts Codex or accesses credentials."""
import json
import argparse
import contextlib
import io
import tempfile
from unittest.mock import patch
from pathlib import Path
import unittest
import runtime_smoke as runtime
import verify
import install

WORKSPACE = Path('/safe/root/runtime-smoke/job-fixture')
COMMANDS = {'browser': '/safe/root/node/bin/node /safe/root/lib/sbs-chromium.mjs --check',
            'foreman': '/safe/root/node/bin/node /safe/root/lib/sbs-read-prompt.mjs foreman',
            'scout': '/safe/root/node/bin/node /safe/root/lib/sbs-read-prompt.mjs scout'}
MARKERS = {'foreman': 'SBS_RANDOM_FOREMAN_1345', 'scout': 'SBS_RANDOM_SCOUT_6789'}
VERSIONS = {'node': 'v24.21.0', 'playwright': '1.64.0', 'chromium': '146.0.7777.1'}
BROWSER = {'ok': True, **VERSIONS, 'readback': 'SBS_CHROMIUM_ALLOW_OK', 'chromium_sandbox': True, 'tls_verification': True}


def fixture(code_mode=True):
    events = [{'type': 'turn_context', 'payload': {'cwd': str(WORKSPACE), 'approval_policy': 'never', 'sandbox_policy': {'type': 'workspace-write', 'network_access': True}}},
              {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'There are 11 available concurrency slots, meaning up to 11 agents.'}]}}]
    for role, cmd in COMMANDS.items():
        call_id = 'call_' + role
        args = {'cmd': cmd, 'login': False, 'yield_time_ms': 30000, 'max_output_tokens': 4000}
        call = {'type': 'custom_tool_call' if code_mode else 'function_call', 'name': 'exec' if code_mode else 'exec_command', 'call_id': call_id}
        call['input' if code_mode else 'arguments'] = 'text(await tools.exec_command(' + json.dumps(args) + '));' if code_mode else json.dumps(args)
        stdout = json.dumps(BROWSER) + '\n' if role == 'browser' else MARKERS[role] + '\n'
        output = [{'type': 'input_text', 'text': 'Script completed\nOutput:\n'}, {'type': 'input_text', 'text': json.dumps({'exit_code': 0, 'output': stdout})}]
        events.extend([{'type': 'response_item', 'payload': call}, {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output' if code_mode else 'function_call_output', 'call_id': call_id, 'output': output}}])
    return events


class SmokeParser(unittest.TestCase):
    def parse(self, events):
        return runtime.parse_rollout(events, COMMANDS, MARKERS, VERSIONS, WORKSPACE)

    def fails(self, events):
        with self.assertRaises((runtime.EvidenceError, ValueError)):
            self.parse(events)

    def test_code_mode_and_direct_function_calls(self):
        for mode in (True, False):
            with self.subTest(mode=mode):
                result = self.parse(fixture(mode))
                self.assertTrue(result['ok'])
                self.assertEqual(set(result['tools']), {'browser', 'foreman', 'scout'})

    def test_managed_policy_reported_without_mutation(self):
        events = fixture()
        events[0]['payload']['approval_policy'] = 'untrusted'
        result = self.parse(events)
        self.assertEqual(result['actual_approval_policies'], ['untrusted'])
        self.assertTrue(result['managed_approval_override_observed'])

    def test_fake_codex_exit_zero_with_browser_exit_one(self):
        events = fixture()
        events[3]['payload']['output'][-1]['text'] = json.dumps({'exit_code': 1, 'output': json.dumps({'ok': False})})
        events.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"completed":true}'}]}})
        self.fails(events)

    def test_browser_semantic_flags_and_versions(self):
        for key, bad in [('ok', False), ('tls_verification', False), ('chromium_sandbox', False), ('node', 'v26.0.0'), ('playwright', '1.56.1'), ('chromium', 'wrong'), ('readback', 'fake')]:
            with self.subTest(key=key):
                events = fixture()
                value = dict(BROWSER, **{key: bad})
                events[3]['payload']['output'][-1]['text'] = json.dumps({'exit_code': 0, 'output': json.dumps(value)})
                self.fails(events)

    def test_missing_malformed_and_incomplete_execution(self):
        for output in ['', 'not JSON', json.dumps({'output': json.dumps(BROWSER)}), json.dumps({'session_id': 2, 'output': json.dumps(BROWSER)})]:
            with self.subTest(output=output):
                events = fixture()
                events[3]['payload']['output'][-1]['text'] = output
                self.fails(events)

    def test_wrong_markers_even_if_final_claims_success(self):
        events = fixture()
        events[5]['payload']['output'][-1]['text'] = json.dumps({'exit_code': 0, 'output': MARKERS['scout']})
        self.fails(events)

    def test_fake_final_without_tool_calls(self):
        events = fixture()[:2]
        events.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': json.dumps(BROWSER)}]}})
        self.fails(events)

    def test_developer_slots_only_and_wrong_count(self):
        events = fixture()
        events[1]['payload']['role'] = 'user'
        self.fails(events)
        events = fixture()
        events[1]['payload']['content'][0]['text'] = 'There are 4 available concurrency slots'
        self.fails(events)

    def test_actual_policy_rejects_wrong_sandbox_and_network(self):
        for sandbox in [{'type': 'danger-full-access', 'network_access': True}, {'type': 'workspace-write', 'network_access': False}]:
            events = fixture()
            events[0]['payload']['sandbox_policy'] = sandbox
            self.fails(events)
        events = fixture()
        events[0]['payload']['approval_policy'] = 'on-request'
        self.fails(events)

    def test_wrong_scope_extra_command_and_call_id(self):
        events = fixture()
        events[0]['payload']['cwd'] = '/another/workspace'
        self.fails(events)
        events = fixture()
        events[2]['payload']['input'] = 'text(await tools.exec_command({cmd:"cat /private/file"}));'
        self.fails(events)
        events = fixture()
        events[3]['payload']['call_id'] = 'not-the-call'
        self.fails(events)

    def test_fake_code_mode_output_is_not_execution(self):
        events = fixture()
        events[2]['payload']['input'] = 'text({exit_code:0,output:"success"});'
        self.fails(events)

    def test_unquoted_object_keys_supported(self):
        events = fixture()
        events[2]['payload']['input'] = 'text(await tools.exec_command({cmd:' + json.dumps(COMMANDS['browser']) + ',login:false,yield_time_ms:30000}));'
        self.assertTrue(self.parse(events)['ok'])

    def test_direct_native_exec_output_format(self):
        events = fixture(False)
        for i, role in [(3, 'browser'), (5, 'foreman'), (7, 'scout')]:
            text = json.dumps(BROWSER) if role == 'browser' else MARKERS[role]
            events[i]['payload']['output'] = 'Chunk ID: fixture\nWall time: 0.3 seconds\nProcess exited with code 0\nFinal output:\n' + text
        self.assertTrue(self.parse(events)['ok'])

    def test_malformed_rollout_records(self):
        for value in [[], {'type': 'turn_context', 'payload': 'wrong'}]:
            self.fails(fixture() + [value])

    def test_installed_browser_metadata_must_match_pin(self):
        for metadata in [[], [{'name': 'chromium', 'browserVersion': '146.0.7777.1'}],
                         [{'name': 'chromium', 'browserVersion': install.CHROMIUM_VERSION}] * 2]:
            with self.subTest(metadata=metadata), patch.object(install, 'read_json', return_value={'browsers': metadata}):
                with self.assertRaises(install.SetupError):
                    runtime._expected_versions(Path('/safe/root'))
        with patch.object(install, 'read_json', return_value={'browsers': [{'name': 'chromium', 'browserVersion': install.CHROMIUM_VERSION}]}):
            self.assertEqual(runtime._expected_versions(Path('/safe/root')), {'node': 'v' + install.NODE_VERSION, 'playwright': install.PLAYWRIGHT_VERSION, 'chromium': install.CHROMIUM_VERSION})

    def test_no_shell_quoting_or_chaining(self):
        events = fixture()
        events[2]['payload']['input'] = 'text(await tools.exec_command(' + json.dumps({'cmd': COMMANDS['browser'] + '; echo ok', 'login': False}) + '));'
        self.fails(events)


class FullVerification(unittest.TestCase):
    def fixture(self, base):
        base = base.resolve()
        root = base / 'root'
        app = base / 'app'
        user = base / 'user'
        auth = base / 'auth'
        node, browser, reader = root / 'node/bin/node', root / 'lib/sbs-chromium.mjs', root / 'lib/sbs-read-prompt.mjs'
        commands = install.launcher_commands(root)
        setup = {**commands, 'private_node': str(node), 'real_codex': str(root / 'bin/codex-real'), 'auth_home': str(auth),
                 'real_worker': str(app / 'Contents/Resources/worker-runtime/aim2-worker'), 'real_worker_sha256': 'digest', 'scout_threads': 10, 'worker_timeout_seconds': 1800,
                 'worker_concurrency': 4, 'codex_wrapper': str(root / 'bin/codex')}
        marker = {**commands, 'install_mode': 'native', 'release': {'version':'0.3.9'}, 'state': 'prepared', 'app': str(app), 'user_data': str(user), 'real_worker_sha256': 'digest', 'profile_sha256': 'digest'}
        source = {'version': 1, 'broker': {'url': 'https://broker.example', 'allow_http': False}, 'executors': {'codex': {'model': 'profile-model'}}}
        files = {root / 'installation.json': marker, root / 'lib/install-config.json': setup,
                 root / 'profile-import.json': {'source_sha256': 'digest', 'encrypted_file_sha256': 'digest'},
                 root / 'lib/runtime-config.json': {'runtime': str(root / 'browser-runtime'), 'browsers_path': str(root / 'browsers')},
                 root / 'codex/node_modules/@openai/codex/package.json': {'version': install.CODEX_VERSION},
                 root / 'browser-runtime/node_modules/playwright/package.json': {'version': install.PLAYWRIGHT_VERSION},
                 root / 'profiles/databroker.aim2': source,
                 user / 'profile.json': {'version': 1, 'profile': source, 'credentials_encrypted': 'test-only-encrypted-placeholder'},
                 user / 'desktop.json': {'autoStart': True, 'autoUpdate': False, 'resumeAfterUpdate': False, 'settings': {'max_browsers': 4, 'consume_codex': True}}}
        for file, value in files.items():
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(value))
            file.chmod(0o600)
        for file, text in [(root / 'bin/codex-real', install.shell_wrapper(node, root / 'codex/node_modules/@openai/codex/bin/codex.js')),
                           (root / 'bin/browser', install.shell_wrapper(node, browser)),
                           (root / 'bin/foreman-reader', install.shell_wrapper(node, reader, 'foreman')),
                           (root / 'bin/scout-reader', install.shell_wrapper(node, reader, 'scout')),
                           (root / 'lib/codex-launcher.mjs', (install.ASSETS / 'codex-launcher.mjs').read_text()),
                           (root / 'lib/sbs-auth-guard.mjs', (install.ASSETS / 'sbs-auth-guard.mjs').read_text()),
                           (root / 'lib/sbs-task-instructions.mjs', (install.ASSETS / 'sbs-task-instructions.mjs').read_text()),
                           (root / 'lib/sbs-url-budget.mjs', (install.ASSETS / 'sbs-url-budget.mjs').read_text()),
                           (reader, (install.ASSETS / reader.name).read_text()),
                           (auth / 'rules/yatorrent-sbs-chromium.rules', install.rule_text(node, browser)),
                           (app / 'Contents/Resources/worker-runtime/aim2-worker', 'pristine-native-fixture'),
                           (root / 'bin/codex', install.shell_wrapper(node, root / 'lib/codex-launcher.mjs'))]:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
            if file.parent.name == 'bin': file.chmod(0o700)
        args = argparse.Namespace(root=root, user_data=None, profile=None, wait_import=0,
                                  skip_chromium=False, skip_login=False, runtime_only=False, runtime_timeout=240)
        return args, (node, browser, reader)

    def run_verification(self, args, paths, runtime_result, worker_result=None):
        stream = io.StringIO()
        def direct_run(command, **kwargs):
            return 'v' + install.NODE_VERSION if command[-1] == '--version' else json.dumps(BROWSER)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(install, 'entry_points', return_value=paths))
            stack.enter_context(patch.object(install, 'validate_existing_app'))
            stack.enter_context(patch.object(verify.releases, 'validate_source'))
            stack.enter_context(patch.object(verify.runtime_contract, 'inspect', return_value={'ok':True}))
            stack.enter_context(patch.object(install, 'digest', return_value='digest'))
            stack.enter_context(patch.object(install, 'check_rule'))
            stack.enter_context(patch.object(install, 'run', side_effect=direct_run))
            stack.enter_context(patch.object(verify, 'authentication_readback', return_value={'codex_auth': 'logged_in', 'file_auth_present': True}))
            stack.enter_context(patch.object(verify, 'worker_readback', return_value=worker_result if worker_result is not None else {'worker_ready': True}))
            stack.enter_context(patch.object(verify.instruction_smoke, 'run_instruction_smoke', return_value={'ok': True}))
            smoke = stack.enter_context(patch.object(runtime, 'run_runtime_smoke', return_value=runtime_result))
            stack.enter_context(contextlib.redirect_stdout(stream))
            code = verify.verify(args)
        return code, json.loads(stream.getvalue()), smoke.call_count

    def test_fullverify_rejects_autostart_disabled_before_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            user = Path(install.read_json(args.root / 'installation.json')['user_data'])
            prefs = install.read_json(user / 'desktop.json')
            prefs['autoStart'] = False
            (user / 'desktop.json').write_text(json.dumps(prefs))
            with self.assertRaises(install.SetupError):
                self.run_verification(args, paths, {'ok': True})

    def test_fullverify_rejects_stale_import_receipt_before_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            (args.root / 'profile-import.json').write_text(json.dumps({'source_sha256': 'stale', 'encrypted_file_sha256': 'digest'}))
            with self.assertRaises(install.SetupError):
                self.run_verification(args, paths, {'ok': True})

    def test_fullverify_fake_codex_zero_browser_one_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            code, result, calls = self.run_verification(args, paths, {'ok': False, 'codex_exit_code': 0, 'failure': 'execution_nonzero_browser'})
            self.assertNotEqual(code, 0)
            self.assertEqual(result['status'], 'не готов')
            self.assertFalse(result['delivery_ready'])
            self.assertEqual(calls, 1)

    def test_fullverify_skip_never_ready_and_no_real_runtime(self):
        for flag in ['skip_login', 'skip_chromium']:
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                args, paths = self.fixture(Path(directory))
                setattr(args, flag, True)
                code, result, calls = self.run_verification(args, paths, {'ok': True})
                self.assertNotEqual(code, 0)
                self.assertFalse(result['delivery_ready'])
                self.assertTrue(result['diagnostic_only'])
                self.assertEqual(calls, 0)

    def test_fullverify_requires_actual_runtime_and_reports_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            code, result, calls = self.run_verification(args, paths, {'ok': True})
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'готов')
            self.assertTrue(result['delivery_ready'])
            self.assertEqual(calls, 1)
            for key, expected in install.launcher_commands(args.root).items():
                self.assertEqual(result[key], expected)

    def test_fullverify_worker_mismatch_cannot_be_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            code, result, calls = self.run_verification(args, paths, {'ok': True}, {'worker_ready': False, 'failure': 'timeout 180'})
            self.assertNotEqual(code, 0)
            self.assertFalse(result['delivery_ready'])
            self.assertEqual(result['status'], 'не готов')

    def test_prelaunch_success_is_not_worker_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            args.pre_launch = True
            code, result, calls = self.run_verification(args, paths, {'ok': True})
            self.assertEqual(code, 0)
            self.assertTrue(result['infrastructure_ready'])
            self.assertFalse(result['delivery_ready'])
            self.assertFalse(result['worker_ready'])
            self.assertEqual(result['status'], 'подготовлен')

    def test_runtime_only_success_stays_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            args, paths = self.fixture(Path(directory))
            args.runtime_only = True
            code, result, calls = self.run_verification(args, paths, {'ok': True})
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'не готов')
            self.assertFalse(result['delivery_ready'])
            self.assertTrue(result['diagnostic_only'])
            self.assertEqual(calls, 1)

    def test_main_exception_is_structured_not_ready(self):
        with patch.object(verify, 'verify', side_effect=install.SetupError('test validation failure')), contextlib.redirect_stdout(io.StringIO()) as stream:
            code = verify.main([])
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())['status'], 'не готов')


if __name__ == '__main__':
    unittest.main()
