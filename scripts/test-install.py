#!/usr/bin/env python3
"""Fixture tests only: no YaTorrent install, no live profile, no broker requests."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install
import verify


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sbs-installer-fixture-")
        self.base = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_plan_is_nonmutating_with_missing_profile(self):
        prefix = self.base / "not-created"
        args = install.arguments(["--plan", "--root", str(prefix), "--app", str(self.base / "YaTorrent.app"), "--profile", str(self.base / "missing.aim2")])
        with patch.object(install.releases, "latest", return_value={"version": "0.3.4"}):
            plan = install.plan(args)
        self.assertFalse(plan["mutates"])
        self.assertEqual(plan["limits"]["total_agent_slots"], 11)
        self.assertEqual(plan["pins"]["playwright"], "1.64.0")
        self.assertEqual(plan["browser_command"], [str(prefix / "node/bin/node"), str(prefix / "lib/sbs-chromium.mjs")])
        self.assertEqual(plan["prompt_reader_examples"]["scout"][-1], "scout")
        self.assertEqual(plan["browser_command_string"], " ".join(plan["browser_command"]))
        self.assertEqual(plan["prompt_reader_command_string"], " ".join(plan["prompt_reader_command"]))
        self.assertEqual(plan["prompt_reader_example_strings"]["scout"], " ".join(plan["prompt_reader_examples"]["scout"]))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_default_root_has_no_space_and_pins_are_unchanged(self):
        self.assertEqual(install.arguments([]).root, Path.home() / ".local/share/yatorrent-sbs")
        self.assertEqual((install.VERSION, install.NODE_VERSION, install.CODEX_VERSION, install.PLAYWRIGHT_VERSION),
                         ("0.3.5", "24.21.0", "0.162.1", "1.64.0"))

    def test_unsafe_custom_roots_stop_before_mutation_or_network(self):
        for suffix in ("space root", "quote'root", 'quote"root', "semi;root", "dollar$root", "newline\nroot", "unicode-ю", "backtick`root"):
            with self.subTest(suffix=suffix):
                root = self.base / suffix
                args = install.arguments(["--apply", "--root", str(root), "--profile", str(self.base / "absent.aim2")])
                with patch.object(install, "run") as execute, patch.object(install, "download") as download, \
                     patch.object(install, "write_owned") as write, patch.object(install, "public_profile") as profile:
                    with self.assertRaises(install.SetupError):
                        install.apply(args)
                    for operation in (execute, download, write, profile):
                        operation.assert_not_called()
                self.assertFalse(root.exists())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_unsafe_existing_root_is_rejected_without_changing_it(self):
        root = self.base / "old root"
        root.mkdir()
        marker = root / "installation.json"
        marker.write_text('{"state":"prepared"}')
        before = marker.read_bytes()
        with self.assertRaises(install.SetupError):
            install.launcher_commands(root)
        self.assertEqual(marker.read_bytes(), before)

    def trusted_fixture(self):
        root = self.base / "trusted"
        node = root / "node/bin/node"
        browser = root / "lib/sbs-chromium.mjs"
        reader = root / "lib/sbs-read-prompt.mjs"
        for file in (node, browser, reader):
            file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            file.write_text("fixture")
            file.chmod(0o700 if file == node else 0o600)
        return root, node, browser, reader

    def test_entry_points_are_real_trusted_files_and_commands_are_literal(self):
        root, node, browser, reader = self.trusted_fixture()
        self.assertEqual(install.entry_points({"private_node": str(node)}, root), (node, browser, reader))
        commands = install.launcher_commands(root, {"private_node": str(node)}, require_existing=True)
        for array_key, string_key in (("browser_command", "browser_command_string"),
                                      ("prompt_reader_command", "prompt_reader_command_string")):
            self.assertEqual(commands[string_key].split(" "), commands[array_key])
            self.assertNotIn("'", commands[string_key])
            self.assertNotIn('"', commands[string_key])
        with self.assertRaises(install.SetupError):
            install.entry_points({"private_node": str(self.base / "other-node")}, root)
        with self.assertRaises(install.SetupError):
            install.entry_points({"private_node": str(node), "node_bin": str(self.base / "other-node")}, root)

    def test_untrusted_entries_and_ancestors_are_rejected(self):
        root, node, browser, reader = self.trusted_fixture()
        for file in (node, browser, reader, root, root / "lib"):
            with self.subTest(file=file):
                mode = stat.S_IMODE(file.stat().st_mode)
                file.chmod(mode | 0o020)
                with self.assertRaises(install.SetupError):
                    install.entry_points({}, root)
                file.chmod(mode)
        with patch.object(install.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(install.SetupError):
                install.entry_points({}, root)
        for file in (node, browser, reader):
            contents, mode = file.read_bytes(), stat.S_IMODE(file.stat().st_mode)
            file.unlink()
            file.symlink_to(self.base / "absent")
            with self.assertRaises(install.SetupError):
                install.entry_points({}, root)
            file.unlink()
            file.write_bytes(contents)
            file.chmod(mode)
        node.chmod(0o600)
        with self.assertRaises(install.SetupError):
            install.entry_points({}, root)

    def test_writable_root_is_refused_before_process_or_network(self):
        root = self.base / "writable"
        root.mkdir(mode=0o777)
        root.chmod(0o777)
        args = install.arguments(["--apply", "--root", str(root)])
        with patch.object(install, "run") as execute, patch.object(install, "download") as download:
            with self.assertRaises(install.SetupError):
                install.apply(args)
            execute.assert_not_called()
            download.assert_not_called()
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o777)
        self.assertEqual(list(root.iterdir()), [])

    def test_known_macos_os_aliases_are_normalized_without_user_symlink_bypass(self):
        if Path("/var").is_symlink() and os.path.realpath("/var") == "/private/var" and str(self.base).startswith("/private/var/"):
            alias = Path("/var" + str(self.base)[len("/private/var"):]) / "safe-root"
            self.assertEqual(install.command_path(alias), self.base / "safe-root")
            self.assertEqual(install.launcher_commands(alias)["browser_command"][0], str(self.base / "safe-root/node/bin/node"))
        root, node, browser, reader = self.trusted_fixture()
        alias = self.base / "user-alias"
        alias.symlink_to(root)
        with self.assertRaises(install.SetupError):
            install.launcher_commands(alias)

    def test_owned_write_backups_and_identical_reuse(self):
        root = self.base / "root"
        file = root / "lib/config.json"
        root.mkdir()
        self.assertTrue(install.write_owned(file, b"original fixture", root))
        inode = file.stat().st_ino
        self.assertFalse(install.write_owned(file, b"original fixture", root))
        self.assertEqual(file.stat().st_ino, inode)
        self.assertTrue(install.write_owned(file, b"new fixture", root))
        saved = [p for p in (root / "backups").rglob("*") if p.is_file()]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"original fixture")
        self.assertEqual(stat.S_IMODE(saved[0].stat().st_mode), 0o600)

    def test_symlink_destination_rejected(self):
        (self.base / "link").symlink_to(self.base / "outside")
        with self.assertRaises(install.SetupError):
            install.safe_path(self.base / "link/config.json")

    def archive(self, names):
        archive = self.base / ("node-" + str(len(list(self.base.iterdir()))) + ".tgz")
        with tarfile.open(archive, "w:gz") as tar:
            for name, kind, link in names:
                item = tarfile.TarInfo(name)
                item.mode = 0o755
                if kind == "file":
                    item.size = 4
                    tar.addfile(item, io.BytesIO(b"test"))
                elif kind == "dir":
                    item.type = tarfile.DIRTYPE
                    tar.addfile(item)
                else:
                    item.type = tarfile.SYMTYPE
                    item.linkname = link
                    tar.addfile(item)
        return archive

    def test_node_extraction_valid_relative_link(self):
        prefix = f"node-v{install.NODE_VERSION}-darwin-arm64"
        archive = self.archive([(prefix + "/bin/node", "file", None),
                                (prefix + "/lib/node", "link", "../bin/node")])
        dest = self.base / "node"
        install.extract_node(archive, dest)
        self.assertEqual((dest / "lib/node").read_bytes(), b"test")
        self.assertTrue((dest / "lib/node").is_symlink())

    def test_node_extraction_blocks_traversal_and_escaping_links(self):
        prefix = f"node-v{install.NODE_VERSION}-darwin-arm64"
        for index, name in enumerate(["../outside", prefix + "/../../outside"]):
            with self.assertRaises(install.SetupError):
                install.extract_node(self.archive([(name, "file", None)]), self.base / ("dest" + str(index)))
        for index, link in enumerate(["../../outside", "/tmp/outside"]):
            with self.assertRaises(install.SetupError):
                install.extract_node(self.archive([(prefix + "/bin/link", "link", link)]), self.base / ("badlink" + str(index)))
        self.assertFalse((self.base.parent / "outside").exists())

    def test_unknown_app_and_updated_bundle_refused(self):
        root = self.base / "root"
        root.mkdir()
        app = self.base / "YaTorrent.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": install.VERSION}))
        marker = root / "installation.json"
        with self.assertRaises(install.SetupError):
            install.validate_existing_app(app, marker)
        marker.write_text(json.dumps({"app": str(app), "app_version": install.VERSION, "bundle_sha256": install.bundle_digest(app)}))
        self.assertTrue(install.validate_existing_app(app, marker))
        (app / "Contents/new-update").write_text("fixture")
        with self.assertRaises(install.SetupError):
            install.validate_existing_app(app, marker)

    def test_public_import_comparison_excludes_credentials(self):
        profile = {"version": 1, "broker": {"url": "https://example.invalid/", "protocol_version": 2, "payload_version": 2},
                   "credentials": {"fixture_only": "never printed"}, "executors": {"codex": {"model": "fixture-model", "auth_mode": "existing"}}}
        source = self.base / "fixture.aim2"
        source.write_text(json.dumps(profile))
        public = copy.deepcopy(profile)
        del public["credentials"]
        public["broker"]["url"] = "https://example.invalid"
        public["broker"]["allow_http"] = False
        public["executors"]["codex"]["auth_mode"] = "default"
        stored = self.base / "profile.json"
        stored.write_text(json.dumps({"version": 1, "profile": public, "credentials_encrypted": "fixture ciphertext"}))
        self.assertEqual(verify.normalized_public(source), verify.normalized_public(stored, stored=True))
        public["broker"]["url"] = "https://changed.invalid"
        stored.write_text(json.dumps({"version": 1, "profile": public, "credentials_encrypted": "fixture ciphertext"}))
        self.assertNotEqual(verify.normalized_public(source), verify.normalized_public(stored, stored=True))

    @unittest.skipUnless(shutil.which("codex"), "Codex unavailable for read-only policy fixture")
    def test_execpolicy_only_exact_fixed_browser_and_prompt_reader_prefixes(self):
        node, browser = self.base / "private-node", self.base / "sbs-chromium.mjs"
        rule = self.base / "fixture.rules"
        rule.write_text(install.rule_text(node, browser))
        install.check_rule(shutil.which("codex"), rule, node, browser, dict(os.environ))

    def test_prompt_reader_rule_is_separate_and_uses_fixed_path(self):
        node, browser = self.base / "private-node", self.base / "lib/sbs-chromium.mjs"
        text = install.rule_text(node, browser)
        self.assertEqual(text.count("prefix_rule("), 2)
        self.assertIn(json.dumps([str(node), str(browser)]), text)
        self.assertIn(json.dumps([str(node), str(browser.with_name("sbs-read-prompt.mjs"))]), text)
        self.assertNotIn('pattern=["/bin/cat"', text)

    def test_keychain_only_login_is_not_isolated_worker_auth_ready(self):
        auth_home = self.base / "fixture-auth"
        auth_home.mkdir()
        with patch.object(verify, "command_result", return_value=SimpleNamespace(returncode=0)):
            checks = verify.authentication_readback("fixture-codex", auth_home, {})
            self.assertEqual(checks["codex_auth"], "logged_in")
            self.assertFalse(checks["file_auth_present"])
            self.assertFalse(verify.authentication_ready(checks))
            (auth_home / "auth.json").write_text("opaque fixture contents are never parsed")
            checks = verify.authentication_readback("fixture-codex", auth_home, {})
            self.assertTrue(verify.authentication_ready(checks))


@unittest.skipUnless(shutil.which("node"), "Node required for adapter fixtures")
class WorkerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sbs-worker-fixture-")
        self.base = Path(self.temporary.name).resolve()
        self.launcher = self.base / "worker-launcher.mjs"
        shutil.copyfile(install.ASSETS / "worker-launcher.mjs", self.launcher)
        self.original = self.base / "aim2-worker-original"
        self.original.write_text("#!" + sys.executable + "\n" + '''import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
result = {"pid": os.getpid(), "args": args, "env_preserved": os.environ.get("SBS_FIXTURE_ENV") == "kept"}
if args and args[0] == "desktop" and "--help" not in args:
    file = Path(args[args.index("--config") + 1])
    result["observed_config"] = json.loads(file.read_text())
Path(os.environ["SBS_FIXTURE_CAPTURE"]).write_text(json.dumps(result))
if os.environ.get("SBS_FIXTURE_SLEEP") == "yes": time.sleep(20)
sys.exit(int(os.environ.get("SBS_FIXTURE_EXIT", "0")))
''')
        self.original.chmod(0o755)
        self.setup = {"real_worker": str(self.original), "real_worker_sha256": install.digest(self.original),
                      "auth_home": str(self.base / "auth"), "codex_wrapper": str(self.base / "bin/codex"),
                      "worker_timeout_seconds": 1800, "worker_concurrency": 4}
        (self.base / "install-config.json").write_text(json.dumps(self.setup))
        self.config = {"worker_id": "fixture-worker-identity", "work_dir": str(self.base / "workspace"),
                       "local_queue": {"path": str(self.base / "settings.sqlite")}, "concurrency": 4,
                       "broker": {"url": "https://example.invalid", "token_env": "FIXTURE_TOKEN"}, "task_types": ["codex", "doc"],
                       "executors": {"codex": {"enabled": True, "model": "fixture-model", "reasoning_effort": "high", "binary": "before", "codex_home": "before"},
                                     "eliza": {"enabled": False, "model": "other-fixture"}}}
        self.config_file = self.base / "worker-config.json"
        self.config_file.write_text(json.dumps(self.config))
        self.config_file.chmod(0o600)
        self.capture = self.base / "capture.json"
        self.env = dict(os.environ, SBS_FIXTURE_CAPTURE=str(self.capture), SBS_FIXTURE_ENV="kept")

    def tearDown(self):
        self.temporary.cleanup()

    def launch(self, args=None, **environment):
        args = args if args is not None else ["desktop", "--config", str(self.config_file), "--state-dir", str(self.base / "state"), "--port", "0"]
        return subprocess.Popen([shutil.which("node"), self.launcher, *args], env=dict(self.env, **environment), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_atomic_patch_preserves_profile_identity_environment_and_pid(self):
        before = self.config_file.stat().st_ino
        proc = self.launch()
        output, error = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, error.decode())
        self.assertEqual(output, b"")
        observed = json.loads(self.capture.read_text())
        self.assertEqual(observed["pid"], proc.pid)
        self.assertTrue(observed["env_preserved"])
        patched = copy.deepcopy(self.config)
        patched["concurrency"] = 4
        patched["executors"]["codex"].update(binary=self.setup["codex_wrapper"], codex_home=self.setup["auth_home"],
                                               timeout_seconds=1800, max_output_bytes=8 * 1024 * 1024)
        self.assertEqual(observed["observed_config"], patched)
        receipt = install.read_json(self.base / "state/sbs-worker-launch.json")
        self.assertEqual(receipt["pid"], proc.pid)
        self.assertEqual(receipt["config_sha256"], install.digest(self.config_file))
        self.assertEqual(receipt["real_worker_sha256"], self.setup["real_worker_sha256"])
        self.assertEqual(stat.S_IMODE((self.base / "state/sbs-worker-launch.json").stat().st_mode), 0o600)
        self.assertNotIn("FIXTURE_TOKEN", json.dumps(receipt))
        self.assertNotEqual(self.config_file.stat().st_ino, before)
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_non_desktop_and_help_passthrough_keep_original_exit(self):
        before = self.config_file.read_bytes()
        for args in [["--help"], ["desktop", "--help"], ["unknown", "value"]]:
            proc = self.launch(args, SBS_FIXTURE_EXIT="7")
            proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 7)
            self.assertEqual(json.loads(self.capture.read_text())["args"], args)
            self.assertEqual(self.config_file.read_bytes(), before)

    def test_modified_original_refused_before_config_patch(self):
        before = self.config_file.read_bytes()
        with self.original.open("a") as output:
            output.write("\n# changed fixture\n")
        proc = self.launch()
        output, error = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(output, b"")
        self.assertIn(b"refused startup", error)
        self.assertFalse(self.capture.exists())
        self.assertEqual(self.config_file.read_bytes(), before)

    def test_unsafe_config_and_unexpected_desktop_args_refused(self):
        self.config_file.unlink()
        self.config_file.symlink_to(self.base / "absent")
        proc = self.launch()
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.capture.exists())
        proc = self.launch(["desktop", "--unexpected", "fixture"])
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)

    def test_signal_targets_original_with_same_pid(self):
        proc = self.launch(["--help"], SBS_FIXTURE_SLEEP="yes")
        try:
            deadline = time.monotonic() + 10
            while not self.capture.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.capture.exists())
            self.assertEqual(json.loads(self.capture.read_text())["pid"], proc.pid)
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, -signal.SIGTERM)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
