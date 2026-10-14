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
import runtime_contract


class WorkerCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sbs-worker-check-')
        self.root = Path(self.temp.name).resolve()
        self.user = self.root / 'user'
        self.state = self.user / 'worker-state'
        self.state.mkdir(parents=True, mode=0o700)
        (self.state / 'worker').mkdir()
        self.binary = self.root / 'aim2-worker'
        self.binary.write_bytes(b'fictional worker; never executed')
        self.binary.chmod(0o700)
        self.setup = {'real_worker': str(self.binary), 'real_worker_sha256': install.digest(self.binary),
                      'auth_home': str(self.root / 'auth'), 'codex_wrapper': str(self.root / 'bin/codex'), 'worker_concurrency': 4, 'app_version':'0.3.9'}
        (self.root/'bin').mkdir()
        (self.root/'bin/codex').write_text('fixed launcher fixture')
        self.descriptor = {'schema_version':1,'contract':'sbs-codex-v1','launcher_version':'6.0.0','config_fingerprint':'f'*64,'total_agent_slots':11,'sandbox':'workspace-write','network_access':True,'real_codex':{'binary':str(self.root/'real-codex'),'version':'codex-cli 0.162.1'}}
        self.now = 100000.0
        self.config = {'work_dir': str(self.state / 'worker'), 'concurrency': 4, 'executors': {'codex': {
            'binary': self.setup['codex_wrapper'], 'codex_home': self.setup['auth_home'], 'enabled': True,
            'timeout_seconds': 1800, 'required_contract':'sbs-codex-v1'}}}
        self.service = {'pid':42,'port':10000}
        self.process_birth = self.now-2
        self.metrics = {'pid': 42, 'updated_at': self.now, 'uptime': 2,
                        'consume_codex': True, 'consume_eliza': False, 'max_browsers': 4}
        runtime_contract.configure(self.root,self.user)
        h=runtime_contract.fingerprint(runtime_contract.expected(self.root))
        identity={'realpath':str(self.root/'bin/codex'),'sha256':install.digest(self.root/'bin/codex')}
        self.config['executors']['codex']['runtime']={'mode':'custom_launcher','config_hash':h,'launcher_identity':identity}
        self.metrics['codex_runtime']={'pid':42,'started_at':self.now-1,'version':'0.3.9','codex_version':'codex-cli 0.162.1','mode':'custom_launcher','binary':self.setup['codex_wrapper'],'launcher_identity':identity,'timeout_seconds':1800,'concurrency':4,'config_hash':h,'contract':'sbs-codex-v1','contract_version':'6.0.0','config_fingerprint':'f'*64,'real_codex':self.descriptor['real_codex'],'ready':True,'error':None,'restart_required':False,'accepting':True}
        self.persist()

    def tearDown(self):
        self.temp.cleanup()

    def persist(self):
        install.write_json(self.user / 'worker-config.json', self.config, self.root)
        install.write_json(self.state / 'service.json', self.service, self.root)
        install.write_json(self.state / 'worker/metrics.json', self.metrics, self.root)

    def read(self, *, minimum=None, process=None):
        with patch.object(worker_check, 'descriptor_readback', return_value=self.descriptor), patch.object(worker_check, 'process_identity', return_value=process or (str(self.binary), 3)), \
             patch.object(worker_check, 'process_started_at', return_value=self.process_birth), patch.object(worker_check.os, 'kill'), patch.object(worker_check.time, 'time', return_value=self.now):
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

    def test_stale_native_service_and_foreign_process_rejected(self):
        with self.assertRaises(install.SetupError): self.read(minimum=self.now - 0.5)
        other = self.root / 'other-worker'
        other.write_text('foreign fixture')
        with self.assertRaises(install.SetupError): self.read(process=(str(other), 3))
        self.process_birth = self.now - 30
        self.persist()
        with self.assertRaises(install.SetupError): self.read(process=(str(self.binary), 0))

    def test_changed_generated_config_cannot_override_native_config(self):
        self.config['executors']['codex']['timeout_seconds'] = 180
        install.write_json(self.user / 'worker-config.json', self.config, self.root)
        with self.assertRaises(install.SetupError): self.read()

    def test_native_identity_and_readiness_mismatches_rejected(self):
        cases = [('ready',False),('ready',1),('accepting',False),('restart_required',True),
                 ('timeout_seconds',180),('concurrency',1),('config_hash','0'*64),
                 ('config_fingerprint','0'*64),('contract','other'),('contract_version','5.0.0'),
                 ('version','0.3.6'),('codex_version','codex-cli 0.160.1'),('error','not_ready')]
        for key,value in cases:
            with self.subTest(key=key):
                previous=copy.deepcopy(self.metrics)
                self.metrics['codex_runtime'][key]=value
                self.persist()
                with self.assertRaises(install.SetupError):self.read()
                self.metrics=previous

    def test_missing_native_runtime_cannot_use_legacy_launch_receipt(self):
        self.metrics.pop('codex_runtime')
        install.write_json(self.state/'sbs-worker-launch.json', {'pid':42,'config_sha256':install.digest(self.user/'worker-config.json')},self.root)
        self.persist()
        with self.assertRaises(install.SetupError):self.read()

    def test_changed_persistent_config_requires_reapply(self):
        config=runtime_contract.expected(self.root)
        config['codex']['timeout_seconds']=180
        install.write_json(self.user/'executor-runtime.json',config,self.root)
        with self.assertRaises(install.SetupError):self.read()

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
