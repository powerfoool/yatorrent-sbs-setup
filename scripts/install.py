#!/usr/bin/env python3
"""Latest stable YaTorrent with pinned private tools. Default is a nonmutating plan."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import releases

VERSION = releases.REVIEWED_VERSION
NODE_VERSION = "24.21.0"
CODEX_VERSION = "0.162.1"
PLAYWRIGHT_VERSION = "1.64.0"
CHROMIUM_VERSION = "156.0.8078.4"
NPM_INTEGRITY = {
    "@openai/codex": "sha512-NWZdi/kxyjv/8EUGFupziGU38YyleugZRM4JXgY5XFH7FUmaFA33NZS2Bmq0HPazf7S3jJQQWsZ/jAK9jsrV3Q==",
    "playwright": "sha512-kVzTnYFYEyQ9RGzBjNCd2+yEqWmVbxl3AHPynv3qQ6JTjv4a+kwRcnqQwIpuP3NnrnIrE/Q6wWAIAvMfIE2/Yg==",
}
NODE_URL = "https://nda.ya.ru/t/IrAg7GvG7ub4ns"  # Official pinned Node archive; HTTPS redirect and SHA256 verified
NODE_SHA256 = "bed7eea5325e1108f32ce5228ddd6a5f0f08a499ee42aa7442aea583702f6057"
ASSETS = Path(__file__).resolve().parents[1] / "assets"
DEFAULT_ROOT = Path.home() / ".local/share/yatorrent-sbs"
COMMAND_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


class SetupError(Exception):
    pass


def digest(file):
    h = hashlib.sha256()
    with Path(file).open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_path(path):
    """Reject symlinks in a selected output path and its existing parents."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    # macOS exposes these fixed OS aliases. Normalize only these aliases, never
    # a user-selected symlink (which must remain visible to the checks below).
    for alias, target in (("/tmp", "/private/tmp"), ("/var", "/private/var")):
        alias_path = Path(alias)
        if alias_path.is_symlink() and os.path.realpath(alias) == target:
            if str(path) == alias or str(path).startswith(alias + "/"):
                path = Path(target + str(path)[len(alias):])
                break
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise SetupError("Selected output path contains a symlink")
    return path


def read_json(file):
    try:
        value = json.loads(Path(file).read_text())
    except (ValueError, OSError):
        raise SetupError("Expected JSON file is missing or invalid") from None
    if not isinstance(value, dict):
        raise SetupError("Expected a JSON object")
    return value


