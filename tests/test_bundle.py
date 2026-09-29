import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]


class BundleTests(unittest.TestCase):
    def test_reproducible_allowlist_excludes_local_data(self):
        spec=importlib.util.spec_from_file_location('build_brxt',ROOT/'scripts/build_brxt.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        manifest=json.loads((ROOT/'manifest.json').read_text())
        name=manifest['name'];version=manifest['version']
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);module.ROOT=work
            for filename in ['manifest.json','README.md','pyproject.toml','uv.lock','LICENSE','NOTICE','docs/QUERY_JOBS.md']:
                out=work/filename;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes((ROOT/filename).read_bytes())
            src=work/'src'/name;src.mkdir(parents=True)
            (src/'__init__.py').write_text(f'__version__ = "{version}"\n')
            (src/'leak.csv').write_text('private sample')
            (src/'secret.env').write_text('PASSWORD=private')
            (src/'private.key').write_text('private key')
            (src/'data').mkdir();(src/'data/schema_reference.json').write_text('{}')
            for skill in manifest['skills']:
                directory=work/'skills'/skill['name'];directory.mkdir(parents=True)
                (directory/'SKILL.md').write_text('---\nname: '+skill['name']+'\ndescription: fixture\n---\n')
                (directory/'results.sqlite3').write_text('private sample')
            bundle=module.build();first=bundle.read_bytes()
            self.assertEqual(module.build().read_bytes(),first)
            with zipfile.ZipFile(bundle) as archive:
                self.assertIn('docs/QUERY_JOBS.md',archive.namelist())
                self.assertFalse(any(n.endswith(('.csv','.env','.key','.sqlite3')) for n in archive.namelist()))
                self.assertEqual('src/cdwagent/data/schema_reference.json' in archive.namelist(),name=='cdwagent')
