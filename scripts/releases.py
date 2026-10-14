"""Official stable release discovery and pristine native runtime contract."""
import hashlib
import json
from pathlib import Path
import plistlib
import re
import struct
from urllib.request import Request, urlopen

LATEST_API = 'https://api.github.com/repos/RudenkoAD/YaTorrent-releases/releases/latest'
REVIEWED_VERSION = '0.3.9'
WORKER_SHA256 = '050e75587af4fb1da8f419d998f6b8fecde54910273aab8c5f9171e810cb0161'
MODULE_SHA256 = {
    'main.cjs': '0444a3b8be4cf0fc50dedef695cbd502df262f90a1014c3e44bb865793bb7849',
    'lib/desktop-worker.cjs': '1bbe01d2c2479b4adfcfcaea5f210ee91eb1c4e86b0cfa1791b1e1e9f82af752',
    'lib/profile.cjs': '9fb951ac419dceab4db18775b7dc5e83a95298ec92df99f553d91f4ea23077bf',
    'lib/codex.cjs': '59461932912eeb85ed65fe5eec026fc208e9833e322aaddfb71343c3e7aa146e',
    'lib/executor-runtime.cjs': 'dd11e5a81eaa3844592b42d9f2bf4310bda71aba57e9cee7a5796d3ec81a69f8',
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


def extract_module(app, name):
    if name not in MODULE_SHA256:
        raise ReleaseError('Unreviewed native module requested')
    with (Path(app) / 'Contents/Resources/app.asar').open('rb') as source:
        prefix = source.read(16)
        if len(prefix) != 16:
            raise ReleaseError('Invalid app.asar header')
        header_size, json_size = struct.unpack('<I', prefix[4:8])[0], struct.unpack('<I', prefix[12:16])[0]
        if not 0 < json_size <= header_size <= 16 * 1024 * 1024:
            raise ReleaseError('Invalid app.asar header size')
        item = json.loads(source.read(json_size))
        for component in name.split('/'):
            item = item['files'][component]
        if item.get('unpacked') or item.get('link') or not 0 <= item['size'] <= 2 * 1024 * 1024:
            raise ReleaseError('Unsupported native module layout')
        source.seek(8 + header_size + int(item['offset']))
        return source.read(item['size'])


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
        raise ReleaseError('Native worker differs from reviewed0.3.9; review compatibility before installation, no older-version fallback')
    for name, expected in MODULE_SHA256.items():
        if hashlib.sha256(extract_module(app, name)).hexdigest() != expected:
            raise ReleaseError('Native desktop module differs from reviewed0.3.9')
    if (app / 'Contents/Resources/worker-runtime/aim2-worker-original').exists():
        raise ReleaseError('App contains a legacy patched worker; use a pristine official application')
