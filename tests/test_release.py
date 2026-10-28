import importlib.util
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
import uuid
from zipfile import ZipFile


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_release', REPO / 'scripts/build_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        for name in (REPO / 'release-files.txt').read_text(encoding='utf-8').splitlines():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, destination)
        self.output = Path(self.temp.name) / 'artifacts'

    def test_excludes_unlisted_reports_financial_data_and_credentials(self):
        canary = 'TEST_CANARY_' + uuid.uuid4().hex
        for name in ('reports/private.md', 'data/financial.csv', '.env', 'notes/private.md', 'secret.key'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canary, encoding='utf-8')
        artifacts = release.build(self.root, self.output)
        with ZipFile(artifacts[0]) as archive:
            self.assertEqual(len(archive.namelist()), 17)
            self.assertFalse(any(canary.encode() in archive.read(n) for n in archive.namelist()))
        with ZipFile(artifacts[1]) as archive:
            self.assertEqual({n.removeprefix(release.SKILL + '/') for n in archive.namelist()}, release.SKILL_FILES)

    def test_rejects_unsafe_allowlist_entries(self):
        for name in ('../private.md', 'reports/private.md', 'Reports/private.md', 'reports./private.md',
                     'docs/data/private.md', 'data/private.md', '.env', 'docs/financial.csv', 'C:/private.md'):
            with self.subTest(name=name):
                manifest = REPO.joinpath('release-files.txt').read_text(encoding='utf-8').rstrip('\n')
                self.root.joinpath('release-files.txt').write_text(manifest + '\n' + name + '\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Invalid|Forbidden'):
                    release.release_files(self.root)
                shutil.copyfile(REPO / 'release-files.txt', self.root / 'release-files.txt')

    def test_rejects_missing_file_and_license_mismatch(self):
        license_path = self.root / f'skills/{release.SKILL}/LICENSE'
        license_path.write_text('different license', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'license copies differ'):
            release.release_files(self.root)
        license_path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or linked'):
            release.release_files(self.root)

    def test_rejects_in_repository_output_and_overwrite(self):
        with self.assertRaises(ValueError):
            release.build(self.root, self.root / 'dist')
        artifacts = release.build(self.root, self.output)
        original = artifacts[0].read_bytes()
        with self.assertRaises(FileExistsError):
            release.build(self.root, self.output)
        self.assertEqual(artifacts[0].read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
