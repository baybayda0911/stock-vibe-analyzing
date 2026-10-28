"""Build the reviewed source and standalone skill archives from an explicit allowlist."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile, ZIP_DEFLATED


SKILL = 'stock-fundamental-analysis'
SKILL_FILES = {'SKILL.md', 'LICENSE', 'agents/openai.yaml',
               'references/prompt.md', 'references/公司研究框架.md'}


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
        if (any(part.casefold() in {'reports', 'data', '_management', '.git', '.local'} for part in path.parts)
                or any(part.casefold().startswith('.env') for part in path.parts)
                or not (path.suffix in {'.md', '.yaml', '.py', '.txt'}
                        or name in {'.gitignore', 'VERSION', 'LICENSE'}
                        or name == f'skills/{SKILL}/LICENSE')):
            raise ValueError(f'Forbidden release file: {name}')
        source = root.joinpath(*path.parts)
        if (not source.is_file() or not source.resolve().is_relative_to(root)
                or any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
                       for p in (source, *source.parents) if p != root and p.is_relative_to(root))):
            raise ValueError(f'Missing or linked release file: {name}')
        content = source.read_bytes()
        content.decode('utf-8')
        result[name] = content
    skill_names = {n.removeprefix(f'skills/{SKILL}/') for n in result if n.startswith(f'skills/{SKILL}/')}
    if skill_names != SKILL_FILES:
        raise ValueError('Standalone skill must contain exactly its five reviewed files')
    if result['LICENSE'] != result[f'skills/{SKILL}/LICENSE']:
        raise ValueError('MIT license copies differ')
    return result


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root):
        raise ValueError('Release output must be outside the repository')
    files = release_files(root)
    version = files['VERSION'].decode().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    archives = {
        f'stock-vibe-analyzing-{version}.zip': {f'stock-vibe-analyzing-{version}/{n}': b for n, b in files.items()},
        f'{SKILL}-{version}.zip': {f'{SKILL}/{n.removeprefix(f"skills/{SKILL}/")}': b
                                  for n, b in files.items() if n.startswith(f'skills/{SKILL}/')},
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
    args = parser.parse_args()
    for artifact in build(Path(__file__).resolve().parents[1], args.output):
        print(artifact)