def run(command, env=None, timeout=900, cwd=None):
    # Never display command output: app imports and auth tools may include secrets.
    try:
        result = subprocess.run([str(x) for x in command], env=env, cwd=cwd,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("Required command unavailable or timed out") from None
    if result.returncode:
        raise SetupError(f"Required command failed (exit {result.returncode}); original output withheld")
    return result.stdout.decode("utf-8", errors="replace")


def assert_stopped():
    output = run(["/bin/ps", "-axo", "comm="], timeout=20)
    for command in output.splitlines():
        name = Path(command.strip()).name.lower()
        if name.startswith("yatorrent") or name.startswith("aim2-worker"):
            raise SetupError("Quit YaTorrent and all AIM2 workers before applying any changes")


def backup(file, root):
    if not file.exists():
        return
    base = root / "backups" / time.strftime("%Y%m%dT%H%M%S")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(str(file).encode()).hexdigest()[:12] + "-" + file.name
    target = base / name
    if target.exists():
        target = base / (uuid.uuid4().hex[:8] + "-" + name)
    if file.is_dir():
        shutil.copytree(file, target, symlinks=True)
    else:
        shutil.copy2(file, target)
        target.chmod(0o600)


def write_owned(file, content, root, mode=0o600):
    file = safe_path(file)
    data = content.encode() if isinstance(content, str) else content
    if file.exists() and file.read_bytes() == data:
        file.chmod(mode)
        return False
    backup(file, root)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".sbs-", dir=file.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as dest:
            dest.write(data)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(temporary, file)
        directory_fd = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def write_json(file, value, root):
    return write_owned(file, json.dumps(value, indent=2, sort_keys=True) + "\n", root)


def download(url, sha256, file):
    if file.exists():
        if digest(file) != sha256:
            raise SetupError("Cached download checksum mismatch; preserve and inspect it")
        return
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = file.with_name(file.name + ".part-" + uuid.uuid4().hex)
    try:
        run(["/usr/bin/curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
             "--tlsv1.2", "--retry", "2", "--output", temp, url], timeout=1800)
        if digest(temp) != sha256:
            raise SetupError("Downloaded artifact checksum mismatch")
        os.replace(temp, file)
    finally:
        if temp.exists():
            temp.unlink()


def extract_node(archive, dest):
    """Manual extraction, one pinned top directory, no traversal or escaping links."""
    prefix = f"node-v{NODE_VERSION}-darwin-arm64"
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for item in members:
            name = PurePosixPath(item.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != prefix:
                raise SetupError("Unsafe Node archive path")
            if not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise SetupError("Unsupported Node archive member")
            if item.issym() or item.islnk():
                link = PurePosixPath(item.linkname)
                target = name.parent / link if item.issym() else link
                # Normalize lexically before checking the first component.
                parts = []
                for part in target.parts:
                    if part == "..":
                        if not parts:
                            raise SetupError("Escaping Node archive link")
                        parts.pop()
                    elif part not in ("", "."):
                        parts.append(part)
                if link.is_absolute() or not parts or parts[0] != prefix:
                    raise SetupError("Escaping Node archive link")
        dest.mkdir(mode=0o700)
        # Regular content first, symlinks only at the end: no write through symlink.
        for item in sorted(members, key=lambda m: m.issym() or m.islnk()):
            relative = PurePosixPath(item.name).relative_to(prefix)
            if not relative.parts:
                continue
            target = dest.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if any(p.is_symlink() for p in [target, *target.parents] if p != dest.parent):
                raise SetupError("Node archive writes through a symlink")
            if item.isdir():
                target.mkdir(exist_ok=True)
                target.chmod(item.mode & 0o777)
            elif item.isfile():
                with tar.extractfile(item) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)
                target.chmod(item.mode & 0o777)
            elif item.issym():
                target.symlink_to(item.linkname)
            else:
                link = PurePosixPath(item.linkname).relative_to(prefix)
                os.link(dest.joinpath(*link.parts), target)


def shell_wrapper(node, script, role=None):
    fixed = " " + shlex.quote(role) if role is not None else ""
    return "#!/bin/sh\nexec " + shlex.quote(str(node)) + " " + shlex.quote(str(script)) + fixed + ' "$@"\n'


def rule_text(node, browser, reader=None):
    reader = Path(reader) if reader is not None else Path(browser).with_name("sbs-read-prompt.mjs")
    return "# Fixed SBS Chromium launcher and technical compatibility reader only.\n" + \
        f"prefix_rule(pattern={json.dumps([str(node), str(browser)])}, decision=\"allow\")\n" + \
        f"prefix_rule(pattern={json.dumps([str(node), str(reader)])}, decision=\"allow\")\n"


def check_rule(real_codex, rule, node, browser, env, reader=None):
    reader = Path(reader) if reader is not None else Path(browser).with_name("sbs-read-prompt.mjs")
    allowed = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", node, browser, "--check"], env=env, timeout=45))
    generic = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", node, "-e", "process.exit(0)"], env=env, timeout=45))
    foreman = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", node, reader, "foreman"], env=env, timeout=45))
    scout = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", node, reader, "scout"], env=env, timeout=45))
    arbitrary = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", node, "/tmp/arbitrary.mjs"], env=env, timeout=45))
    shell = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", "/bin/sh", "-c", "true"], env=env, timeout=45))
    cat = json.loads(run([real_codex, "execpolicy", "check", "--rules", rule, "--", "/bin/cat", "prompts/foreman.md"], env=env, timeout=45))
    if any(result.get("decision") != "allow" for result in (allowed, foreman, scout)) or any(result.get("decision") == "allow" for result in (generic, arbitrary, shell, cat)):
        raise SetupError("Exact browser/prompt-reader allow rule validation failed")


def bundle_digest(app):
    h = hashlib.sha256()
    for file in sorted(app.rglob("*")):
        relative = str(file.relative_to(app)).encode()
        h.update(relative + b"\0")
        if file.is_symlink():
            h.update(b"link\0" + os.readlink(file).encode())
        elif file.is_file():
            h.update(b"file\0" + digest(file).encode())
    return h.hexdigest()


def validate_existing_app(app, marker):
    if not app.exists():
        return False
    if not marker.exists():
        raise SetupError("Existing YaTorrent app is not owned by this installer; choose a separate app path")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    saved = read_json(marker)
    if info.get("CFBundleShortVersionString") != saved.get("app_version"):
        raise SetupError("Existing app version differs; native compatibility must be re-checked after updates")
    if saved.get("app") != str(app) or saved.get("bundle_sha256") != bundle_digest(app):
        raise SetupError("Existing app changed or is unknown; preserve it and re-check native compatibility")
    return True


def public_profile(source):
    # Only public fields are retained in memory for comparison, never credential values.
    value = read_json(source)
    value.pop("credentials", None)
    if value.get("version") != 1 or not isinstance(value.get("broker"), dict) or not value.get("executors", {}).get("codex", {}).get("model"):
        raise SetupError("Profile must be AIM2 version 1 with broker and Codex model")
    return value


def external_profile(source):
    if source is None:
        raise SetupError("External AIM2 profile required: use --profile PATH; credentials are not bundled")
    source = safe_path(source)
    package = ASSETS.parent.resolve()
    if source == package or package in source.parents:
        raise SetupError("AIM2 must be supplied outside the published skill directory")
    try:
        details = source.stat()
    except OSError:
        raise SetupError("External AIM2 profile is unavailable") from None
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600 or details.st_size > 1024 * 1024:
        raise SetupError("External AIM2 must be an owned regular file of at most 1 MiB with permissions 0600")
    public_profile(source)
    return source



def command_path(path):
    """A canonical absolute path safe to spell literally in a Codex command."""
    original = os.path.abspath(os.path.expanduser(str(path)))
    if not COMMAND_PATH.fullmatch(original):
        raise SetupError("Installation entry paths must match ^/[A-Za-z0-9_./-]+$; choose an explicit safe root (no spaces or shell metacharacters)")
    path = safe_path(original)
    if not COMMAND_PATH.fullmatch(str(path)):
        raise SetupError("Canonical installation entry path is not safe for an unquoted command")
    return path


def trusted_path(path, root, require_existing=True, executable=False):
    """Check trust before launch, allowing root-owned OS ancestors only."""
    root = command_path(root)
    path = command_path(path)
    if path != root and root not in path.parents:
        raise SetupError("Installation entry point is outside the selected root")
    uid = os.getuid()
    for item in [path, *path.parents]:
        try:
            details = item.lstat()
        except FileNotFoundError:
            if item == path and require_existing:
                raise SetupError("Required trusted installation entry point is missing") from None
            continue
        if stat.S_ISLNK(details.st_mode):
            raise SetupError("Trusted installation entry path contains a symlink")
        inside_root = item == root or root in item.parents
        allowed_owners = (uid,) if inside_root else (0, uid)
        if details.st_uid not in allowed_owners:
            raise SetupError("Trusted installation entry path has an unexpected owner")
        if details.st_mode & 0o022:
            raise SetupError("Trusted installation entry path is group- or world-writable")
        if item == path and path != root:
            if not stat.S_ISREG(details.st_mode):
                raise SetupError("Trusted installation entry point must be a real regular file")
            if executable and not details.st_mode & stat.S_IXUSR:
                raise SetupError("Trusted private Node entry point must be executable")
        elif not stat.S_ISDIR(details.st_mode):
            raise SetupError("Trusted installation ancestor must be a real directory")
    return path


def entry_points(setup, root):
    """Verifier contract: permanent real Node + fixed browser/reader scripts."""
    root = trusted_path(root, root)
    expected_node = root / "node/bin/node"
    node = command_path(setup.get("private_node", str(expected_node)))
    if node != expected_node or ("node_bin" in setup and command_path(setup["node_bin"]) != expected_node):
        raise SetupError("Private Node entry point differs from the fixed installation path")
    return (trusted_path(node, root, executable=True),
            trusted_path(root / "lib/sbs-chromium.mjs", root),
            trusted_path(root / "lib/sbs-read-prompt.mjs", root))


def launcher_commands(root, setup=None, require_existing=False):
    root = command_path(root)
    if require_existing:
        node, browser, reader = entry_points(setup or {}, root)
    else:
        trusted_path(root, root, require_existing=False)
        node = trusted_path(root / "node/bin/node", root, require_existing=False, executable=True)
        browser = trusted_path(root / "lib/sbs-chromium.mjs", root, require_existing=False)
        reader = trusted_path(root / "lib/sbs-read-prompt.mjs", root, require_existing=False)
    node, browser, reader = map(str, (node, browser, reader))
    # Do not use shlex.join: quoting these safe paths triggers the diagnosed
    # Codex/macOS launch failure even when the argv policy prefix is identical.
    return {"browser_command": [node, browser],
            "browser_command_string": node + " " + browser,
            "prompt_reader_command": [node, reader],
            "prompt_reader_command_string": node + " " + reader,
            "prompt_reader_examples": {role: [node, reader, role] for role in ("foreman", "scout")},
            "prompt_reader_example_strings": {role: node + " " + reader + " " + role for role in ("foreman", "scout")}}


def plan(args):
    args.root = command_path(args.root)
    try:
        release = releases.latest()
    except releases.ReleaseError as error:
        raise SetupError(str(error)) from None
    return {"mode": "plan", "mutates": False, "platform_required": "macOS arm64", "root": str(args.root),
            "app": str(args.app), "user_data": str(args.user_data), "auth_home": str(args.auth_home),
            "pins": {"node": NODE_VERSION, "yatorrent": release["version"], "codex": CODEX_VERSION, "playwright": PLAYWRIGHT_VERSION},
            "release": release, "startup": {"consume_codex": True, "autoStart": True, "launch_after_profile": True},
            "operations": ["resolve official latest stable YaTorrent and verify asset SHA256/native contract; keep private Node pin", "install isolated Codex and full Playwright Chromium",
                           "copy user-supplied external AIM2 into private runtime with mode 0600", "install/reuse pristine official YaTorrent without app or worker patches",
                           "verify existing application signature; preserve Gatekeeper and original app bytes", "write separate exact Chromium and technical reader rules and verify execpolicy", "write strict native executor-runtime.json with custom launcher and timeout1800",
                           "import profile with worker stopped if necessary", "confirm encrypted profile and auth; enable Codex and worker auto-start; verify infrastructure before app launch; confirm actual worker PID/config/timeout/heartbeat after launch"],
            "limits": {"worker_concurrency": args.worker_concurrency, "worker_timeout_seconds": 1800, "scout_threads": 10, "total_agent_slots": 11},
            "profile_required": args.profile is None, "profile_bundled": False,
            "broker_intake_started": False, "clean_machine_install_tested": False,
            **launcher_commands(args.root)}


def apply(args):
    if sys.version_info < (3, 10):
        raise SetupError('Python 3.10 or newer required; select supported Python before installation')
    # Complete entry-path preflight before process commands, mutations or network.
    args.root = command_path(args.root)
    launcher_commands(args.root)
    args.profile = external_profile(args.profile)
    if safe_path(args.user_data) != safe_path(Path.home() / "Library/Application Support/YaTorrent"):
        raise SetupError("Custom --user-data relocation is unsupported for app launch; use the native YaTorrent directory")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SetupError("Only macOS Apple Silicon (arm64) is supported by this verified release")
    for p in (args.root, args.app, args.auth_home, args.user_data):
        safe_path(p)
    assert_stopped()
    root, app = args.root, args.app
    marker = root / "installation.json"
    app_owned = False
    if marker.exists() and app.exists():
        saved = read_json(marker)
        actual = plistlib.loads((app / 'Contents/Info.plist').read_bytes()).get('CFBundleShortVersionString')
        if saved.get('app') == str(app) and actual == saved.get('app_version'):
            app_owned = validate_existing_app(app, marker)
    try:
        release = releases.latest()
    except releases.ReleaseError as error:
        raise SetupError(str(error)) from None
    app_reused = False
    if app.exists():
        actual_version = plistlib.loads((app / "Contents/Info.plist").read_bytes()).get("CFBundleShortVersionString")
        legacy = (app / "Contents/Resources/worker-runtime/aim2-worker-original").exists()
        if actual_version == release["version"] and not legacy:
            try:
                releases.validate_source(app, release)
            except releases.ReleaseError as error:
                raise SetupError(str(error)) from None
            run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], timeout=180)
            app_reused = True
        elif not app_owned:
            raise SetupError("Existing app is not a reviewed native installation; preserve it and choose an empty app path")
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise SetupError("Existing installation root is unknown; use an empty directory")
    for name in ("sbs-chromium.mjs", "sbs-read-prompt.mjs", "codex-launcher.mjs", "sbs-auth-guard.mjs", "sbs-check-contract.mjs", "sbs-task-instructions.mjs", "sbs-url-budget.mjs"):
        if not (ASSETS / name).is_file():
            raise SetupError("Required bundled asset missing")
    public_profile(args.profile)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    # Record a recoverable partial installation before network operations.
    if not marker.exists():
        write_json(marker, {"state": "preparing", "app": str(app), "app_version": VERSION}, root)
    node = root / "node/bin/node"
    env = dict(os.environ, PATH=str(root / "node/bin") + os.pathsep + os.environ.get("PATH", ""),
               PLAYWRIGHT_BROWSERS_PATH=str(root / "browsers"))
    if node.exists():
        trusted_path(node, root, executable=True)
        if run([node, "--version"], env=env, timeout=20).strip() != "v" + NODE_VERSION:
            raise SetupError("Existing private Node version differs")
    else:
        archive = root / "cache" / f"node-v{NODE_VERSION}-darwin-arm64.tar.gz"
        download(NODE_URL, NODE_SHA256, archive)
        staging = root / (".node-" + uuid.uuid4().hex)
        try:
            extract_node(archive, staging)
            if (root / "node").exists():
                raise SetupError("Unexpected partial Node directory; preserve it and inspect")
            os.replace(staging, root / "node")
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    trusted_path(node, root, executable=True)
    npm = root / "node/lib/node_modules/npm/bin/npm-cli.js"
    for prefix, package, version in [(root / "codex", "@openai/codex", CODEX_VERSION), (root / "browser-runtime", "playwright", PLAYWRIGHT_VERSION)]:
        installed = prefix / "node_modules" / package / "package.json"
        if installed.exists():
            if read_json(installed).get("version") != version:
                raise SetupError("Existing isolated npm dependency version differs")
        else:
            prefix.mkdir(exist_ok=True, mode=0o700)
            write_json(prefix / "package.json", {"private": True, "dependencies": {package: version}}, root)
            run([node, npm, "install", "--prefix", prefix, "--omit=dev", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"], env=env, timeout=1800)
            if read_json(installed).get("version") != version:
                raise SetupError("Installed dependency version does not match exact pin")
        lock = read_json(prefix / "package-lock.json")
        if lock.get("packages", {}).get("node_modules/" + package, {}).get("integrity") != NPM_INTEGRITY[package]:
            raise SetupError("Pinned npm package integrity differs from verified release")
    run([node, root / "browser-runtime/node_modules/playwright/cli.js", "install", "chromium", "--no-shell"], env=env, timeout=1800)
    if not app_reused:
        dmg = root / "cache" / release["asset_name"]
        download(release["url"], release["sha256"], dmg)
        mount = root / (".mount-" + uuid.uuid4().hex)
        mount.mkdir(mode=0o700)
        app.parent.mkdir(parents=True, exist_ok=True)
        staging = app.with_name(".YaTorrent-sbs-" + uuid.uuid4().hex + ".app")
        attached = False
        try:
            run(["/usr/bin/hdiutil", "attach", dmg, "-readonly", "-nobrowse", "-mountpoint", mount], timeout=120)
            attached = True
            source = mount / "YaTorrent.app"
            info = plistlib.loads((source / "Contents/Info.plist").read_bytes())
            if info.get("CFBundleShortVersionString") != release["version"]:
                raise SetupError("Verified DMG contains unexpected app version")
            try:
                releases.validate_source(source, release)
            except releases.ReleaseError as error:
                raise SetupError(str(error)) from None
            run(["/usr/bin/ditto", source, staging], timeout=600)
            assert_stopped()
            if app.exists():
                if not app_owned:
                    raise SetupError("App appeared during setup; preserve it and retry after review")
                validate_existing_app(app, marker)
                retired = root / "backups" / ("upgrade-" + uuid.uuid4().hex + ".app")
                retired.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(app, retired)
                try:
                    os.replace(staging, app)
                except OSError:
                    os.replace(retired, app)
                    raise
            else:
                os.replace(staging, app)
        finally:
            if attached:
                run(["/usr/bin/hdiutil", "detach", mount], timeout=120)
            if mount.exists():
                mount.rmdir()
            if staging.exists():
                shutil.rmtree(staging)
    assert_stopped()
    # From this point on all writes are outside the app bundle. Native worker stays pristine.
    worker = app / "Contents/Resources/worker-runtime/aim2-worker"
    try:
        releases.validate_source(app, release)
    except releases.ReleaseError as error:
        raise SetupError(str(error)) from None
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], timeout=180)
    for name in ("sbs-chromium.mjs", "sbs-read-prompt.mjs", "codex-launcher.mjs", "sbs-auth-guard.mjs", "sbs-check-contract.mjs", "sbs-task-instructions.mjs", "sbs-url-budget.mjs"):
        write_owned(root / "lib" / name, (ASSETS / name).read_bytes(), root)
    write_owned(root / "profiles/databroker.aim2", args.profile.read_bytes(), root)
    write_json(root / "lib/runtime-config.json", {"runtime": str(root / "browser-runtime"), "browsers_path": str(root / "browsers")}, root)
    # Native readiness runs real_codex directly with a minimal PATH: make the
    # private Node explicit instead of relying on the npm shim's env shebang.
    npm_codex = root / "codex/node_modules/@openai/codex/bin/codex.js"
    real_codex = root / "bin/codex-real"
    write_owned(real_codex, shell_wrapper(node, npm_codex), root, 0o755)
    write_owned(root / "bin/codex", shell_wrapper(node, root / "lib/codex-launcher.mjs"), root, 0o755)
    write_owned(root / "bin/browser", shell_wrapper(node, root / "lib/sbs-chromium.mjs"), root, 0o755)
    for role in ("foreman", "scout"):
        write_owned(root / "bin" / (role + "-reader"), shell_wrapper(node, root / "lib/sbs-read-prompt.mjs", role), root, 0o755)
    commands = launcher_commands(root, require_existing=True)
    write_json(root / "lib/install-config.json", {**commands, "real_codex": str(real_codex), "auth_home": str(args.auth_home),
               "rule_dir": str(args.auth_home / "rules"), "scout_threads": 10, "private_node": str(node), "node_bin": str(node),
               "real_worker": str(worker), "real_worker_sha256": digest(worker), "codex_wrapper": str(root / "bin/codex"),
               "app": str(app), "app_version": release["version"],
               "worker_timeout_seconds": 1800, "worker_concurrency": args.worker_concurrency}, root)
    browser = root / "lib/sbs-chromium.mjs"
    rule = args.auth_home / "rules/yatorrent-sbs-chromium.rules"
    write_owned(rule, rule_text(node, browser), root)
    check_rule(real_codex, rule, node, browser, env)
    write_json(marker, {"state": "prepared", "app": str(app), "app_version": release["version"],
               "release": release, "root": str(root), "auth_home": str(args.auth_home), "user_data": str(args.user_data), "profile_sha256": digest(args.profile),
               "install_mode": "native", "real_worker_sha256": digest(worker), "bundle_sha256": bundle_digest(app),
               "pins": {"node": NODE_VERSION, "codex": CODEX_VERSION, "playwright": PLAYWRIGHT_VERSION}, **commands}, root)
    import startup
    try:
        launch = startup.start(root)
    except startup.install.SetupError as error:
        raise SetupError(str(error)) from None
    print(json.dumps({"installation": "prepared", "verification_required": not launch.get("full_verify_passed", False),
                      **commands, **launch}))



