#!/usr/bin/env python3
"""Confirm encrypted profile, configure desktop, then launch the owned app."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

import install
import verify
import worker_check
import runtime_contract


def profile_matches(source, imported):
    if not imported.exists():
        return False
    install.safe_path(imported)
    if stat.S_IMODE(imported.stat().st_mode) != 0o600:
        raise install.SetupError('Imported profile permissions must be 0600')
    return verify.normalized_public(source) == verify.normalized_public(imported, stored=True)


def file_identity(file):
    if not file.exists():
        return None
    info = file.stat()
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def confirmed_import(source, imported, root):
    receipt = root / 'profile-import.json'
    if not receipt.exists() or not profile_matches(source, imported):
        return False
    value = install.read_json(install.safe_path(receipt))
    return (value.get('source_sha256') == install.digest(source)
            and value.get('encrypted_file_sha256') == install.digest(imported))


def preferences(user_data, root, auto_start):
    setup = install.read_json(root / "lib/install-config.json")
    concurrency = setup.get("worker_concurrency")
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise install.SetupError("Invalid installed worker concurrency")
    file = install.safe_path(user_data / 'desktop.json')
    current = install.read_json(file) if file.exists() else {}
    settings = current.get('settings', {})
    if not isinstance(settings, dict):
        raise install.SetupError('Invalid existing desktop settings; preserve and inspect them')
    if 'workerId' in current:
        try:
            if not isinstance(current['workerId'], str) or len(current['workerId']) != 36:
                raise ValueError('invalid UUID shape')
            uuid.UUID(current['workerId'])
        except (ValueError, TypeError, AttributeError):
            raise install.SetupError('Invalid existing worker identity; preserve and inspect it') from None
    # Preserve identity and unrelated preferences. These keys implement the
    # requested worker-on-app-start and Codex-intake choices.
    current.setdefault('workerId', str(uuid.uuid4()))
    current['autoStart'] = auto_start
    # A stale update-resume marker must not start the old profile during import.
    current['resumeAfterUpdate'] = False
    current.setdefault('autoUpdate', False)
    current['settings'] = dict(settings, consume_codex=True, consume_eliza=False, max_browsers=concurrency)
    install.write_json(file, current, root)
    readback = install.read_json(file)
    if readback != current:
        raise install.SetupError('Desktop preference readback failed')


def quit_import_app(app):
    install.run(['/usr/bin/osascript', '-e', 'tell application ' + json.dumps(str(app)) + ' to quit'], timeout=20)
    deadline = time.monotonic() + 20
    while True:
        try:
            install.assert_stopped()
            return
        except install.SetupError:
            if time.monotonic() >= deadline:
                raise install.SetupError('Profile import app did not quit; no worker launch attempted') from None
            time.sleep(0.25)


def start(root, wait_import=60):
    root = install.command_path(root)
    marker = install.read_json(root / 'installation.json')
    if marker.get('state') != 'prepared':
        raise install.SetupError('Installation is incomplete')
    app = install.safe_path(marker['app'])
    user_data = install.safe_path(marker['user_data'])
    install.validate_existing_app(app, root / 'installation.json')
    setup = install.read_json(root / 'lib/install-config.json')
    node, _, _ = install.entry_points(setup, root)
    install.assert_stopped()
    source = install.safe_path(root / 'profiles/databroker.aim2')
    if stat.S_IMODE(source.stat().st_mode) != 0o600:
        raise install.SetupError('Source profile permissions must be 0600')
    if install.digest(source) != marker.get('profile_sha256'):
        raise install.SetupError('Installed profile differs from the supplied external profile')
    runtime_contract.configure(root, user_data)
    imported = user_data / 'profile.json'
    bootstrap_used = not confirmed_import(source, imported, root)
    if bootstrap_used:
        # YaTorrent checks autoStart BEFORE importing a queued .aim2. Import
        # with intake stopped, quit, then enable autoStart before normal launch.
        preferences(user_data, root, False)
        before_import = file_identity(imported)
        install.run(['/usr/bin/open', '-a', app, source], timeout=30)
        try:
            deadline = time.monotonic() + wait_import
            while file_identity(imported) == before_import or not profile_matches(source, imported):
                if time.monotonic() >= deadline:
                    raise install.SetupError('Supported encrypted profile import not confirmed; no worker launch attempted')
                time.sleep(0.25)
        finally:
            quit_import_app(app)
        install.write_json(root / 'profile-import.json', {'source_sha256': install.digest(source),
                           'encrypted_file_sha256': install.digest(imported)}, root)
    auth_home = install.safe_path(setup['auth_home'])
    env = dict(os.environ, PATH=str(node.parent) + os.pathsep + os.environ.get('PATH', ''), CODEX_HOME=str(auth_home))
    auth = verify.authentication_readback(Path(setup['real_codex']), auth_home, env)
    result = {'encrypted_profile_confirmed': True, 'import_bootstrap_used': bootstrap_used,
              'app_launch_requested': False, 'worker_ready': False, 'readiness_scope': 'local_worker_only',
              'broker_end_to_end_verified': False, 'parallel_jobs_verified': False}
    if not verify.authentication_ready(auth):
        preferences(user_data, root, False)
        result.update(state='awaiting_codex_login', next_step='file-auth login, then scripts/startup.py --root <root>')
        install.write_json(root / 'startup.json', result, root)
        return result
    # Enable only for the verified final launch. Any pre-launch failure must
    # undo autoStart so a later manual app opening cannot start unchecked jobs.
    install.assert_stopped()
    try:
        preferences(user_data, root, True)
        if not confirmed_import(source, imported, root):
            raise install.SetupError('Profile changed before verification')
        verified = json.loads(install.run([sys.executable, Path(__file__).with_name('verify.py'),
                                        '--pre-launch', '--root', root, '--profile', source], env=env, timeout=1200))
        if not isinstance(verified, dict) or verified.get('infrastructure_ready') is not True or verified.get('pre_launch_only') is not True or verified.get('delivery_ready') is not False:
            raise install.SetupError('Mandatory pre-launch verify did not confirm infrastructure readiness')
        install.write_json(root / 'verification.json', verified, root)
        install.assert_stopped()
        if not confirmed_import(source, imported, root):
            raise install.SetupError('Profile changed during verification')
        result.update(state='verified_awaiting_app_launch', consume_codex=True, autoStart=True,
                      infrastructure_verify_passed=True, full_verify_passed=False, delivery_ready=False)
        # Check that required startup metadata can be persisted before opening.
        install.write_json(root / 'startup.json', result, root)
        launch_started_at = time.time()
        install.run(['/usr/bin/open', '-a', app], timeout=30)
    except Exception:
        failure = sys.exc_info()[1]
        try:
            preferences(user_data, root, False)
        except Exception:
            raise install.SetupError('Startup failed and autoStart could not be disabled; keep the app closed and inspect desktop preferences') from None
        reason = str(failure) if isinstance(failure, install.SetupError) else 'startup metadata or format check failed'
        raise install.SetupError('Startup stopped; autoStart disabled; ' + reason) from None
    result.update(state='app_launch_requested', app_launch_requested=True)
    try:
        actual = worker_check.wait_ready(user_data, setup, minimum_started_at=launch_started_at)
        if not isinstance(actual, dict) or actual.get('worker_ready') is not True:
            raise install.SetupError('Actual worker proof did not confirm readiness')
    except Exception as error:
        result.update(state='worker_check_failed', status='не готов', worker_ready=False,
                      delivery_ready=False, full_verify_passed=False,
                      failure=str(error) if isinstance(error, install.SetupError) else 'Actual worker proof unavailable')
        try:
            preferences(user_data, root, False)
            result['autoStart_disabled_on_disk'] = True
        except Exception:
            result['autoStart_disabled_on_disk'] = False
        try:
            install.write_json(root / 'startup.json', result, root)
        except Exception:
            pass
        # App has already been opened; do not claim it was not, kill another
        # worker, or restart jobs automatically. Installation must return nonzero.
        raise StartupFailure(result) from None
    result.update(state='worker_ready', status='готов', worker_ready=True, delivery_ready=True,
                  full_verify_passed=True, actual_worker=actual)
    verified.update(status='готов', pre_launch_only=False, worker_ready=True, worker=actual, delivery_ready=True)
    try:
        install.write_json(root / 'verification.json', verified, root)
        install.write_json(root / 'startup.json', result, root)
        result['startup_receipt_saved'] = True
    except (install.SetupError, OSError, ValueError):
        result.update(state='verification_report_failed', status='не готов', delivery_ready=False,
                      startup_receipt_saved=False, failure='Actual worker verified but final verification report could not be saved')
        try:
            install.write_json(root / 'startup.json', result, root)
        except Exception:
            pass
        raise StartupFailure(result) from None
    return result


class StartupFailure(install.SetupError):
    def __init__(self, result):
        super().__init__('Worker setup did not finish: ' + result['failure'])
        self.result = result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=install.DEFAULT_ROOT)
    parser.add_argument('--wait-import', type=float, default=60)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_import <= 180:
        parser.error('--wait-import must be 0..180')
    try:
        result = start(args.root, args.wait_import)
        print(json.dumps(result))
        return 0 if result.get('delivery_ready') is True else 2
    except StartupFailure as error:
        print(json.dumps(error.result, ensure_ascii=False))
        return 1
    except (install.SetupError, OSError, ValueError, KeyError, TypeError):
        error = sys.exc_info()[1]
        print(json.dumps({'status': 'не готов', 'app_launch_requested': False,
                          'failure': str(error) if isinstance(error, install.SetupError) else 'filesystem or format check failed; no secret output'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
