"""Read-only proof of the actual worker; no broker requests or secret output."""
import math
import datetime
import json
import os
from pathlib import Path
import stat
import time

import install
import runtime_contract


def private_json(file, sensitive=False):
    file = install.safe_path(file)
    info = file.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (0o077 if sensitive else 0o022) or info.st_size > 4 * 1024 * 1024:
        raise install.SetupError('Unsafe worker evidence file')
    return install.read_json(file)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def process_identity(pid):
    # comm excludes arguments/environment (which may contain credentials).
    env = dict(os.environ, LC_ALL='C')
    command = install.run(['/bin/ps', '-p', str(pid), '-o', 'comm='], env=env, timeout=5).strip()
    elapsed = install.run(['/bin/ps', '-p', str(pid), '-o', 'etime='], env=env, timeout=5).strip()
    days, clock = elapsed.split('-', 1) if '-' in elapsed else ('0', elapsed)
    parts = [int(x) for x in clock.split(':')]
    if len(parts) not in (2, 3) or any(x < 0 for x in parts):
        raise install.SetupError('Unexpected worker process elapsed-time format')
    seconds = int(days) * 86400 + (parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1])
    return command, seconds


def process_started_at(pid):
    # Native 0.3.10 service.json contains only pid/port. Read process birth from
    # the OS rather than inventing a timestamp field in that file.
    env = dict(os.environ, LC_ALL='C')
    value = install.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='], env=env, timeout=5).strip()
    date = datetime.datetime.strptime(value, '%a %b %d %H:%M:%S %Y')
    return time.mktime(date.timetuple())


def descriptor_readback(setup):
    launcher = Path(setup['codex_wrapper'])
    root = launcher.parent.parent
    node, _, _ = install.entry_points(setup, root)
    install.trusted_path(launcher, root, executable=True)
    if launcher.read_text() != install.shell_wrapper(node, root / 'lib/codex-launcher.mjs'):
        raise install.SetupError('Configured launcher differs from the fixed SBS wrapper')
    value = json.loads(install.run([launcher, '--sbs-print-config'], timeout=20))
    if not isinstance(value, dict):
        raise install.SetupError('SBS descriptor must be an object')
    return value


