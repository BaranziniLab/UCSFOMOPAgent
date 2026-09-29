"""Move package, manifest, API and README versions together."""
import json
from pathlib import Path
import re
import sys
ROOT = Path(__file__).resolve().parents[1]
version = sys.argv[1]
if not re.fullmatch(r'\d+\.\d+\.\d+', version):
    raise SystemExit('Use MAJOR.MINOR.PATCH')
path = ROOT / 'manifest.json'
manifest = json.loads(path.read_text())
old = manifest['version']; manifest['version'] = version
path.write_text(json.dumps(manifest, indent=2) + '\n')
for filename, pattern, replacement in [
    ('pyproject.toml', r'(?m)^version = "[^"]+"', f'version = "{version}"'),
    (f'src/{manifest["name"]}/__init__.py', r'(?m)^__version__ = "[^"]+"', f'__version__ = "{version}"'),
]:
    path = ROOT / filename
    path.write_text(re.sub(pattern, replacement, path.read_text(), count=1))
path = ROOT / 'README.md'
path.write_text(path.read_text().replace(f'Current version: **{old}**', f'Current version: **{version}**'))
print('Run uv lock, tests, then scripts/build_brxt.py before tagging.')
