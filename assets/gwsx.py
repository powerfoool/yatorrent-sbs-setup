#!/usr/bin/env python3
"""Portable Sheets profile wrapper. Secrets stay in gws' local credential store."""
import json
import os
from pathlib import Path
import subprocess
import sys


READ = {'get', 'list', 'batchGet', 'status', 'schema'}


def main(argv):
    here = Path(__file__).resolve().parent
    cfg = json.loads((here / 'google-config.json').read_text())
    profile = argv.pop(0) if argv else ''
    if profile == 'status':
        if argv != ['work']:
            return 2
        argv = ['auth', 'status']
    elif profile != 'work':
        print('Use gwsx work <Sheets command> or gwsx status work', file=sys.stderr)
        return 2
    confirmed = '--confirm' in argv
    args = [a for a in argv if a != '--confirm']
    positional = []
    for arg in args:
        if arg.startswith('-'):
            break
        positional.append(arg)
    if not positional or positional[0] not in {'sheets', 'auth', 'schema'}:
        print('This installed wrapper supports Sheets, auth and schema only', file=sys.stderr)
        return 2
    if positional[0] == 'auth' and (len(positional) != 2 or positional[1] not in {'status', 'login', 'setup'}):
        print('Only local auth status/login/setup are supported', file=sys.stderr)
        return 2
    method = positional[-1]
    if method not in READ and not confirmed:
        print(json.dumps({'executed': False, 'preview': True, 'profile': 'work', 'method': method,
                          'next': 'After authorization repeat with --confirm'}, ensure_ascii=False))
        return 3
    env = dict(os.environ, GOOGLE_WORKSPACE_CLI_CONFIG_DIR=cfg['config_dir'])
    # Explicit profile wins over ambient tokens for unrelated Google accounts.
    for name in ['GOOGLE_WORKSPACE_CLI_TOKEN', 'GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE',
                 'GOOGLE_WORKSPACE_CLI_CLIENT_ID', 'GOOGLE_WORKSPACE_CLI_CLIENT_SECRET']:
        env.pop(name, None)
    # gws loads dotenv from cwd/ancestors. At '/' it has no untrusted parents;
    # refuse the exceptional system dotenv instead of allowing account override.
    if Path('/.env').exists():
        print('System dotenv would override Google profile; refusing command', file=sys.stderr)
        return 2
    child = subprocess.run([cfg['gws'], *args], env=env, cwd='/', stdout=subprocess.PIPE)
    lines = child.stdout.decode('utf-8', errors='replace').splitlines()
    clean = '\n'.join(x for x in lines if not x.startswith('Using keyring backend'))
    if clean:
        print(clean)
    return child.returncode


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, KeyError):
        print('gwsx unavailable or misconfigured; credential contents withheld', file=sys.stderr)
        sys.exit(2)
