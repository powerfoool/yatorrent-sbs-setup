#!/usr/bin/env python3
"""Coordinator add-on after the common worker setup; never imports credentials from another Mac."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import venv
import uuid

import install

SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
VALIDATOR_REQUIREMENTS = ['jsonschema==4.26.0', 'attrs==26.1.0', 'jsonschema-specifications==2025.9.1',
                          'referencing==0.37.0', 'rpds-py==2026.9.1']


def status(gws, directory):
    env = dict(os.environ, GOOGLE_WORKSPACE_CLI_CONFIG_DIR=str(directory))
    for key in ['GOOGLE_WORKSPACE_CLI_TOKEN', 'GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE',
                'GOOGLE_WORKSPACE_CLI_CLIENT_ID', 'GOOGLE_WORKSPACE_CLI_CLIENT_SECRET']:
        env.pop(key, None)
    if Path('/.env').exists():
        raise install.SetupError('System dotenv would override Google profile')
    raw = install.run([gws, 'auth', 'status'], env=env, cwd='/', timeout=30)
    value = json.loads('\n'.join(x for x in raw.splitlines() if not x.startswith('Using keyring backend')))
    # Do not store the CLI auth status wholesale: it can contain client metadata.
    scopes = value.get('scopes', [])
    if isinstance(scopes, str):
        scopes = scopes.replace(',', ' ').split()
    # New gws status exposes encryption validity but no refresh-token/scopes.
    # Unknown write scopes are not a reason to force an existing user to log in.
    signed = value.get('has_refresh_token') is True or (
        value.get('auth_method') == 'oauth2' and value.get('encryption_valid') is True)
    return {'signed_in': signed,
            'sheets_read_write_scope': SCOPE in scopes if 'scopes' in value else None}


def configure(args):
    if sys.version_info < (3, 10):
        raise install.SetupError('Python 3.10 or newer required; select supported Python before installation')
    root = install.command_path(args.root)
    google = install.safe_path(args.google_config)
    base = {'mode': 'apply' if args.apply else 'plan', 'role': 'coordinator',
            'worker_on_this_machine': True, 'pairs_per_task': 1,
            'common_setup': 'scripts/install.py --apply --profile <external private AIM2>',
            'google_enabled': not args.no_google, 'credentials_bundled': False,
            'format_agreement_required': True, 'broker_tasks_sent': 0, 'sheet_writes': 0}
    if not args.apply:
        return {**base, 'mutates': False, 'google_scope': SCOPE,
            'operations': ['reuse common verified worker runtime', 'install private JSON Schema validator', 'install gws if missing',
                               'install portable private gwsx work', 'reuse this Mac Google login or request owner OAuth',
                               'agree result schema and output columns before dispatch/write']}
    setup = install.read_json(root / 'lib/install-config.json')
    install.entry_points(setup, root)
    virtual = install.safe_path(root / 'coordinator-python')
    if virtual.exists():
        info = virtual.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise install.SetupError('Private validator environment is not trusted')
        current = json.loads(install.run([virtual / 'bin/python3', '-c', 'import sys,json;print(json.dumps(list(sys.version_info[:2])))'], timeout=20))
        if not isinstance(current, list) or len(current) != 2 or any(type(x) is not int for x in current):
            raise install.SetupError('Cannot verify existing validator Python')
        if tuple(current) < (3, 10):
            retired = root / 'backups' / ('coordinator-python-' + uuid.uuid4().hex)
            retired.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(virtual, retired)
    if not virtual.exists():
        venv.EnvBuilder(with_pip=True).create(virtual)
    python = virtual / 'bin/python3'
    install.run([python, '-m', 'pip', 'install', '--only-binary=:all:', *VALIDATOR_REQUIREMENTS], timeout=900)
    install.write_owned(root / 'lib/coordinator-validate.py', (install.ASSETS / 'coordinator-validate.py').read_bytes(), root)
    validator = '#!/bin/sh\nexec ' + install.shlex.quote(str(python)) + ' ' + install.shlex.quote(str(root / 'lib/coordinator-validate.py')) + ' "$@"\n'
    install.write_owned(root / 'bin/coordinator-validate', validator, root, 0o755)
    base['validator'] = str(root / 'bin/coordinator-validate')
    if args.no_google:
        result = {**base, 'status': 'local_tools_prepared', 'google_ready': False,
                  'source_required': 'local pairs file', 'worker_readiness_verified_here': False}
        install.write_json(root / 'coordinator-setup.json', result, root)
        return result
    gws = shutil.which('gws')
    if not gws:
        brew = shutil.which('brew')
        if not brew:
            raise install.SetupError('Google tools need Homebrew; owner must install supported Homebrew first')
        install.run([brew, 'install', 'googleworkspace-cli'], timeout=1800)
        gws = shutil.which('gws') or str(Path(brew).parent / 'gws')
    version = install.run([gws, '--version'], timeout=30).strip().splitlines()[0]
    if not version.startswith('gws '):
        raise install.SetupError('Selected gws is not Google Workspace CLI')
    google.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = google.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise install.SetupError('Google profile directory must be owned and not writable by other users')
    google.chmod(0o700)
    # Existing authentication remains local and is never copied into the release.
    cfg = {'gws': str(Path(gws).resolve()), 'config_dir': str(google)}
    install.write_json(root / 'lib/google-config.json', cfg, root)
    install.write_owned(root / 'lib/gwsx.py', (install.ASSETS / 'gwsx.py').read_bytes(), root)
    wrapper = '#!/bin/sh\nexec ' + install.shlex.quote(sys.executable) + ' ' + install.shlex.quote(str(root / 'lib/gwsx.py')) + ' "$@"\n'
    install.write_owned(root / 'bin/gwsx', wrapper, root, 0o755)
    try:
        auth = status(gws, google)
    except (install.SetupError, ValueError):
        auth = {'signed_in': False, 'sheets_read_write_scope': False}
    ready = auth['signed_in'] is True
    result = {**base, 'gws_version': version, 'gwsx': str(root / 'bin/gwsx'),
              'auth': auth, 'google_ready': ready,
              'status': 'google_tools_prepared' if ready else 'awaiting_google_login',
              'worker_readiness_verified_here': False,
              'document_access_verified': False, 'write_access_verified': False,
              'next': 'read requested sheet and agree output format' if ready else
                      'Configure owner OAuth desktop client here, then gwsx work --confirm auth login --scopes ' + SCOPE}
    install.write_json(root / 'coordinator-setup.json', result, root)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=install.DEFAULT_ROOT)
    parser.add_argument('--google-config', type=Path, default=Path.home() / '.config/gws-corp-app')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--no-google', action='store_true', help='Local input/output only')
    args = parser.parse_args(argv)
    try:
        result = configure(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not args.apply or result.get('google_ready') or args.no_google else 2
    except (install.SetupError, OSError, ValueError, IndexError):
        error = sys.exc_info()[1]
        print('Coordinator setup incomplete: ' + (str(error) if isinstance(error, install.SetupError) else 'private diagnostics withheld'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
