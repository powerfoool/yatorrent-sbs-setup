"""Actual process-group termination with synthetic auth; no Codex/broker calls."""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


class AuthCleanupTests(unittest.TestCase):
    def exercise(self, mode):
        node = os.environ.get('SBS_TEST_NODE') or shutil.which('node')
        if not node or os.name != 'posix':
            self.skipTest('POSIX Node process required')
        with tempfile.TemporaryDirectory(prefix='sbs-auth-fixture-') as temporary:
            root = Path(temporary).resolve()
            lib = root / 'lib'; lib.mkdir()
            auth = root / 'private-auth'; auth.mkdir(mode=0o700)
            (auth / 'auth.json').write_text('synthetic-auth-fixture')
            (auth / 'rules').mkdir()
            source = Path(__file__).resolve().parents[1] / 'assets'
            for name in ('codex-launcher.mjs', 'sbs-auth-guard.mjs', 'sbs-task-instructions.mjs', 'sbs-url-budget.mjs'):
                shutil.copyfile(source / name, lib / name)
            fake = root / 'fake-codex'
            fake.write_text('#!' + node + '\n' + "require('fs').writeFileSync('.fake-ready','ready'); if(process.argv.includes('finish')) process.exit(0); setInterval(()=>{},1000);\n")
            fake.chmod(0o700)
            (lib / 'install-config.json').write_text(json.dumps({'real_codex': str(fake), 'auth_home': str(auth), 'rule_dir': str(auth / 'rules'), 'node_bin': node, 'scout_threads': 10}))
            if mode == 'missing_guard':
                (lib / 'sbs-auth-guard.mjs').unlink()
            proc = subprocess.Popen([node, str(lib / 'codex-launcher.mjs'), '--sbs-diagnostic', 'exec', '--sandbox', 'workspace-write', *(['finish'] if mode == 'normal' else [])], cwd=root, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if mode in ('SIGKILL', 'SIGTERM'):
                    deadline = time.monotonic() + 10
                    while not (root / '.fake-ready').exists():
                        if proc.poll() is not None or time.monotonic() > deadline:
                            self.fail('Fixture CLI did not become ready')
                        time.sleep(0.02)
                    homes = list(root.glob('.sbs-cli-*'))
                    self.assertEqual(len(homes), 1)
                    self.assertTrue((homes[0] / 'auth.json').is_symlink())
                    os.killpg(proc.pid, getattr(signal, mode))
                proc.communicate(timeout=10)
                if mode == 'normal':
                    self.assertEqual(proc.returncode, 0)
                if mode == 'missing_guard':
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertFalse((root / '.fake-ready').exists())
                deadline = time.monotonic() + 5
                while any((p / 'auth.json').is_symlink() for p in root.glob('.sbs-cli-*')) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(any((p / 'auth.json').is_symlink() for p in root.glob('.sbs-cli-*')))
                self.assertEqual((auth / 'auth.json').read_text(), 'synthetic-auth-fixture')
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=10)

    def test_normal_exit_cleans_only_temporary_auth_link(self):
        self.exercise('normal')

    def test_worker_hard_group_kill_still_cleans_auth_link(self):
        self.exercise('SIGKILL')

    def test_term_group_kill_cleans_auth_link(self):
        self.exercise('SIGTERM')

    def test_missing_guard_prevents_cli_execution(self):
        self.exercise('missing_guard')


if __name__ == '__main__':
    unittest.main(verbosity=2)
