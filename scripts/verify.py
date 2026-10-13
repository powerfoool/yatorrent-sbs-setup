#!/usr/bin/env python3
"""Readback for an installed prefix; never starts broker intake or prints credentials."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from urllib.parse import urlparse

# Same-directory import keeps verification pins identical to the installer.
import install
import runtime_smoke
import worker_check


def normalized_public(file, stored=False):
    value = install.read_json(file)
    if stored:
        if value.get("version") != 1 or not isinstance(value.get("profile"), dict) or not isinstance(value.get("credentials_encrypted"), str) or not value.get("credentials_encrypted"):
            raise install.SetupError("Imported encrypted profile has unexpected format")
        value = dict(value["profile"])
        if "credentials" in value:
            raise install.SetupError("Imported profile contains plaintext credential fields")
    else:
        value.pop("credentials", None)
    # Normalize the reviewed public transformations of YaTorrent 0.3.5.
    if isinstance(value.get("broker"), dict):
        value["broker"] = dict(value["broker"])
        value["broker"]["url"] = value["broker"]["url"].rstrip("/")
        value["broker"].setdefault("allow_http", False)
    if isinstance(value.get("s3"), dict) and "endpoint_url" in value["s3"]:
        value["s3"] = dict(value["s3"])
        value["s3"]["endpoint_url"] = value["s3"]["endpoint_url"].rstrip("/")
    for kind, settings in value.get("executors", {}).items():
        if kind == "codex" and settings.get("auth_mode") == "existing":
            settings["auth_mode"] = "default"
        for key in ("url", "proxy_url"):
            if key in settings:
                settings[key] = settings[key].rstrip("/")
        if "material_hosts" in settings:
            hosts = settings["material_hosts"]
            hosts = hosts.split(",") if isinstance(hosts, str) else hosts
            settings["material_hosts"] = list(dict.fromkeys(str(x).strip().lower() for x in hosts if str(x).strip()))
    if isinstance(value.get("limits"), dict):
        value["limits"].setdefault("max_browsers", 4)
    return value


def command_result(command, env, timeout=60):
    try:
        return subprocess.run([str(x) for x in command], env=env, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def authentication_readback(real_codex, auth_home, env, skip_login=False):
    # Inspect presence only. Never read credentials or serialize auth command output.
    file_present = (auth_home / "auth.json").is_file()
    if skip_login:
        return {"codex_auth": "skipped", "file_auth_present": file_present}
    login = command_result([real_codex, "login", "status"], env, timeout=30)
    return {"codex_auth": "logged_in" if login is not None and login.returncode == 0 else "login_required_or_status_unavailable",
            "file_auth_present": file_present}


def authentication_ready(checks):
    return checks["codex_auth"] == "logged_in" and checks["file_auth_present"]


def worker_readback(user_data, setup):
    return worker_check.wait_ready(user_data, setup)


def verify(args):
    root = args.root
    if args.runtime_only:
        result = runtime_smoke.run_runtime_smoke(root, args.runtime_timeout)
        result.update(diagnostic_only=True, delivery_ready=False, status="не готов")
        print(json.dumps(result, indent=2))
        return 0 if result['ok'] else 1
    marker = install.read_json(root / "installation.json")
    if marker.get("state") != "prepared":
        raise install.SetupError("Installation is incomplete")
    app = Path(marker["app"])
    user_data = args.user_data or Path(marker["user_data"])
    install.validate_existing_app(app, root / "installation.json")
    setup = install.read_json(root / "lib/install-config.json")
    node, browser, reader = install.entry_points(setup, root)
    real_codex = Path(setup["real_codex"])
    expected_commands = install.launcher_commands(root, setup, require_existing=True)
    if any(setup.get(key) != value or marker.get(key) != value for key, value in expected_commands.items()):
        raise install.SetupError("Stored command readback differs from trusted installed entry points")
    auth_home = Path(setup["auth_home"])
    if not reader.is_file() or reader.is_symlink() or reader.read_bytes() != (install.ASSETS / "sbs-read-prompt.mjs").read_bytes():
        raise install.SetupError("Fixed role prompt reader asset mismatch")
    env = dict(os.environ, PATH=str(node.parent) + os.pathsep + os.environ.get("PATH", ""),
               CODEX_HOME=str(auth_home), PLAYWRIGHT_BROWSERS_PATH=str(root / "browsers"))
    checks = {"installation": "prepared", "worker_ready": False, "broker_intake_started_by_verify": False}
    if install.run([node, "--version"], env=env, timeout=20).strip() != "v" + install.NODE_VERSION:
        raise install.SetupError("Private Node pin mismatch")
    for name, directory, expected in [("codex", root / "codex/node_modules/@openai/codex", install.CODEX_VERSION),
                                       ("playwright", root / "browser-runtime/node_modules/playwright", install.PLAYWRIGHT_VERSION)]:
        if install.read_json(directory / "package.json").get("version") != expected:
            raise install.SetupError("Installed package pin mismatch")
    original = Path(setup["real_worker"])
    if install.digest(original) != setup.get("real_worker_sha256") or setup["real_worker_sha256"] != marker.get("real_worker_sha256"):
        raise install.SetupError("Preserved worker digest mismatch")
    concurrency = setup.get("worker_concurrency")
    if setup.get("scout_threads") != 10 or setup.get("worker_timeout_seconds") != 1800 or type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise install.SetupError("Agent/worker limits mismatch")
    expected_worker = install.shell_wrapper(node, root / "lib/worker-launcher.mjs")
    if (app / "Contents/Resources/worker-runtime/aim2-worker").read_text() != expected_worker:
        raise install.SetupError("Desktop adapter wrapper mismatch")
    if (root / "bin/codex").read_text() != install.shell_wrapper(node, root / "lib/codex-launcher.mjs") or (app / "Contents/Resources/codex/codex").read_text() != install.shell_wrapper(node, root / "lib/codex-launcher.mjs"):
        raise install.SetupError("Private Codex wrapper mismatch")
    for name in ("codex-launcher.mjs", "sbs-auth-guard.mjs"):
        installed = install.trusted_path(root / "lib" / name, root)
        if installed.read_bytes() != (install.ASSETS / name).read_bytes():
            raise install.SetupError("Codex launcher or detached auth cleanup guard differs from this release")
    runtime = install.read_json(root / "lib/runtime-config.json")
    if runtime != {"runtime": str(root / "browser-runtime"), "browsers_path": str(root / "browsers")}:
        raise install.SetupError("Private browser runtime paths mismatch")
    rule = auth_home / "rules/yatorrent-sbs-chromium.rules"
    if rule.read_text() != install.rule_text(node, browser):
        raise install.SetupError("Exact browser rule mismatch")
    install.check_rule(real_codex, rule, node, browser, env)
    checks["execpolicy"] = "exact_browser_and_both_prompt_reader_roles_allowed; generic_node_and_cat_not_allowed_by_this_rule"
    checks.update(expected_commands)
    profile = root / "profiles/databroker.aim2"
    if stat.S_IMODE(profile.stat().st_mode) != 0o600:
        raise install.SetupError("Profile permissions must be 0600")
    if install.digest(profile) != marker.get("profile_sha256"):
        raise install.SetupError("Installed profile differs from its installation receipt")
    if args.profile is not None and install.digest(profile) != install.digest(install.external_profile(args.profile)):
        raise install.SetupError("Installed AIM2 profile is not byte-exact")
    imported = user_data / "profile.json"
    deadline = time.monotonic() + args.wait_import
    source_public = normalized_public(profile)
    matched = False
    while True:
        if imported.exists():
            matched = source_public == normalized_public(imported, stored=True)
        if matched or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if not matched:
        raise install.SetupError("Supported encrypted profile import has not been confirmed; check the app and user-data path")
    if stat.S_IMODE(imported.stat().st_mode) != 0o600:
        raise install.SetupError("Imported profile permissions must be 0600")
    receipt = install.read_json(install.safe_path(root / "profile-import.json"))
    if (receipt.get("source_sha256") != install.digest(profile)
            or receipt.get("encrypted_file_sha256") != install.digest(imported)):
        raise install.SetupError("Current profile import receipt missing or stale; rerun startup.py")
    checks["profile_import"] = "encrypted_storage_present; public_fields_match; source_byte_exact"
    preferences = install.read_json(user_data / "desktop.json")
    settings = preferences.get("settings", {})
    if preferences.get("autoStart") is not True or settings.get("max_browsers") != concurrency or settings.get("consume_codex") is not True:
        raise install.SetupError("Desktop defaults mismatch")
    if preferences.get("autoUpdate") is not False or preferences.get("resumeAfterUpdate") is not False:
        raise install.SetupError("Automatic app replacement or resume-after-update bypasses reviewed startup")
    checks["desktop_startup"] = {"autoStart": True, "consume_codex": True, "max_browsers": concurrency}
    checks.update(readiness_scope="local_worker_only", broker_end_to_end_verified=False, parallel_jobs_verified=False)
    if not args.skip_chromium:
        result = json.loads(install.run([node, browser, "--check"], env=env, timeout=90))
        if result.get("ok") is not True or result.get("chromium_sandbox") is not True or result.get("readback") != "SBS_CHROMIUM_ALLOW_OK":
            raise install.SetupError("Chromium smoke readback failed")
        checks["chromium"] = {key: result.get(key) for key in ("playwright", "chromium", "chromium_sandbox", "tls_verification")}
    else:
        checks["chromium"] = "skipped"
    # The launcher links auth.json into isolated sessions; keychain-only login
    # status does not satisfy that contract. Inspect existence, never contents.
    checks.update(authentication_readback(real_codex, auth_home, env, args.skip_login))
    skipped = args.skip_login or args.skip_chromium
    if not skipped and authentication_ready(checks):
        checks["real_codex_runtime"] = runtime_smoke.run_runtime_smoke(root, args.runtime_timeout)
    else:
        checks["real_codex_runtime"] = {"ok": False, "skipped": True, "reason": "diagnostic_skip_or_authentication_not_ready"}
    checks["diagnostic_only"] = bool(skipped)
    pre_launch = getattr(args, "pre_launch", False)
    checks["infrastructure_ready"] = (not skipped and authentication_ready(checks) and checks["real_codex_runtime"].get("ok") is True)
    checks["pre_launch_only"] = pre_launch
    if checks["infrastructure_ready"] and not pre_launch:
        try:
            checks["worker"] = worker_readback(user_data, setup)
        except (install.SetupError, OSError, ValueError, KeyError, TypeError):
            error = sys.exc_info()[1]
            checks["worker"] = {"worker_ready": False, "failure": str(error) if isinstance(error, install.SetupError) else "actual worker readback failed"}
    else:
        checks["worker"] = {"worker_ready": False, "reason": "pre_launch_or_infrastructure_not_ready"}
    checks["worker_ready"] = checks["worker"].get("worker_ready") is True
    checks["delivery_ready"] = checks["infrastructure_ready"] and not pre_launch and checks["worker_ready"]
    checks["status"] = "готов" if checks["delivery_ready"] else "подготовлен" if pre_launch and checks["infrastructure_ready"] else "не готов"
    checks["managed_policy"] = "actual policy is reported from real Codex turn_context; runtime tool outputs are independently validated"
    print(json.dumps(checks, indent=2))
    return 0 if checks["delivery_ready"] or (pre_launch and checks["infrastructure_ready"]) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=install.DEFAULT_ROOT)
    parser.add_argument("--profile", type=Path, help="Optional external original AIM2 for additional digest comparison")
    parser.add_argument("--user-data", type=Path, help="Actual YaTorrent Electron data directory if different")
    parser.add_argument("--skip-chromium", action="store_true", help="Static checks only, no browser launch")
    parser.add_argument("--skip-login", action="store_true", help="Do not inspect login status")
    parser.add_argument("--pre-launch", action="store_true", help="Verify infrastructure before app launch; never confirms worker or delivery readiness")
    parser.add_argument("--runtime-only", action="store_true", help="Diagnostic real-Codex runtime check only; does not establish delivery readiness")
    parser.add_argument("--runtime-timeout", type=int, default=240, help="Bounded real-Codex smoke timeout, 30..300 seconds")
    parser.add_argument("--wait-import", type=float, default=15, help="Bounded readback wait, max 60 seconds")
    args = parser.parse_args(argv)
    if args.pre_launch and (args.runtime_only or args.skip_login or args.skip_chromium):
        parser.error("--pre-launch cannot combine with diagnostic skip or runtime-only options")
    if args.runtime_only and (args.skip_login or args.skip_chromium):
        parser.error("--runtime-only cannot combine with skip options")
    if not 30 <= args.runtime_timeout <= 300:
        parser.error("--runtime-timeout must be 30..300")
    if not 0 <= args.wait_import <= 60:
        parser.error("--wait-import must be between 0 and 60")
    args.root = args.root.expanduser().absolute()
    if args.profile is not None:
        args.profile = args.profile.expanduser().absolute()
    try:
        return verify(args)
    except (install.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        exc = sys.exc_info()[1]
        print(json.dumps({"status": "не готов", "delivery_ready": False,
                          "failure": str(exc) if isinstance(exc, install.SetupError) else "filesystem, runtime or format check failed; no secret output"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
