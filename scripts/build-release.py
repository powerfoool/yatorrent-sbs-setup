#!/usr/bin/env python3
"""Public release builder: explicit inventory, no private profiles or prior ZIPs."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import stat
import zipfile

FILES = tuple('''SKILL.md
README.md
.gitignore
version.json
agents/openai.yaml
prompts/foreman.md
prompts/scout.md
schemas/default-final.schema.json
assets/codex-launcher.mjs
assets/sbs-task-instructions.mjs
assets/sbs-url-budget.mjs
assets/sbs-navigation-transaction.mjs
assets/sbs-auth-guard.mjs
assets/sbs-chromium.mjs
assets/sbs-read-prompt.mjs
assets/sbs-check-contract.mjs
assets/gwsx.py
assets/coordinator-validate.py
references/coordinator.md
references/browser.md
references/broker.md
references/profile.md
references/prompts.md
references/url-visit.md
references/runtime.md
references/setup.md
scripts/coordinator_prompts.py
scripts/test-task-instructions.py
scripts/instruction_smoke.py
scripts/test-instruction-smoke.py
scripts/test-url-budget.py
scripts/test-navigation.py
scripts/serp_urls.py
scripts/coordinator_setup.py
scripts/coordinator_data.py
scripts/test-coordinator.py
scripts/build-release.py
scripts/install.py
scripts/releases.py
scripts/runtime_smoke.py
scripts/runtime_contract.py
scripts/test-native-runtime.py
scripts/startup.py
scripts/test-install.py
scripts/test-runtime-smoke.py
scripts/test-startup.py
scripts/test-worker-check.py
scripts/test-public-release.py
scripts/test-auth-cleanup.py
scripts/verify.py
scripts/worker_check.py'''.splitlines())


def inventory(root):
    found = set()
    for p in root.rglob('*'):
        name = p.relative_to(root).as_posix()
        if '__pycache__' in p.parts or '.git' in p.parts:
            continue
        if p.is_symlink():
            raise ValueError('Symlinks are forbidden in a public release')
        if p.is_file() and name != 'MANIFEST.sha256':
            found.add(name)
    if found != set(FILES):
        raise ValueError('Unexpected or missing public release files')
    for name in FILES:
        value = (root / name).read_text(encoding='utf-8')
        if re.search(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|y0__[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', value):
            raise ValueError('Credential-shaped content found; release blocked')
        if re.search(r'/Users/(?:emc2|papa)(?:/|\b)|memory' + '-bank', value):
            raise ValueError('Author-machine dependency found; release blocked')
    return FILES


def build(root, output):
    root = root.resolve()
    files = inventory(root)
    if output.resolve() == root or root in output.resolve().parents:
        raise ValueError('Release output must be outside the skill directory')
    manifest = ''.join(hashlib.sha256((root / name).read_bytes()).hexdigest() + '  ' + name + '\n' for name in sorted(files))
    (root / 'MANIFEST.sha256').write_text(manifest)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted((*files, 'MANIFEST.sha256')):
            info = zipfile.ZipInfo('yatorrent-sbs-setup/' + name, (2026, 10, 14, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (root / name).read_bytes())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP integrity failed')
        for line in manifest.splitlines():
            expected, name = line.split('  ', 1)
            if hashlib.sha256(archive.read('yatorrent-sbs-setup/' + name)).hexdigest() != expected:
                raise ValueError('Manifest verification failed')
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + '.sha256').write_text(sha + '  ' + output.name + '\n')
    return {'archive': output.name, 'sha256': sha, 'members': len(files) + 1, 'credentials_bundled': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build(Path(__file__).resolve().parents[1], args.output)))
    except (ValueError, OSError):
        parser.exit(1, 'Public release blocked: inventory, secret pattern or integrity check failed\n')