def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Show nonmutating setup plan (default)")
    mode.add_argument("--apply", action="store_true", help="Install, import profile, verify, then launch app with Codex intake and worker auto-start")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--app", type=Path, default=Path.home() / "Applications/YaTorrent.app")
    parser.add_argument("--auth-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--user-data", type=Path, default=Path.home() / "Library/Application Support/YaTorrent", help="Native app data directory; --apply cannot relocate Electron data")
    parser.add_argument("--profile", type=Path, help="External private AIM2 profile (required for --apply)")
    parser.add_argument("--worker-concurrency", type=int, choices=range(1, 17), default=4, help="Concurrent worker jobs and max_browsers (default 4)")
    args = parser.parse_args(argv)
    for key in ("root", "app", "auth_home", "user_data", "profile"):
        if getattr(args, key) is not None:
            setattr(args, key, Path(os.path.abspath(os.path.expanduser(str(getattr(args, key))))))
    return args


def main(argv=None):
    try:
        args = arguments(argv)
        if args.apply:
            apply(args)
        else:
            print(json.dumps(plan(args), indent=2))
    except (SetupError, OSError, ValueError, plistlib.InvalidFileException):
        # Display only controlled errors; file/JSON data can contain credentials.
        exc = sys.exc_info()[1]
        print("Setup stopped: " + (str(exc) if isinstance(exc, SetupError) else "filesystem or format check failed; originals preserved"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
