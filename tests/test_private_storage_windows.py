"""Real NTFS/Windows ACL checks, with synthetic data and no database access."""
import importlib
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PACKAGE = 'ucsfomopagent'


@unittest.skipUnless(os.name == 'nt', 'Requires actual Windows security descriptors and filesystem handles.')
class WindowsPrivateStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.windows = importlib.import_module(PACKAGE + '._private_storage_windows')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def powershell(self, script, path):
        environment = dict(os.environ, PRIVATE_STORAGE_TEST_PATH=str(path))
        # A parent PS7 module path can prevent Windows PowerShell 5 from loading
        # its own security module. Let the selected engine build its defaults.
        environment.pop('PSModulePath', None)
        result = subprocess.run(
            [shutil.which('pwsh.exe') or 'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
             "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + script],
            env=environment, capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def acl(self, path):
        return json.loads(self.powershell("""
            $acl=Get-Acl -LiteralPath $env:PRIVATE_STORAGE_TEST_PATH;
            $sidType=[Security.Principal.SecurityIdentifier];
            $owner=$acl.GetOwner($sidType).Value;
            $rules=@($acl.GetAccessRules($true,$true,$sidType) | ForEach-Object {
                @{sid=$_.IdentityReference.Value;
                  type=$_.AccessControlType.ToString(); rights=[int64]$_.FileSystemRights;
                  inherited=$_.IsInherited; inheritance=[int]$_.InheritanceFlags}
            });
            @{owner=$owner; current=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
              tokenOwner=[Security.Principal.WindowsIdentity]::GetCurrent().Owner.Value;
              protected=$acl.AreAccessRulesProtected; rules=$rules;
              sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)} |
                ConvertTo-Json -Depth 5 -Compress
        """, path))

    def broad_acl(self, path, *, mutation=False):
        self.powershell("""
            $item=Get-Item -Force -LiteralPath $env:PRIVATE_STORAGE_TEST_PATH;
            $acl=if ($item.PSIsContainer) {[Security.AccessControl.DirectorySecurity]::new()}
                 else {[Security.AccessControl.FileSecurity]::new()};
            $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User;
            $acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false);
            $inheritance=if ($item.PSIsContainer) {
                [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [Security.AccessControl.InheritanceFlags]::ObjectInherit
            } else {[Security.AccessControl.InheritanceFlags]::None};
            foreach ($identity in @($sid,[Security.Principal.SecurityIdentifier]::new('S-1-1-0'))) {
                $rights=if ($identity.Equals($sid) -or MUTATION_FIXTURE) {
                    [Security.AccessControl.FileSystemRights]::FullControl
                } else {[Security.AccessControl.FileSystemRights]::ReadAndExecute};
                $rule=[Security.AccessControl.FileSystemAccessRule]::new(
                    $identity,$rights,
                    $inheritance,[Security.AccessControl.PropagationFlags]::None,
                    [Security.AccessControl.AccessControlType]::Allow);
                $acl.AddAccessRule($rule)
            };
            Set-Acl -LiteralPath $env:PRIVATE_STORAGE_TEST_PATH -AclObject $acl
        """.replace('MUTATION_FIXTURE', '$true' if mutation else '$false'), path)

    def assert_private(self, path, directory, *, token_owner=False):
        acl = self.acl(path)
        self.assertIn(acl['owner'], (acl['current'], acl['tokenOwner']) if token_owner else (acl['current'],))
        self.assertTrue(acl['protected'])
        self.assertEqual(acl['rules'], [dict(sid=acl['current'], type='Allow',
                                          rights=0x001F01FF, inherited=False,
                                          inheritance=3 if directory else 0)])

    def junction(self, link, target):
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)

    def test_first_publication_is_private_and_reopen_does_not_truncate(self):
        parent = self.base / 'broad-parent'
        parent.mkdir()
        self.broad_acl(parent)
        parent_acl = self.acl(parent)['sddl']
        with self.windows.PrivateDirectory(parent / 'new' / 'store') as root:
            self.assert_private(root.path, True)
            self.assert_private(parent / 'new', True)
            root.verify()
            with root.open_file('output.csv', exclusive=True) as output:
                self.assert_private(root.path / 'output.csv', False)
                output.write(b'synthetic,only\r\n1,ok\r\n')
            with root.open_file('output.csv') as output:
                self.assertEqual(output.read(), b'synthetic,only\r\n1,ok\r\n')
            with self.assertRaises(FileExistsError):
                root.open_file('output.csv', exclusive=True)
            with root.child('job') as child:
                self.assert_private(child.path, True)
        self.assertEqual(self.acl(parent)['sddl'], parent_acl)

    def test_legacy_tree_repairs_real_acls_and_preserves_content(self):
        path = self.base / 'legacy'
        child = path / 'job'
        child.mkdir(parents=True)
        output = child / 'result.csv'
        output.write_bytes(b'synthetic result')
        for entry in (path, child, output):
            self.broad_acl(entry)
        with self.windows.PrivateDirectory(path):
            for entry in (path, child):
                self.assert_private(entry, True)
            self.assert_private(output, False)
            self.assertEqual(output.read_bytes(), b'synthetic result')

    def test_junction_root_ancestor_and_tree_refused_without_acl_changes(self):
        outside = self.base / 'outside'
        (outside / 'nested').mkdir(parents=True)
        canary = outside / 'canary'
        canary.write_bytes(b'unchanged')
        self.broad_acl(outside)
        before = {entry: self.acl(entry)['sddl'] for entry in (outside, canary)}
        link = self.base / 'junction'
        self.junction(link, outside)
        for path in (link, link / 'nested'):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                self.windows.PrivateDirectory(path)
        legacy = self.base / 'legacy'
        legacy.mkdir()
        self.broad_acl(legacy)
        legacy_acl = self.acl(legacy)['sddl']
        self.junction(legacy / 'linked', outside)
        with self.assertRaises((OSError, ValueError)):
            self.windows.PrivateDirectory(legacy)
        self.assertEqual(self.acl(legacy)['sddl'], legacy_acl)
        self.assertEqual({entry: self.acl(entry)['sddl'] for entry in before}, before)
        self.assertEqual(canary.read_bytes(), b'unchanged')

    def test_hardlink_rejected_before_any_legacy_acl_repair(self):
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'external canary')
        self.broad_acl(outside)
        legacy = self.base / 'legacy'
        legacy.mkdir()
        sibling = legacy / 'ordinary.txt'
        sibling.write_bytes(b'ordinary')
        self.broad_acl(legacy)
        self.broad_acl(sibling)
        os.link(outside, legacy / 'linked.txt')
        before = {entry: self.acl(entry)['sddl'] for entry in (legacy, sibling, outside)}
        with self.assertRaises(ValueError):
            self.windows.PrivateDirectory(legacy)
        self.assertEqual({entry: self.acl(entry)['sddl'] for entry in before}, before)
        self.assertEqual(outside.read_bytes(), b'external canary')
        with self.windows.PrivateDirectory(self.base / 'private') as root:
            os.link(outside, root.path / 'linked.txt')
            with self.assertRaises(ValueError):
                root.open_file('linked.txt')
        self.assertEqual(self.acl(outside)['sddl'], before[outside])

    def test_foreign_owner_is_refused_without_taking_ownership(self):
        path = Path(os.environ['WINDIR'])
        before = self.acl(path)
        self.assertNotEqual(before['owner'], before['current'])
        context = self.windows._Security()
        self.addCleanup(context.close)
        handle = self.windows._open(path, True, repair=False)
        try:
            with self.assertRaisesRegex(ValueError, 'owned'):
                context.validate(handle, True)
        finally:
            self.windows._close(handle)
        self.assertEqual(self.acl(path)['sddl'], before['sddl'])

    def test_nonadministrator_default_owner_cannot_authorize_acl_changes(self):
        canary = self.base / 'owner-canary.txt'
        canary.write_bytes(b'unchanged owner canary')
        self.broad_acl(canary)
        before = self.acl(canary)['sddl']
        context = self.windows._Security()
        self.addCleanup(context.close)
        ordinary_group = context._sid('S-1-5-32-545')
        context.owner_sid = ordinary_group
        self.assertFalse(context._owner_allowed(ordinary_group))
        actual_security_info = self.windows._security_info

        @contextmanager
        def group_owned(handle):
            with actual_security_info(handle) as (owner, dacl, descriptor):
                yield ordinary_group, dacl, descriptor

        handle = self.windows._open(canary, False)
        try:
            with patch.object(self.windows, '_security_info', group_owned), self.assertRaisesRegex(ValueError, 'owned'):
                context.protect(handle, False)
        finally:
            self.windows._close(handle)
        self.assertEqual(self.acl(canary)['sddl'], before)
        self.assertEqual(canary.read_bytes(), b'unchanged owner canary')

    def test_foreign_writable_nested_directory_refuses_before_propagation(self):
        outside_parent = self.base / 'outside'
        outside_parent.mkdir()
        self.broad_acl(outside_parent)
        outside = outside_parent / 'unprotected-canary.txt'
        outside.write_bytes(b'outside propagation canary')
        self.assertFalse(self.acl(outside)['protected'])
        legacy = self.base / 'legacy'
        nested = legacy / 'nested'
        nested.mkdir(parents=True)
        self.broad_acl(legacy)
        self.broad_acl(nested, mutation=True)
        before = {entry: self.acl(entry)['sddl'] for entry in (legacy, nested, outside)}
        attempts = []
        original = self.windows._Security.protect

        def insert_at_first_directory_repair(context, handle, directory):
            attempts.append(directory)
            if directory and not (nested / 'injected-hardlink').exists():
                os.link(outside, nested / 'injected-hardlink')
            return original(context, handle, directory)

        error = importlib.import_module(PACKAGE + '.private_storage').PrivateStorageError
        with patch.object(self.windows._Security, 'protect', insert_at_first_directory_repair):
            with self.assertRaises(error) as refused:
                self.windows.PrivateDirectory(legacy)
        self.assertEqual(refused.exception.code, 'private_storage_requires_quiescent_repair')
        self.assertIn('Stop all processes', str(refused.exception))
        self.assertEqual(attempts, [])
        self.assertEqual({entry: self.acl(entry)['sddl'] for entry in before}, before)
        self.assertEqual(outside.read_bytes(), b'outside propagation canary')
        self.assertFalse((nested / 'injected-hardlink').exists())

    def test_unknown_directory_ace_refuses_before_repair(self):
        path = self.base / 'private'
        with self.windows.PrivateDirectory(path) as root:
            before = self.acl(path)['sddl']
            unknown = ctypes.create_string_buffer(32)
            ctypes.cast(unknown, ctypes.POINTER(self.windows._AceHeader)).contents.kind = 9
            address = ctypes.addressof(unknown)

            def unknown_ace(dacl, index, destination):
                ctypes.cast(destination, ctypes.POINTER(ctypes.c_void_p)).contents.value = address
                return True

            error = importlib.import_module(PACKAGE + '.private_storage').PrivateStorageError
            with patch.object(self.windows._security, 'GetAce', unknown_ace), self.assertRaises(error):
                root._security.preflight_directory(root._handle)
            self.assertEqual(self.acl(path)['sddl'], before)

    def test_retained_directory_prevents_replacement_and_names_are_plain(self):
        path = self.base / 'private'
        with self.windows.PrivateDirectory(path) as root:
            for name in ('../outside', 'a/b', 'a\\b', 'x:stream', 'x.', 'x ', 'NUL', 'COM1.log'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    root.child(name)
            with self.assertRaises(OSError):
                path.rename(self.base / 'replacement')
            root.verify()
            self.assertEqual(root.identity, self.windows._identity(self.windows._information(root._handle)))
        path.rename(self.base / 'replacement')
        with self.assertRaises(ValueError):
            root.verify()

    def test_normal_job_and_concurrent_sqlite_store_stay_usable(self):
        jobs = importlib.import_module(PACKAGE + '.jobs')
        worker = importlib.import_module(PACKAGE + '.job_worker')
        backend = importlib.import_module(PACKAGE + '.query_backend')
        store = jobs.JobStore(PACKAGE, self.base)
        self.addCleanup(store._storage.close)
        with store.connect() as database:
            database.execute('CREATE TABLE synthetic_check(value TEXT)')
            database.execute("INSERT INTO synthetic_check VALUES ('synthetic')")
            journal = Path(str(store.dbpath) + '-journal')
            self.assertTrue(journal.is_file())
            second = jobs.JobStore(PACKAGE, self.base)
            self.addCleanup(second._storage.close)
            self.assert_private(journal, False, token_owner=True)
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen') as popen:
            data = store.submit('SELECT 1 AS value', {}, {'password': 'synthetic'}, format='csv')
            wire = json.loads(popen.return_value.stdin.write.call_args.args[0])
        done = worker.execute(store, data['job_id'], wire,
                              lambda *args: iter([(['value'], [[1], [2]])]))
        self.assertEqual(done['state'], 'completed', done)
        self.assertEqual(store.status(data['job_id'])['state'], 'completed')
        result = Path(done['result_path'])
        self.assertEqual(result.read_bytes(), b'value\r\n1\r\n2\r\n')
        self.assert_private(result, False)
        self.assert_private(store.dbpath, False)
        self.assertFalse(list(result.parent.glob('*.partial')))


if __name__ == '__main__':
    unittest.main()
