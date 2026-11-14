"""Build the reviewed source and standalone skill archives from an explicit allowlist."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZIP_DEFLATED


SKILL = 'stock-fundamental-analysis'
APP_FILES = {'app/__init__.py', 'app/store.py', 'app/server.py', 'app/report_view.py', 'app/launch.py',
             'app/dashboard.py', 'app/fundamentals.py', 'app/analyst_sources.py', 'app/broker_cache.py',
             'app/continuity.py', 'app/continuity_actions.py', 'app/broker_archive.py',
             'app/research_tasks.py', 'app/research_provider.py', 'app/research_materials.py', 'app/comparison.py',
             'app/static/index.html', 'app/static/app.css', 'app/static/app.js', 'start-platform.cmd'}
SKILL_FILES = {
    SKILL: {'SKILL.md', 'LICENSE', 'agents/openai.yaml', 'references/prompt.md',
            'references/公司研究框架.md', 'references/研究成果衔接.md'},
    'stock-research-lab': {'SKILL.md', 'LICENSE', 'agents/openai.yaml',
                           'references/研究室工作流.md', 'scripts/research_lab.py', 'scripts/research_roles.py'},
    'stock-technical-analysis': {'SKILL.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
                                 'agents/openai.yaml', 'references/谐波专题说明书.md',
                                 'references/技术面分析说明书.md', 'requirements.txt',
                                 'scripts/harmonic_analysis.py', 'scripts/harmonic_core.py',
                                 'scripts/technical_core.py', 'scripts/technical_data.py',
                                 'scripts/research_outputs.py', 'scripts/technical_analysis.py',
                                 'scripts/industry_context.py', 'scripts/report_presentation.py'},
    'stock-research-archive': {'SKILL.md', 'LICENSE', 'agents/openai.yaml',
                               'references/档案室说明书.md', 'scripts/research_archive.py',
                               'scripts/archive_views.py', 'scripts/archive_backup.py'},
}


def skill_version(files, skill):
    text = files[f'skills/{skill}/SKILL.md'].decode('utf-8').replace('\r\n', '\n')
    frontmatter = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)', text, re.DOTALL)
    if frontmatter is None or not re.search(rf'^name: {re.escape(skill)}\s*$', frontmatter[1], re.MULTILINE):
        raise ValueError(f'Invalid standalone skill name: {skill}')
    metadata = re.search(r'^metadata:\s*\n((?:[ \t]+[^\n]*\n?)*)', frontmatter[1], re.MULTILINE)
    # shortcut: supports the current scalar frontmatter form; use a YAML parser if that schema expands.
    versions = [match[2] for match in re.finditer(r'''^  version: (["']?)(\d+\.\d+\.\d+)\1[ \t]*$''',
                                                metadata[1] if metadata else '', re.MULTILINE)]
    if len(versions) != 1:
        raise ValueError(f'Invalid standalone skill version: {skill}')
    return versions[0]


def check_skill_links(files, skill):
    prefix = f'skills/{skill}/'
    members = SKILL_FILES[skill]
    for member in members:
        if not member.endswith('.md'):
            continue
        text = files[prefix + member].decode('utf-8')
        targets = re.findall(r'\]\((<[^>\n]+>|[^\s)\n]+)(?:\s+[^)\n]*)?\)', text)
        targets += re.findall(r'^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|[^\s\n]+)', text, re.MULTILINE)
        for target in targets:
            link = urlsplit(target.strip('<>'))
            if link.scheme in {'http', 'https', 'mailto'}:
                continue
            dependency = unquote(link.path)
            resource = PurePosixPath(posixpath.normpath(str(PurePosixPath(member).parent / dependency)))
            if (link.scheme or link.netloc or '\\' in dependency or resource.is_absolute()
                    or '..' in resource.parts or (dependency and resource.as_posix() not in members)):
                raise ValueError(f'Standalone skill dependency escapes or is undeclared: {skill}/{member}')


def release_files(root):
    root = root.resolve()
    names = (root / 'release-files.txt').read_text(encoding='utf-8').splitlines()
    if not names or len(names) != len({name.casefold() for name in names}):
        raise ValueError('Release allowlist must be nonempty and unique')
    result = {}
    for name in names:
        path = PurePosixPath(name)
        if (not name or '\\' in name or ':' in name or path.is_absolute()
                or path.as_posix() != name or '..' in path.parts
                or any(part.endswith(('.', ' ')) for part in path.parts)):
            raise ValueError(f'Invalid release path: {name}')
        if (any(part.casefold() in {'reports', 'data', 'gstock', '_management', '.git', '.local'} for part in path.parts)
                or any(part.casefold().startswith('.env') for part in path.parts)
                or not (path.suffix in {'.md', '.yaml', '.py', '.txt'}
                        or name in {'.gitignore', 'VERSION', 'LICENSE'} or name in APP_FILES
                        or any(name == f'skills/{skill}/LICENSE' for skill in SKILL_FILES))):
            raise ValueError(f'Forbidden release file: {name}')
        source = root.joinpath(*path.parts)
        if (not source.is_file() or not source.resolve().is_relative_to(root)
                or any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
                       for p in (source, *source.parents) if p != root and p.is_relative_to(root))):
            raise ValueError(f'Missing or linked release file: {name}')
        content = source.read_bytes()
        content.decode('utf-8')
        result[name] = content
    if not {'LICENSE', 'VERSION', 'release-files.txt'}.issubset(result):
        raise ValueError('Required source release files are missing')
    if not APP_FILES.issubset(result):
        raise ValueError('Missing reviewed platform resources')
    declared_skills = {PurePosixPath(name).parts[1] for name in result if name.startswith('skills/')}
    if declared_skills != set(SKILL_FILES):
        raise ValueError('Unknown or missing standalone skill')
    for skill, members in SKILL_FILES.items():
        skill_names = {n.removeprefix(f'skills/{skill}/') for n in result if n.startswith(f'skills/{skill}/')}
        if skill_names != members:
            raise ValueError(f'Standalone skill must contain exactly its reviewed files: {skill}')
        if result['LICENSE'] != result[f'skills/{skill}/LICENSE']:
            raise ValueError(f'MIT license copies differ: {skill}')
        skill_version(result, skill)
        check_skill_links(result, skill)
    return result


def build(root, output, skills=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root):
        raise ValueError('Release output must be outside the repository')
    skills = (SKILL,) if skills is None else tuple(skills)
    if not skills or len(skills) != len(set(skills)) or any(skill not in SKILL_FILES for skill in skills):
        raise ValueError('Select unique, declared standalone skills')
    files = release_files(root)
    version = files['VERSION'].decode().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    archives = {
        f'stock-vibe-analyzing-{version}.zip': {f'stock-vibe-analyzing-{version}/{n}': b for n, b in files.items()},
    }
    for skill in skills:
        archives[f'{skill}-{skill_version(files, skill)}.zip'] = {
            f'{skill}/{n.removeprefix(f"skills/{skill}/")}': b
            for n, b in files.items() if n.startswith(f'skills/{skill}/')
        }
    targets = [output / name for name in (*archives, 'SHA256SUMS.txt')]
    if any(p.exists() for p in targets):
        raise FileExistsError('Refusing to overwrite an existing release artifact')
    output.mkdir(parents=True, exist_ok=True)
    for name, contents in archives.items():
        with ZipFile(output / name, 'x', compression=ZIP_DEFLATED) as archive:
            for member, content in contents.items():
                archive.writestr(member, content)
        with ZipFile(output / name) as archive:
            if set(archive.namelist()) != set(contents) or archive.testzip() is not None:
                raise ValueError('Archive verification failed')
            for member, content in contents.items():
                if archive.read(member) != content:
                    raise ValueError('Archived content differs from reviewed source')
    sums = '\n'.join(f'{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}' for name in archives) + '\n'
    with (output / 'SHA256SUMS.txt').open('x', encoding='utf-8') as stream:
        stream.write(sums)
    return targets


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--skill', action='append', choices=tuple(SKILL_FILES),
                        help=f'Standalone skill to build; repeat to select multiple (default: {SKILL})')
    args = parser.parse_args()
    for artifact in build(Path(__file__).resolve().parents[1], args.output, args.skill):
        print(artifact)
