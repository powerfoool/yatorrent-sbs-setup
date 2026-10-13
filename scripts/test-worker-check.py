"""Actual-worker proof fixtures: ordinary Codex, stale files and live heartbeat."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install
import worker_check


class WorkerCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sbs-worker-check-')
        self.root = Path(self.temp.name).resolve()
        self.user = self.root / 'user'
        self.state = self.user / 'worker-state'
        self.state.mkdir(parents=True, mode=0o700)
        (self.state / 'worker').mkdir()
        self.binary = self.root / 'aim2-worker-original'
        self.binary.write_bytes(b'fictional worker; never executed')
        self.binary.chmod(0o700)
        self.setup = {'real_worker': str(self.binary), 'real_worker_sha256': install.digest(self.binary),
                      'auth_home': str(self.root / 'auth'), 'codex_wrapper': str(self.root / 'bin/codex'), 'worker_concurrency': 4}
        self.now = 100000.0
        self.config = {'work_dir': str(self.state / 'worker'), 'concurrency': 4, 'executors': {'codex': {
            'binary': self.setup['codex_wrapper'], 'codex_home': self.setup['auth_home'], 'enabled': True,
            'timeout_seconds': 1800, 'max_output_bytes': 8 * 1024 * 1024}}}
        self.receipt = {'schema_version': 1, 'pid': 42, 'started_at': self.now - 2,
                        'config_file': str(self.user / 'worker-config.json'), 'state_dir': str(self.state),
                        'real_worker': str(self.binary), 'real_worker_sha256': self.setup['real_worker_sha256']}
        self.metrics = {'pid': 42, 'updated_at': self.now, 'uptime': 2,
                        'consume_codex': True, 'consume_eliza': False, 'max_browsers': 4}
        self.persist()

    def tearDown(self):
        self.temp.cleanup()

    def persist(self):
        install.write_json(self.user / 'worker-config.json', self.config, self.root)
        self.receipt['config_sha256'] = install.digest(self.user / 'worker-config.json')
        install.write_json(self.state / 'sbs-worker-launch.json', self.receipt, self.root)
        install.write_json(self.state / 'service.json', {'pid': 42, 'port': 10000}, self.root)
        install.write_json(self.state / 'worker/metrics.json', self.metrics, self.root)

    def read(self, *, minimum=None, process=None):
        with patch.object(worker_check, 'process_identity', return_value=process or (str(self.binary), 3)), \
             patch.object(worker_check.os, 'kill'), patch.object(worker_check.time, 'time', return_value=self.now):
            return worker_check.snapshot(self.user, self.setup, minimum)

    def test_correct_pid_launch_config_and_metrics(self):
        self.assertEqual(self.read()['timeout_seconds'], 1800)
        self.assertEqual(self.read()['pid'], 42)

    def test_ordinary_codex_and_default_timeout_rejected_even_with_live_metrics(self):
        for key, value in [('binary', '/ordinary/bin/codex'), ('timeout_seconds', 180), ('timeout_seconds', True)]:
            with self.subTest(key=key, value=value):
                original = copy.deepcopy(self.config)
                self.config['executors']['codex'][key] = value
                self.persist()
                with self.assertRaises(install.SetupError): self.read()
                self.config = original

    def test_stale_receipt_and_foreign_process_rejected(self):
        with self.assertRaises(install.SetupError): self.read(minimum=self.now - 1)
        other = self.root / 'other-worker'
        other.write_text('foreign fixture')
        with self.assertRaises(install.SetupError): self.read(process=(str(other), 3))
        self.receipt['started_at'] = self.now - 30
        self.persist()
        with self.assertRaises(install.SetupError): self.read(process=(str(self.binary), 0))

    def test_changed_config_cannot_reuse_launch_receipt(self):
        self.config['executors']['codex']['timeout_seconds'] = 180
        install.write_json(self.user / 'worker-config.json', self.config, self.root)
        with self.assertRaises(install.SetupError): self.read()

    def test_pid_and_freshness_and_intake_mismatches_rejected(self):
        cases = [('pid', 43), ('updated_at', self.now - 30), ('updated_at', self.now + 1),
                 ('updated_at', float('nan')), ('uptime', 100), ('consume_codex', False), ('max_browsers', 2)]
        for key, value in cases:
            with self.subTest(key=key):
                previous = copy.deepcopy(self.metrics)
                self.metrics[key] = value
                self.persist()
                with self.assertRaises(install.SetupError): self.read()
                self.metrics = previous

    def test_config_file_permissions_and_symlink_rejected(self):
        file = self.user / 'worker-config.json'
        file.chmod(0o644)
        with self.assertRaises(install.SetupError): self.read()
        file.chmod(0o600)
        target = self.root / 'other.json'
        target.write_bytes(file.read_bytes())
        file.unlink()
        file.symlink_to(target)
        with self.assertRaises(install.SetupError): self.read()

    def test_dead_worker_rejected(self):
        with patch.object(worker_check.os, 'kill', side_effect=ProcessLookupError()):
            with self.assertRaises(ProcessLookupError):
                worker_check.snapshot(self.user, self.setup)

    def test_single_fresh_file_is_not_proof_of_live_heartbeat(self):
        with patch.object(worker_check, 'snapshot', return_value=self.read()):
            with self.assertRaises(install.SetupError):
                worker_check.wait_ready(self.user, self.setup, timeout=0)

    def test_advancing_same_pid_heartbeat_confirms_worker(self):
        first = self.read()
        second = dict(first, heartbeat_updated_at=first['heartbeat_updated_at'] + 1, uptime=3)
        with patch.object(worker_check, 'snapshot', side_effect=[first, second]), patch.object(worker_check.time, 'sleep'):
            result = worker_check.wait_ready(self.user, self.setup)
        self.assertTrue(result['worker_ready'])
        self.assertTrue(result['heartbeat_progress_confirmed'])

    def test_public_result_never_contains_unrelated_config_or_metrics_data(self):
        self.config['credentials'] = {'fixture_secret': 'must-not-print'}
        self.metrics['private_data'] = 'must-not-print'
        self.persist()
        self.assertNotIn('must-not-print', json.dumps(self.read()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
