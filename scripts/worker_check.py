"""Read-only proof of the actual worker; no broker requests or secret output."""
import math
import os
from pathlib import Path
import stat
import time

import install


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


def snapshot(user_data, setup, minimum_started_at=None):
    concurrency = setup.get('worker_concurrency')
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise install.SetupError('Invalid installed worker concurrency')
    state = user_data / 'worker-state'
    config_file = user_data / 'worker-config.json'
    receipt = private_json(state / 'sbs-worker-launch.json', sensitive=True)
    service = private_json(state / 'service.json')
    metrics = private_json(state / 'worker/metrics.json')
    config = private_json(config_file, sensitive=True)
    pid = receipt.get('pid')
    if receipt.get('schema_version') != 1 or type(pid) is not int or pid <= 1 or service.get('pid') != pid or metrics.get('pid') != pid:
        raise install.SetupError('Worker PID differs between launch receipt, service and metrics')
    if type(service.get('port')) is not int or not 1 <= service['port'] <= 65535:
        raise install.SetupError('Invalid worker service descriptor')
    os.kill(pid, 0)
    executable, elapsed = process_identity(pid)
    expected_worker = str(Path(setup['real_worker']))
    if not os.path.samefile(executable, expected_worker) or not os.path.samefile(receipt.get('real_worker', ''), expected_worker):
        raise install.SetupError('Actual worker executable differs from the installed worker')
    if (receipt.get('real_worker_sha256') != setup['real_worker_sha256']
            or install.digest(expected_worker) != setup['real_worker_sha256']):
        raise install.SetupError('Actual worker executable digest mismatch')
    if (not os.path.samefile(receipt.get('config_file', ''), config_file) or not os.path.samefile(receipt.get('state_dir', ''), state)
            or receipt.get('config_sha256') != install.digest(config_file)):
        raise install.SetupError('Actual worker config is not the config recorded immediately before exec')
    started = receipt.get('started_at')
    now = time.time()
    if not number(started) or not 0 <= now - started <= elapsed + 10 or now - elapsed > started + 3:
        raise install.SetupError('Worker PID was reused or its launch receipt is stale')
    if minimum_started_at is not None and started < minimum_started_at:
        raise install.SetupError('Worker launch receipt predates this app launch')
    codex = config.get('executors', {}).get('codex', {})
    if (codex.get('binary') != setup['codex_wrapper'] or codex.get('codex_home') != setup['auth_home']
            or type(codex.get('timeout_seconds')) is not int or codex['timeout_seconds'] != 1800
            or codex.get('max_output_bytes') != 8 * 1024 * 1024 or codex.get('enabled') is not True
            or type(config.get('concurrency')) is not int or config['concurrency'] != concurrency
            or not os.path.samefile(config.get('work_dir', ''), state / 'worker')):
        raise install.SetupError('Actual worker config must use the Codex launcher, timeout 1800 and the installed concurrency')
    updated = metrics.get('updated_at')
    uptime = metrics.get('uptime')
    if not number(updated) or not 0 <= now - updated <= 20 or updated < started:
        raise install.SetupError('Worker metrics heartbeat is stale or predates launch')
    if not number(uptime) or not 0 <= uptime <= elapsed + 3:
        raise install.SetupError('Worker metrics uptime does not match the live process')
    if metrics.get('consume_codex') is not True or metrics.get('consume_eliza') is not False or metrics.get('max_browsers') != concurrency:
        raise install.SetupError('Actual worker intake/limits do not match desktop preferences')
    # Output only an explicit public projection; never serialize config/env.
    return {'pid': pid, 'worker_executable': executable, 'started_at': started,
            'codex_binary': codex['binary'], 'timeout_seconds': 1800, 'concurrency': concurrency,
            'consume_codex': True, 'max_browsers': concurrency, 'heartbeat_updated_at': updated,
            'uptime': uptime, 'config_sha256': receipt['config_sha256']}


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
