"""Official stable release discovery and reviewed desktop-adapter contract."""
import hashlib
import json
from pathlib import Path
import plistlib
import re
import struct
from urllib.request import Request, urlopen

LATEST_API = 'https://api.github.com/repos/RudenkoAD/YaTorrent-releases/releases/latest'
REVIEWED_VERSION = '0.3.5'
WORKER_SHA256 = 'bcf174b56f10db6a8afd551d4af45658c321ad694783148287d4bbf378d2bc44'
MODULE_SHA256 = {
    'lib/auto-updater.cjs': '1fe50dd6ba6fd98ebb23c1ba86888777eb36e1327b5fecb4b20f35f195d0299f',
    'main.cjs': '0e989c988fb88de2eb1fea007ba8989339cb1394bcb96ee9031cac78ec6359f8',
    'lib/desktop-worker.cjs': '61e6cb3266571ca1e034626cfce27b7df294f94bcf8dae40ca4e54bfd9e45648',
    'lib/profile.cjs': '2151148ed3f01a220f618f480db946eff11b5fef6c6d3da451480ea54bf729fd',
    'lib/codex.cjs': '6246d22f1a83f6f65c9652b1363f28fc883d6d43df563fd34f851149878b448a',
}


class ReleaseError(ValueError):
    pass


def latest(metadata=None):
    if metadata is None:
        request = Request(LATEST_API, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'yatorrent-sbs-setup'})
        try:
            with urlopen(request, timeout=30) as response:
                metadata = json.loads(response.read(2 * 1024 * 1024))
        except (OSError, ValueError):
            raise ReleaseError('Cannot resolve official latest stable YaTorrent; no cached-version fallback') from None
    if not isinstance(metadata, dict) or metadata.get('draft') is not False or metadata.get('prerelease') is not False:
        raise ReleaseError('Expected a published stable official release')
    tag = metadata.get('tag_name', '')
    match = re.fullmatch(r'yatorrent-v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))', tag)
    if not match:
        raise ReleaseError('Unexpected official release tag')
    version = match[1]
    name = f'YaTorrent-{version}-arm64.dmg'
    source_assets = metadata.get('assets', [])
    if not isinstance(source_assets, list):
        raise ReleaseError('Invalid official release asset list')
    assets = [a for a in source_assets if isinstance(a, dict) and a.get('name') == name]
    if len(assets) != 1:
        raise ReleaseError('Latest release has no unique macOS arm64 DMG')
    asset = assets[0]
    expected_url = f'https://github.com/RudenkoAD/YaTorrent-releases/releases/download/{tag}/{name}'
    digest = asset.get('digest', '')
    if asset.get('browser_download_url') != expected_url or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ReleaseError('Official release asset URL or SHA-256 is missing or invalid')
    return {'version': version, 'tag': tag, 'url': expected_url, 'sha256': digest[7:], 'asset_name': name,
            'release_id': metadata.get('id'), 'source': LATEST_API}


def validate_source(app, release):
    app = Path(app)
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleShortVersionString') != release['version']:
        raise ReleaseError('Latest DMG contains an unexpected app version')
    worker = app / 'Contents/Resources/worker-runtime/aim2-worker'
    digest = hashlib.sha256()
    with worker.open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != WORKER_SHA256:
            raise ReleaseError('Latest worker binary differs from the reviewed adapter contract; review adapter before installation, no older-version fallback')
    with (app / 'Contents/Resources/app.asar').open('rb') as source:
        prefix = source.read(16)
        if len(prefix) != 16:
            raise ReleaseError('Invalid app.asar header')
        header_size, json_size = struct.unpack('<I', prefix[4:8])[0], struct.unpack('<I', prefix[12:16])[0]
        if not 0 < json_size <= header_size <= 16 * 1024 * 1024:
            raise ReleaseError('Invalid app.asar header size')
        header = json.loads(source.read(json_size))
        for name, expected in MODULE_SHA256.items():
            item = header
            for component in name.split('/'):
                item = item['files'][component]
            if item.get('unpacked') or item.get('link') or not 0 <= item['size'] <= 2 * 1024 * 1024:
                raise ReleaseError('Unsupported adapter module layout')
            source.seek(8 + header_size + int(item['offset']))
            if hashlib.sha256(source.read(item['size'])).hexdigest() != expected:
                raise ReleaseError('Latest desktop module differs from the reviewed adapter contract')
