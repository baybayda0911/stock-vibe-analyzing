import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import uuid
from zipfile import ZipFile


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_release', REPO / 'scripts/build_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_platform_resources_are_exact_and_import_with_standard_library_only(self):
        files = release.release_files(self.root)
        self.assertTrue(release.APP_FILES.issubset(files))
        original = self.root.joinpath('release-files.txt').read_text(encoding='utf-8')
        self.root.joinpath('release-files.txt').write_text(original.replace('app/static/app.js\n', ''), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'platform resources'):
            release.release_files(self.root)
        self.root.joinpath('release-files.txt').write_text(original + 'app/static/financial.html\n', encoding='utf-8')
        self.root.joinpath('app/static/financial.html').write_text('private financial fixture', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Forbidden'):
            release.release_files(self.root)
        self.root.joinpath('release-files.txt').write_text(original, encoding='utf-8')
        command = 'import sys;sys.path.insert(0,sys.argv[1]);import app.server,app.launch,app.store,app.report_view'
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', command, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.manifest = (REPO / 'release-files.txt').read_text(encoding='utf-8')
        for name in self.manifest.splitlines():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, destination)
        self.output = Path(self.temp.name) / 'artifacts'

    def test_excludes_unlisted_reports_financial_data_and_credentials(self):
        canary = 'TEST_CANARY_' + uuid.uuid4().hex
        for name in ('reports/private.md', 'data/financial.csv', 'GStock/registry.json',
                     '.env', '.research-lab.local.json', 'notes/private.md', 'secret.key',
                     'skills/stock-technical-analysis/output/private.html',
                     'skills/stock-technical-analysis/data/prices.csv',
                     'private-snapshot/analysis.json', 'private-snapshot/chart.png',
                     '_management/runtime-state.txt', '_workspace/platform/broker-cache/source/original.pdf',
                     '_workspace/platform/broker-cache/source/pages.json', 'stock-analyst-ranking/fictional.csv'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canary, encoding='utf-8')
        artifacts = release.build(self.root, self.output, tuple(release.SKILL_FILES))
        files = release.release_files(self.root)
        version = files['VERSION'].decode().strip()
        with ZipFile(artifacts[0]) as archive:
            self.assertEqual(set(archive.namelist()),
                             {f'stock-vibe-analyzing-{version}/{name}' for name in files})
            self.assertFalse(any(canary.encode() in archive.read(n) for n in archive.namelist()))
            for name, content in files.items():
                self.assertEqual(archive.read(f'stock-vibe-analyzing-{version}/{name}'), content)
        for artifact, skill in zip(artifacts[1:-1], release.SKILL_FILES):
            with ZipFile(artifact) as archive:
                self.assertEqual({n.removeprefix(skill + '/') for n in archive.namelist()},
                                 release.SKILL_FILES[skill])
                for member in release.SKILL_FILES[skill]:
                    self.assertEqual(archive.read(f'{skill}/{member}'), files[f'skills/{skill}/{member}'])
                self.assertFalse(any(canary.encode() in archive.read(n) for n in archive.namelist()))
        for line in artifacts[-1].read_text(encoding='utf-8').splitlines():
            digest, name = line.split('  ')
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        self.assertEqual(len(artifacts[-1].read_text(encoding='utf-8').splitlines()), 5)

    def test_executable_skill_archives_run_in_isolation_without_site_packages(self):
        artifacts = release.build(self.root, self.output, tuple(release.SKILL_FILES))
        for artifact, skill in zip(artifacts[1:-1], release.SKILL_FILES):
            if skill == release.SKILL:
                continue
            isolated = Path(self.temp.name) / f'isolated-{skill}'
            with self.subTest(skill=skill), ZipFile(artifact) as archive:
                archive.extractall(isolated)
                scripts = {'stock-research-lab': ('research_lab.py',),
                           'stock-technical-analysis': ('harmonic_analysis.py', 'technical_analysis.py', 'industry_context.py'),
                           'stock-research-archive': ('research_archive.py',)}[skill]
                for script in scripts:
                    result = subprocess.run([sys.executable, '-I', '-S', '-X', 'utf8', '-B',
                                             str(isolated / skill / 'scripts' / script), '--help'],
                                            cwd=isolated, capture_output=True, text=True, encoding='utf-8')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('usage:', result.stdout)
                if skill == 'stock-technical-analysis':
                    self.assertEqual(len(release.SKILL_FILES[skill]), 15)
                    for module in ('harmonic_core.py', 'technical_core.py', 'technical_data.py', 'research_outputs.py', 'industry_context.py', 'report_presentation.py'):
                        result = subprocess.run([sys.executable, '-I', '-S', '-X', 'utf8', '-B',
                                                 '-c', 'import runpy; runpy.run_path('+repr(str(isolated / skill / 'scripts' / module))+", run_name='isolated_import')"],
                                                cwd=isolated, capture_output=True, text=True, encoding='utf-8')
                        self.assertEqual(result.returncode, 0, result.stderr)

                if skill == 'stock-research-archive':
                    self.assertEqual(len(release.SKILL_FILES[skill]), 7)
                    for module in ('research_archive.py', 'archive_views.py', 'archive_backup.py'):
                        result = subprocess.run([sys.executable, '-I', '-S', '-X', 'utf8', '-B',
                            '-c', 'import runpy; runpy.run_path('+repr(str(isolated / skill / 'scripts' / module))+", run_name='isolated_import')"],
                            cwd=isolated, capture_output=True, text=True, encoding='utf-8')
                        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_and_cli_selection_use_independent_skill_versions(self):
        self.root.joinpath('VERSION').write_text('9.8.7\n', encoding='utf-8')
        files = release.release_files(self.root)
        basic_version = release.skill_version(files, release.SKILL)
        lab_version = release.skill_version(files, 'stock-research-lab')
        technical_version = release.skill_version(files, 'stock-technical-analysis')
        archive_version = release.skill_version(files, 'stock-research-archive')
        artifacts = release.build(self.root, self.output)
        self.assertEqual([p.name for p in artifacts],
                         ['stock-vibe-analyzing-9.8.7.zip',
                          f'{release.SKILL}-{basic_version}.zip', 'SHA256SUMS.txt'])
        cases = [([], {f'{release.SKILL}-{basic_version}.zip'}),
                 (['--skill', 'stock-research-lab'], {f'stock-research-lab-{lab_version}.zip'}),
                 (['--skill', release.SKILL, '--skill', 'stock-research-lab'],
                  {f'{release.SKILL}-{basic_version}.zip', f'stock-research-lab-{lab_version}.zip'}),
                 (['--skill', 'stock-technical-analysis'],
                  {f'stock-technical-analysis-{technical_version}.zip'}),
                 (['--skill', release.SKILL, '--skill', 'stock-research-lab',
                   '--skill', 'stock-technical-analysis'],
                  {f'{release.SKILL}-{basic_version}.zip', f'stock-research-lab-{lab_version}.zip',
                   f'stock-technical-analysis-{technical_version}.zip'}),
                 (['--skill', 'stock-research-archive'], {f'stock-research-archive-{archive_version}.zip'}),
                 (['--skill', release.SKILL, '--skill', 'stock-research-lab', '--skill', 'stock-technical-analysis', '--skill', 'stock-research-archive'],
                  {f'{release.SKILL}-{basic_version}.zip', f'stock-research-lab-{lab_version}.zip',
                   f'stock-technical-analysis-{technical_version}.zip', f'stock-research-archive-{archive_version}.zip'})]
        for index, (selection, skill_archives) in enumerate(cases):
            with self.subTest(selection=selection):
                cli_output = self.output.parent / f'cli-artifacts-{index}'
                result = subprocess.run([sys.executable, '-X', 'utf8', '-B',
                                         str(self.root / 'scripts/build_release.py'), '--output', str(cli_output),
                                         *selection], capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual({p.name for p in cli_output.iterdir()},
                                 {'stock-vibe-analyzing-9.8.7.zip', 'SHA256SUMS.txt'} | skill_archives)

    def test_rejects_unknown_empty_or_duplicate_skill_selection(self):
        for skills in ((), ('stock-unknown-analysis',), (release.SKILL, release.SKILL)):
            with self.subTest(skills=skills), self.assertRaisesRegex(ValueError, 'declared standalone skills'):
                release.build(self.root, self.output, skills)
        self.assertFalse(self.output.exists())

    def test_rejects_unsafe_allowlist_entries(self):
        for name in ('../private.md', 'reports/private.md', 'Reports/private.md', 'reports./private.md',
                     'docs/data/private.md', 'data/private.md', '.env', 'docs/financial.csv', 'C:/private.md'):
            with self.subTest(name=name):
                self.root.joinpath('release-files.txt').write_text(self.manifest + name + '\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Invalid|Forbidden'):
                    release.release_files(self.root)
        for name in ('GStock/registry.md', 'gstock/config.txt'):
            with self.subTest(name=name):
                self.root.joinpath('release-files.txt').write_text(self.manifest + name + '\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Forbidden'):
                    release.release_files(self.root)

    def test_rejects_unknown_and_unreviewed_skill_members(self):
        for name in ('skills/stock-unknown-analysis/SKILL.md',
                     'skills/stock-fundamental-analysis/references/private.md',
                     'skills/stock-research-lab/references/private.md',
                     'skills/stock-technical-analysis/references/private.md'):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('unreviewed test member', encoding='utf-8')
                self.root.joinpath('release-files.txt').write_text(self.manifest + name + '\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Unknown|reviewed files'):
                    release.release_files(self.root)

    def test_rejects_duplicate_empty_and_incomplete_allowlists(self):
        for manifest in ('', self.manifest + 'README.md\n',
                         self.manifest.replace('skills/stock-research-lab/scripts/research_lab.py\n', ''),
                         self.manifest.replace('skills/stock-technical-analysis/scripts/harmonic_core.py\n', '')):
            with self.subTest(manifest=manifest[-80:]):
                self.root.joinpath('release-files.txt').write_text(manifest, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'nonempty and unique|reviewed files'):
                    release.release_files(self.root)

    def test_rejects_escaped_and_undeclared_local_dependencies(self):
        for skill in release.SKILL_FILES:
            path = self.root / f'skills/{skill}/SKILL.md'
            original = path.read_text(encoding='utf-8')
            for target in ('../../README.md', '../stock-technical-analysis/SKILL.md',
                           'references/missing.md', '/private.md', 'file:///private.md',
                           '%2e%2e/%2e%2e/README.md'):
                with self.subTest(skill=skill, target=target):
                    path.write_text(original + f'\n[dependency]({target})\n', encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'dependency escapes or is undeclared'):
                        release.release_files(self.root)
            path.write_text(original, encoding='utf-8')

    def test_rejects_invalid_skill_identity_and_version(self):
        path = self.root / 'skills/stock-research-lab/SKILL.md'
        original = path.read_text(encoding='utf-8')
        version = release.skill_version(release.release_files(self.root), 'stock-research-lab')
        for text in (original.replace('name: stock-research-lab', 'name: stock-technical-analysis'),
                     original.replace(f'"{version}"', '"0.1"')):
            with self.subTest(text=text[:80]):
                path.write_text(text, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Invalid standalone skill'):
                    release.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_missing_file_and_license_mismatch(self):
        for skill in release.SKILL_FILES:
            with self.subTest(skill=skill):
                license_path = self.root / f'skills/{skill}/LICENSE'
                license_path.write_text('different license', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'license copies differ'):
                    release.release_files(self.root)
                license_path.unlink()
                with self.assertRaisesRegex(ValueError, 'Missing or linked'):
                    release.release_files(self.root)
                shutil.copyfile(REPO / f'skills/{skill}/LICENSE', license_path)

    def test_rejects_linked_source_file(self):
        outside = Path(self.temp.name) / 'outside.md'
        outside.write_text('private test document', encoding='utf-8')
        path = self.root / 'README.md'
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest('Host cannot create symbolic links with current privileges')
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
