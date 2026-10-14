"""Public packaging and external-profile fixtures; no real credentials."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import install

spec = importlib.util.spec_from_file_location('public_release', Path(__file__).with_name('build-release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = Path(__file__).resolve().parents[1]
        self.profile = self.base / 'external.aim2'
        self.profile.write_text(json.dumps({'version': 1, 'broker': {'url': 'https://broker.example'}, 'executors': {'codex': {'model': 'fixture'}}}))
        self.profile.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_external_profile_stops_before_mutation(self):
        args = install.arguments(['--apply', '--root', str(self.base / 'runtime')])
        with patch.object(install, 'assert_stopped') as processes, patch.object(install, 'download') as download, patch.object(install, 'write_owned') as write:
            with self.assertRaises(install.SetupError):
                install.apply(args)
            processes.assert_not_called()
            download.assert_not_called()
            write.assert_not_called()
        self.assertFalse(args.root.exists())

    def test_private_external_profile_accepted(self):
        self.assertEqual(install.external_profile(self.profile), self.profile)

    def test_custom_app_data_relocation_stops_before_mutation(self):
        args = install.arguments(['--apply', '--root', str(self.base / 'runtime'), '--profile', str(self.profile), '--user-data', str(self.base / 'custom-data')])
        with patch.object(install, 'assert_stopped') as processes, patch.object(install, 'download') as download:
            with self.assertRaises(install.SetupError):
                install.apply(args)
            processes.assert_not_called()
            download.assert_not_called()
        self.assertFalse(args.root.exists())

    def test_public_profile_or_symlink_refused(self):
        self.profile.chmod(0o644)
        with self.assertRaises(install.SetupError):
            install.external_profile(self.profile)
        link = self.base / 'link.aim2'
        link.symlink_to(self.profile)
        with self.assertRaises(install.SetupError):
            install.external_profile(link)

    def test_profile_inside_package_refused(self):
        with patch.object(install, 'ASSETS', self.base / 'assets'):
            with self.assertRaises(install.SetupError):
                install.external_profile(self.profile)

    def test_default_four_jobs_and_explicit_override(self):
        self.assertEqual(install.arguments([]).worker_concurrency, 4)
        self.assertEqual(install.arguments(['--worker-concurrency', '1']).worker_concurrency, 1)
        with patch.object(install.releases, 'latest', return_value={'version': 'fixture'}):
            plan = install.plan(install.arguments(['--root', str(self.base / 'runtime')]))
        self.assertTrue(plan['profile_required'])
        self.assertFalse(plan['profile_bundled'])
        self.assertEqual(plan['limits']['worker_concurrency'], 4)

    def package(self):
        target = self.base / 'skill'
        shutil.copytree(self.source, target, ignore=shutil.ignore_patterns('__pycache__'))
        return target

    def test_public_zip_and_manifest(self):
        target = self.package()
        result = release.build(target, self.base / 'release.zip')
        self.assertFalse(result['credentials_bundled'])
        with zipfile.ZipFile(self.base / 'release.zip') as archive:
            self.assertFalse(any(n.endswith(('.aim2', '.auth', 'auth.json')) for n in archive.namelist()))
            self.assertIn('yatorrent-sbs-setup/MANIFEST.sha256', archive.namelist())
            for name in ('assets/sbs-navigation-transaction.mjs', 'scripts/serp_urls.py', 'scripts/test-navigation.py'):
                self.assertIn('yatorrent-sbs-setup/' + name, archive.namelist())

    def test_private_files_block_public_build(self):
        target = self.package()
        for name in ('assets/profile.aim2', 'auth.json', 'old-private.zip', '.env'):
            p = target / name
            p.write_text('synthetic-private-fixture')
            with self.assertRaises(ValueError):
                release.build(target, self.base / 'release.zip')
            p.unlink()

    def test_token_shaped_content_blocks_public_build(self):
        target = self.package()
        (target / 'SKILL.md').write_text('fixture ' + 'gh' + 'p_' + 'x' * 30)
        with self.assertRaises(ValueError):
            release.build(target, self.base / 'release.zip')


if __name__ == '__main__':
    unittest.main(verbosity=2)