def snapshot(user_data, setup, minimum_started_at=None):
    concurrency = setup.get('worker_concurrency')
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise install.SetupError('Invalid installed worker concurrency')
    state = user_data / 'worker-state'
    config_file = user_data / 'worker-config.json'
    service = private_json(state / 'service.json')
    metrics = private_json(state / 'worker/metrics.json')
    config = private_json(config_file, sensitive=True)
    native = metrics.get('codex_runtime')
    if not isinstance(native, dict):
        raise install.SetupError('Native Codex readiness is missing; running alone is insufficient')
    pid = metrics.get('pid')
    if type(pid) is not int or pid <= 1 or service.get('pid') != pid or native.get('pid') != pid:
        raise install.SetupError('Worker PID differs between native runtime, service and metrics')
    if type(service.get('port')) is not int or not 1 <= service['port'] <= 65535:
        raise install.SetupError('Invalid worker service descriptor')
    os.kill(pid, 0)
    executable, elapsed = process_identity(pid)
    expected_worker = str(Path(setup['real_worker']))
    if not os.path.samefile(executable, expected_worker):
        raise install.SetupError('Actual worker executable differs from the installed worker')
    if install.digest(expected_worker) != setup['real_worker_sha256']:
        raise install.SetupError('Actual worker executable digest mismatch')
    started = process_started_at(pid)
    runtime_started = native.get('started_at')
    now = time.time()
    if not number(started) or not 0 <= now - started <= elapsed + 3 or now - elapsed > started + 3:
        raise install.SetupError('Worker PID was reused or its native service identity is stale')
    if not number(runtime_started) or not started <= runtime_started <= now:
        raise install.SetupError('Native Codex readiness predates this service')
    # ps lstart is rounded to seconds; readiness has sub-second precision.
    if minimum_started_at is not None and (started + 1 < minimum_started_at or runtime_started < minimum_started_at):
        raise install.SetupError('Worker service predates this app launch')
    codex = config.get('executors', {}).get('codex', {})
    if (codex.get('binary') != setup['codex_wrapper']
            or type(codex.get('timeout_seconds')) is not int or codex['timeout_seconds'] != 1800
            or codex.get('enabled') is not True or codex.get('required_contract') != 'sbs-codex-v1'
            or type(config.get('concurrency')) is not int or config['concurrency'] != concurrency
            or not os.path.samefile(config.get('work_dir', ''), state / 'worker')):
        raise install.SetupError('Actual worker config must use the Codex launcher, timeout 1800 and the installed concurrency')
    updated = metrics.get('updated_at')
    uptime = metrics.get('uptime')
    if not number(updated) or not 0 <= now - updated <= 15 or updated < runtime_started:
        raise install.SetupError('Worker metrics heartbeat is stale or predates launch')
    if not number(uptime) or not 0 <= uptime <= elapsed + 3:
        raise install.SetupError('Worker metrics uptime does not match the live process')
    if metrics.get('consume_codex') is not True or metrics.get('consume_eliza') is not False or type(metrics.get('max_browsers')) is not int or metrics.get('max_browsers') != concurrency:
        raise install.SetupError('Actual worker intake/limits do not match desktop preferences')
    root = Path(setup['codex_wrapper']).parent.parent
    persistent = runtime_contract.readback(root, user_data)
    descriptor = descriptor_readback(setup)
    launcher = Path(setup['codex_wrapper'])
    identity = {'realpath': str(launcher.resolve()), 'sha256': install.digest(launcher)}
    required = {'mode': 'custom_launcher', 'binary': setup['codex_wrapper'], 'launcher_identity': identity,
                'timeout_seconds': 1800, 'concurrency': concurrency, 'config_hash': persistent['runtime_config_hash'],
                'contract': 'sbs-codex-v1', 'contract_version': descriptor.get('launcher_version'),
                'config_fingerprint': descriptor.get('config_fingerprint'), 'real_codex': descriptor.get('real_codex'),
                'ready': True, 'error': None, 'restart_required': False, 'accepting': True, 'version': setup.get('app_version')}
    if any(key not in native or native[key] != value for key, value in required.items()) or type(native.get('timeout_seconds')) is not int or type(native.get('concurrency')) is not int:
        raise install.SetupError('Actual native Codex runtime is not ready with the installed launcher, descriptor, timeout and configuration')
    if native.get('ready') is not True or native.get('accepting') is not True or native.get('restart_required') is not False:
        raise install.SetupError('Native readiness/intake/restart flags must be explicit booleans')
    if descriptor.get('contract') != 'sbs-codex-v1' or descriptor.get('schema_version') != 1 or descriptor.get('total_agent_slots') != 11 or descriptor.get('sandbox') != 'workspace-write' or descriptor.get('network_access') is not True:
        raise install.SetupError('Current SBS descriptor is incompatible')
    if native.get('codex_version') != descriptor.get('real_codex', {}).get('version'):
        raise install.SetupError('Native real Codex version differs from current descriptor')
    runtime = codex.get('runtime', {})
    if runtime.get('mode') != 'custom_launcher' or runtime.get('config_hash') != persistent['runtime_config_hash'] or runtime.get('launcher_identity') != identity or runtime.get('error'):
        raise install.SetupError('Generated worker runtime differs from native persistent configuration')
    # Output only an explicit public projection; never serialize config/env.
    return {'pid': pid, 'worker_executable': executable, 'started_at': started,
            'codex_binary': codex['binary'], 'timeout_seconds': 1800, 'concurrency': concurrency,
            'consume_codex': True, 'max_browsers': concurrency, 'heartbeat_updated_at': updated,
            'uptime': uptime, 'config_sha256': persistent['runtime_config_hash'],
            'config_fingerprint': native['config_fingerprint'], 'contract': native['contract'],
            'launcher_version': native['contract_version'], 'real_codex': native['real_codex'],
            'native_ready': True, 'native_accepting': True, 'worker_version': native['version']}


def wait_ready(user_data, setup, timeout=45, minimum_started_at=None):
    deadline = time.monotonic() + timeout
    previous = None
    last_error = 'Actual worker evidence is unavailable'
    while True:
        try:
            current = snapshot(user_data, setup, minimum_started_at)
            if (previous and current['pid'] == previous['pid'] and current['started_at'] == previous['started_at']
                    and current['config_sha256'] == previous['config_sha256']
                    and current['heartbeat_updated_at'] > previous['heartbeat_updated_at']
                    and current['uptime'] > previous['uptime']):
                return dict(current, worker_ready=True, heartbeat_progress_confirmed=True,
                            broker_end_to_end_verified=False)
            previous = current
            last_error = 'Actual worker heartbeat has not advanced'
        except Exception as error:
            previous = None
            last_error = str(error) if isinstance(error, install.SetupError) else 'Worker evidence or process readback unavailable'
        if time.monotonic() >= deadline:
            raise install.SetupError(last_error)
        time.sleep(0.25)
