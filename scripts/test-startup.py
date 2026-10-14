#!/usr/bin/env python3
"""Offline startup/release fixtures; no actual app, broker or credentials."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install
import releases
import startup


class ReleaseTests(unittest.TestCase):
    def metadata(self, version='0.3.4'):
        name = f'YaTorrent-{version}-arm64.dmg'
        tag = 'yatorrent-v' + version
        return {'draft': False, 'prerelease': False, 'tag_name': tag, 'assets': [
            {'name': name, 'digest': 'sha256:' + 'a' * 64,
             'browser_download_url': f'https://github.com/RudenkoAD/YaTorrent-releases/releases/download/{tag}/{name}'}]}

    def test_select_latest_and_do_not_silently_install_pinned_older_release(self):
        self.assertEqual(releases.latest(self.metadata())['version'], '0.3.4')
        self.assertEqual(releases.latest(self.metadata('0.3.5'))['version'], '0.3.5')
        # Source-byte contract validation still precedes staging/adapter changes.

    def test_reject_bad_asset_and_nonstable_release(self):
        for change in ('draft', 'prerelease', 'digest', 'url', 'duplicate', 'missing', 'tag'):
            value = self.metadata()
            if change in ('draft', 'prerelease'):
                value[change] = True
            elif change == 'digest':
                value['assets'][0]['digest'] = ''
            elif change == 'url':
                value['assets'][0]['browser_download_url'] = 'https://example.invalid/other.dmg'
            elif change == 'duplicate':
                value['assets'].append(copy.deepcopy(value['assets'][0]))
            elif change == 'missing':
                value['assets'] = []
            else:
                value['tag_name'] = 'untrusted tag'
            with self.subTest(change=change), self.assertRaises(releases.ReleaseError):
                releases.latest(value)

    def test_network_failure_has_no_fallback(self):
        with patch.object(releases, 'urlopen', side_effect=OSError('fixture failure')):
            with self.assertRaises(releases.ReleaseError):
                releases.latest()


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sbs-startup-fixture-')
        self.root = Path(self.temp.name).resolve() / 'root'
        self.root.mkdir(mode=0o700)
        self.user = self.root / 'user'
        self.user.mkdir()
        self.app = self.root / 'YaTorrent.app'
        self.profile = self.root / 'profiles/databroker.aim2'
        self.profile.parent.mkdir()
        self.profile.write_text(json.dumps({'version': 1, 'broker': {'url': 'https://fixture.invalid/'},
                                           'executors': {'codex': {'model': 'fixture-model'}}}))
        self.profile.chmod(0o600)
        marker = {'state': 'prepared', 'app': str(self.app), 'user_data': str(self.user),
                  'profile_sha256': install.digest(self.profile)}
        install.write_json(self.root / 'installation.json', marker, self.root)
        self.setup = {'real_codex': str(self.root / 'codex'), 'auth_home': str(self.root / 'auth'), 'worker_concurrency': 4}
        install.write_json(self.root / 'lib/install-config.json', self.setup, self.root)
        self.saved_id = '00000000-0000-0000-0000-000000000001'
        install.write_json(self.user / 'desktop.json', {'workerId': self.saved_id, 'autoStart': True,
                           'custom': 'preserved', 'settings': {'other': 'preserved'}}, self.root)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def imported(self, url='https://fixture.invalid', confirmed=False):
        install.write_json(self.user / 'profile.json', {'version': 1, 'credentials_encrypted': 'opaque-fixture',
                           'profile': {'version': 1, 'broker': {'url': url, 'allow_http': False},
                                       'executors': {'codex': {'model': 'fixture-model'}}}}, self.root)
        if confirmed:
            install.write_json(self.root / 'profile-import.json', {'source_sha256': install.digest(self.profile),
                               'encrypted_file_sha256': install.digest(self.user / 'profile.json')}, self.root)

    def execute(self, command, **kwargs):
        command = [str(x) for x in command]
        prefs = install.read_json(self.user / 'desktop.json')
        self.calls.append((command, prefs))
        if command[0] == '/usr/bin/open':
            if len(command) == 4:
                self.assertFalse(prefs['autoStart'])
                self.imported()
            else:
                self.assertTrue(prefs['autoStart'])
                self.assertTrue(prefs['settings']['consume_codex'])
        if 'verify.py' in command[1]:
            self.assertTrue(prefs['autoStart'])
            self.assertEqual(self.calls[-1][0][-1], str(self.profile))
            return json.dumps({'status': 'подготовлен', 'infrastructure_ready': True, 'pre_launch_only': True, 'delivery_ready': False})
        return ''

    def launch(self, *, authenticated=True, execute=None, worker_failure=None):
        with patch.object(install, 'validate_existing_app', return_value=True), \
             patch.object(install, 'entry_points', return_value=(self.root / 'node/bin/node', None, None)), \
             patch.object(install, 'assert_stopped'), \
             patch.object(install, 'run', side_effect=execute or self.execute), \
             patch.object(startup.worker_check, 'wait_ready', side_effect=worker_failure, return_value={'worker_ready': True, 'pid': 42, 'timeout_seconds': 1800}), \
             patch.object(startup.verify, 'authentication_readback', return_value={
                 'codex_auth': 'logged_in' if authenticated else 'login_required', 'file_auth_present': authenticated}):
            return startup.start(self.root, wait_import=0)

    def test_fresh_import_then_preferences_then_verify_then_plain_launch(self):
        result = self.launch()
        self.assertTrue(result['app_launch_requested'])
        self.assertTrue(result['full_verify_passed'])
        self.assertTrue(result['worker_ready'])
        self.assertEqual([c[0][0] for c in self.calls], ['/usr/bin/open', '/usr/bin/osascript', self.calls[2][0][0], '/usr/bin/open'])
        self.assertIn('verify.py', self.calls[2][0][1])
        prefs = install.read_json(self.user / 'desktop.json')
        self.assertEqual(prefs['workerId'], self.saved_id)
        self.assertEqual(prefs['custom'], 'preserved')
        self.assertEqual(prefs['settings']['other'], 'preserved')

    def test_existing_wrong_profile_cannot_start_before_import(self):
        self.imported('https://wrong.invalid')
        self.assertTrue(self.launch()['import_bootstrap_used'])
        self.assertFalse(self.calls[0][1]['autoStart'])

    def test_update_resume_cannot_bypass_import_and_auto_update_is_preserved(self):
        prefs = install.read_json(self.user / 'desktop.json')
        prefs.update(autoUpdate=True, resumeAfterUpdate=True)
        install.write_json(self.user / 'desktop.json', prefs, self.root)
        self.launch()
        self.assertFalse(self.calls[0][1]['autoStart'])
        for _, observed in self.calls:
            self.assertTrue(observed['autoUpdate'])
            self.assertFalse(observed['resumeAfterUpdate'])

    def test_matching_profile_skips_import_bootstrap(self):
        self.imported(confirmed=True)
        result = self.launch()
        self.assertFalse(result['import_bootstrap_used'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][0], ['/usr/bin/open', '-a', str(self.app)])

    def test_failed_import_quits_bootstrap_and_never_launches_worker(self):
        def execute(command, **kwargs):
            self.calls.append(([str(x) for x in command], {}))
            return ''
        with self.assertRaises(install.SetupError):
            self.launch(execute=execute)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][0][0], '/usr/bin/osascript')
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_missing_auth_keeps_intake_stopped_and_requests_file_login(self):
        result = self.launch(authenticated=False)
        self.assertEqual(result['state'], 'awaiting_codex_login')
        self.assertFalse(result['app_launch_requested'])
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_failed_full_verify_never_opens_worker_and_disables_autostart(self):
        self.imported(confirmed=True)
        def execute(command, **kwargs):
            self.calls.append(([str(x) for x in command], {}))
            return json.dumps({'status': 'не готов', 'delivery_ready': False})
        with self.assertRaises(install.SetupError):
            self.launch(execute=execute)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('verify.py', self.calls[0][0][1])
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_verify_nonzero_prevents_launch(self):
        self.imported(confirmed=True)
        with self.assertRaises(install.SetupError):
            self.launch(execute=lambda *a, **k: (_ for _ in ()).throw(install.SetupError('fixture exit 1')))
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_old_public_match_is_not_proof_of_current_credentials_import(self):
        self.imported()
        # The mocked importer leaves identical old ciphertext untouched: must
        # fail even though public fields happen to match the incoming profile.
        with self.assertRaises(install.SetupError):
            self.launch()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_profile_change_during_verify_prevents_final_open(self):
        self.imported(confirmed=True)
        def execute(command, **kwargs):
            result = self.execute(command, **kwargs)
            if 'verify.py' in str(command[1]):
                self.imported('https://changed.invalid')
            return result
        with self.assertRaises(install.SetupError):
            self.launch(execute=execute)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_nonobject_verify_output_does_not_bypass_rollback(self):
        self.imported(confirmed=True)
        with self.assertRaises(install.SetupError):
            self.launch(execute=lambda *a, **k: '[]')
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_verification_receipt_write_failure_disables_autostart(self):
        self.imported(confirmed=True)
        (self.root / 'verification.json').mkdir()
        with self.assertRaises(install.SetupError):
            self.launch()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])

    def test_postlaunch_worker_timeout_mismatch_is_nonready(self):
        self.imported(confirmed=True)
        with self.assertRaises(startup.StartupFailure) as caught:
            self.launch(worker_failure=install.SetupError('Actual timeout 180'))
        result = caught.exception.result
        self.assertTrue(result['app_launch_requested'])
        self.assertFalse(result['delivery_ready'])
        self.assertFalse(result['full_verify_passed'])
        self.assertEqual(result['status'], 'не готов')
        self.assertFalse(install.read_json(self.user / 'desktop.json')['autoStart'])
        saved = install.read_json(self.root / 'startup.json')
        self.assertEqual(saved['state'], 'worker_check_failed')

    def test_postlaunch_failure_cli_returns_nonzero_and_true_launch_state(self):
        failure = startup.StartupFailure({'failure': 'wrong worker', 'status': 'не готов',
                                         'app_launch_requested': True, 'delivery_ready': False})
        import contextlib, io
        output = io.StringIO()
        with patch.object(startup, 'start', side_effect=failure), contextlib.redirect_stdout(output):
            code = startup.main(['--root', str(self.root)])
        self.assertNotEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())['app_launch_requested'])

    def test_final_verification_report_failure_is_not_delivery_ready(self):
        self.imported(confirmed=True)
        original_write = install.write_json
        writes = 0
        def write(file, value, root):
            nonlocal writes
            if Path(file).name == 'verification.json':
                writes += 1
                if writes == 2:
                    raise OSError('fictional report failure')
            return original_write(file, value, root)
        with patch.object(install, 'write_json', side_effect=write):
            with self.assertRaises(startup.StartupFailure) as caught:
                self.launch()
        self.assertTrue(caught.exception.result['worker_ready'])
        self.assertFalse(caught.exception.result['delivery_ready'])
        self.assertEqual(caught.exception.result['status'], 'не готов')


if __name__ == '__main__':
    unittest.main(verbosity=2)
