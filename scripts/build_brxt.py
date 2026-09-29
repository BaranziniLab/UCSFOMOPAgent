"""Build a deterministic BRXT from an explicit source allowlist."""
import hashlib
import json
from pathlib import Path
import re
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    name, version = manifest['name'], manifest['version']
    assert re.fullmatch(r'[a-z][a-z0-9-]*', name)
    assert re.fullmatch(r'\d+\.\d+\.\d+', version)
    assert project['name'] == name and project['version'] == version
    assert manifest['entry_point'] in project['scripts']
    assert f'__version__ = "{version}"' in (ROOT / 'src' / name / '__init__.py').read_text()
    declared = {s['name'] for s in manifest.get('skills', [])}
    found = {p.parent.name for p in (ROOT / 'skills').glob('*/SKILL.md')}
    assert declared == found, (declared, found)
    for env in manifest['env_vars']:
        assert not (env.get('secret') and env.get('default')), 'Secrets must not have defaults'
    paths = [ROOT / f for f in ('manifest.json','README.md','pyproject.toml','uv.lock','LICENSE','NOTICE')]
    paths += [p for p in (ROOT / 'src').rglob('*.py') if '__pycache__' not in p.parts]
    paths += list((ROOT / 'skills').glob('*/SKILL.md'))
    paths += [ROOT / 'docs/QUERY_JOBS.md']
    if name == 'cdwagent':
        paths += [ROOT / 'src/cdwagent/data/schema_reference.json']
    output = ROOT / 'extensions' / f'{name}.brxt'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(paths):
            assert not path.is_symlink(), f'Symlinks are not bundle inputs: {path.name}'
            rel = path.relative_to(ROOT).as_posix()
            assert not any(part.startswith('.') for part in Path(rel).parts), rel
            info = zipfile.ZipInfo(rel, date_time=(2020,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (output.parent / 'SHA256SUMS').write_text(f'{digest}  {output.name}\n')
    print(f'{output.name} {version}: {len(paths)} files, {output.stat().st_size} bytes; SHA256 {digest}')
    return output


if __name__ == '__main__':
    build()
