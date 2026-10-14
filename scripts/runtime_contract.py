"""Persistent native runtime config and exact installed Electron validator."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import install
import releases


def expected(root):
    return {'schema_version': 1, 'codex': {'mode': 'custom_launcher',
            'binary': str(root / 'bin/codex'), 'timeout_seconds': 1800,
            'required_contract': 'sbs-codex-v1'}}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def readback(root, user_data):
    file = install.safe_path(user_data / 'executor-runtime.json')
    details = file.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
        raise install.SetupError('Persistent executor-runtime must be an owned regular file with mode 0600')
    value = install.read_json(file)
    if value != expected(root):
        raise install.SetupError('Persistent executor-runtime must select the fixed SBS launcher, strict contract and timeout1800')
    return {'runtime_config_hash': fingerprint(value), 'runtime_config_path': str(file)}


def configure(root, user_data):
    # Called only with the worker stopped by installation/startup.
    install.write_json(user_data / 'executor-runtime.json', expected(root), root)
    return readback(root, user_data)


def inspect(root, app, user_data):
    """Validate the real descriptor with the pristine installed 0.3.9 module."""
    wanted = readback(root, user_data)
    module = releases.extract_module(app, 'lib/executor-runtime.cjs')
    if hashlib.sha256(module).hexdigest() != releases.MODULE_SHA256['lib/executor-runtime.cjs']:
        raise install.SetupError('Native executor-runtime validator differs from the reviewed contract')
    setup = install.read_json(root / 'lib/install-config.json')
    node, _, _ = install.entry_points(setup, root)
    script = install.trusted_path(root / 'lib/sbs-check-contract.mjs', root)
    if script.read_bytes() != (install.ASSETS / script.name).read_bytes():
        raise install.SetupError('Fixed contract inspector differs from this skill')
    with tempfile.TemporaryDirectory(prefix='native-contract-', dir=root) as directory:
        file = Path(directory) / 'executor-runtime.cjs'
        file.write_bytes(module)
        result = json.loads(install.run([node, script, file, user_data / 'executor-runtime.json'], timeout=30))
    if result.get('ok') is not True or result.get('runtime_config_hash') != wanted['runtime_config_hash']:
        raise install.SetupError('Native strict descriptor validation failed')
    return result
