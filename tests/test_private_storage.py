"""Synthetic owner-private storage controls; no database credentials are needed."""
import asyncio
import errno
import importlib
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = 'ucsfomopagent'
storage = importlib.import_module(PACKAGE + '.private_storage')
jobs = importlib.import_module(PACKAGE + '.jobs')


@unittest.skipUnless(os.name == 'posix' and (sys.platform == 'darwin' or sys.platform.startswith('linux')), 'POSIX private storage backend required')
class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='connector-private-storage-')
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)

    def assert_mode(self, path, mode):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_new_file_is_private_before_any_sensitive_bytes(self):
        with storage.private_directory(self.parent / 'store') as directory:
            with directory.open_file('new-result', exclusive=True) as result:
                self.assertEqual(os.fstat(result.fileno()).st_size, 0)
                self.assert_mode(directory.path, 0o700)
                self.assert_mode(directory.path / 'new-result', 0o600)
                result.write(b'synthetic-only')

    def test_existing_owned_tree_is_repaired_without_reading_contents(self):
        root = self.parent / 'store'
        child = root / 'old-job'
        child.mkdir(parents=True)
        result = child / 'result.csv'
        result.write_text('synthetic-only')
        root.chmod(0o755)
        child.chmod(0o755)
        result.chmod(0o644)
        with storage.private_directory(root):
            self.assert_mode(root, 0o700)
            self.assert_mode(child, 0o700)
            self.assert_mode(result, 0o600)
            self.assertEqual(result.read_text(), 'synthetic-only')

    def test_symlink_root_and_descendant_never_change_outside_permissions(self):
        outside = self.parent / 'outside'
        outside.mkdir(mode=0o755)
        root = self.parent / 'store'
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(storage.PrivateStorageError):
            storage.private_directory(root)
        self.assert_mode(outside, 0o755)
        root.unlink()
        root.mkdir()
        (root / 'alias').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(storage.PrivateStorageError):
            storage.private_directory(root)
        self.assert_mode(outside, 0o755)

    def test_hardlinked_file_is_rejected_before_outside_inode_repair(self):
        root = self.parent / 'store'
        root.mkdir()
        outside = self.parent / 'outside'
        outside.write_text('synthetic-only')
        outside.chmod(0o644)
        os.link(outside, root / 'linked-result')
        with self.assertRaises(storage.PrivateStorageError):
            storage.private_directory(root)
        self.assert_mode(outside, 0o644)
        self.assertEqual(outside.read_text(), 'synthetic-only')

    def test_foreign_owner_refusal_precedes_permission_repair(self):
        root = self.parent / 'store'
        root.mkdir(mode=0o755)
        other_uid = os.geteuid() + 1
        with patch.object(storage.os, 'geteuid', return_value=other_uid):
            with self.assertRaises(storage.PrivateStorageError):
                storage.private_directory(root)
        self.assert_mode(root, 0o755)

    def test_replacement_directory_is_refused_without_writing_into_it(self):
        root = self.parent / 'store'
        with storage.private_directory(root) as directory:
            root.rename(self.parent / 'original')
            root.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                directory.open_file('result.csv', exclusive=True)
            self.assertFalse((root / 'result.csv').exists())
            self.assert_mode(root, 0o755)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS inherited ACL control')
    def test_macos_inherited_acl_removed_before_new_file_and_from_old_tree(self):
        shared = self.parent / 'shared'
        shared.mkdir()
        subprocess.run(['/bin/chmod', '+a', 'everyone allow list,search,read,readattr,readextattr,readsecurity,file_inherit,directory_inherit', str(shared)], check=True)
        root = shared / 'store'
        old = root / 'old-job'
        old.mkdir(parents=True)
        result = old / 'result.csv'
        result.write_text('synthetic-only')
        before = subprocess.run(['/bin/ls', '-lde', str(result)], check=True, text=True, capture_output=True).stdout
        self.assertIn('everyone', before)
        with storage.private_directory(root) as directory:
            for path in (root, old, result):
                acl = subprocess.run(['/bin/ls', '-lde', str(path)], check=True, text=True, capture_output=True).stdout
                self.assertNotIn('everyone', acl)
            with directory.open_file('new-result', exclusive=True) as created:
                self.assertEqual(os.fstat(created.fileno()).st_size, 0)
                acl = subprocess.run(['/bin/ls', '-lde', str(root / 'new-result')], check=True, text=True, capture_output=True).stdout
                self.assertNotIn('everyone', acl)
                created.write(b'synthetic-only')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux default ACL control')
    def test_linux_default_and_access_acls_are_removed(self):
        shared = self.parent / 'shared'
        shared.mkdir()
        entries = [(1, 7, 0xffffffff), (2, 5, 65534), (4, 5, 0xffffffff), (16, 5, 0xffffffff), (32, 5, 0xffffffff)]
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
        try:
            os.setxattr(shared, 'system.posix_acl_default', acl)
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EPERM):
                self.skipTest('the synthetic filesystem does not support setting POSIX ACLs')
            raise
        root = shared / 'store'
        root.mkdir()
        self.assertTrue(os.getxattr(root, 'system.posix_acl_access'))
        with storage.private_directory(root) as directory:
            for attribute in ('system.posix_acl_access', 'system.posix_acl_default'):
                with self.assertRaises(OSError) as caught:
                    os.getxattr(root, attribute)
                self.assertEqual(caught.exception.errno, errno.ENODATA)
            with directory.open_file('result.csv', exclusive=True):
                with self.assertRaises(OSError) as caught:
                    os.getxattr(root / 'result.csv', 'system.posix_acl_access')
                self.assertEqual(caught.exception.errno, errno.ENODATA)

    @unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0, 'Linux root-only two-UID synthetic control')
    def test_linux_second_uid_reads_control_but_cannot_read_private_result(self):
        self.parent.chmod(0o755)
        control = self.parent / 'control'
        control.write_text('synthetic-only')
        control.chmod(0o644)
        with storage.private_directory(self.parent / 'store') as directory:
            with directory.open_file('result.csv', exclusive=True) as result:
                result.write(b'synthetic-only')
            code = 'import os,sys; os.setgroups([]); os.setgid(65534); os.setuid(65534); assert open(sys.argv[1]).read()=="synthetic-only";\ntry: open(sys.argv[2]).read()\nexcept PermissionError: sys.exit(0)\nsys.exit(1)'
            completed = subprocess.run([sys.executable, '-c', code, str(control), str(directory.path / 'result.csv')], timeout=10)
            self.assertEqual(completed.returncode, 0)


class StorageFailureToolTests(unittest.TestCase):
    def test_specific_safe_recovery_refusal_survives_the_storage_wrapper(self):
        refusal = storage.PrivateStorageError(
            'Stop writers and have an administrator safely repair this owned legacy store.',
            code='private_storage_requires_quiescent_repair',
        )
        def refuse():
            raise refusal
        async def run():
            from fastmcp import Client, FastMCP
            mcp = FastMCP('legacy storage recovery')
            with patch.object(jobs, 'JobStore', side_effect=lambda package: storage.private_storage_call(refuse)):
                jobs.register_job_tools(mcp, PACKAGE, {}, prefix='fixture-')
            async with Client(mcp) as client:
                result = await client.call_tool('fixture-query_job_capacity', {}, raise_on_error=False)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'],
                                 'private_storage_requires_quiescent_repair')
                self.assertIn('administrator', result.structured_content['error']['message'])
                self.assertNotIn(jobs.OPERATION_META_KEY, result.meta or {})
        asyncio.run(run())

    def test_full_disk_keeps_actionable_storage_category(self):
        def full():
            raise OSError(errno.ENOSPC, 'private-path-marker')
        with self.assertRaises(storage.PrivateStorageError) as caught:
            storage.private_storage_call(full)
        details = jobs.failure_details(caught.exception, 'opening_output')
        self.assertEqual(details['code'], 'disk_full')
        self.assertNotIn('private-path-marker', details['message'])

    def test_storage_failure_keeps_all_six_tools_registered_with_actionable_errors(self):
        async def run():
            from fastmcp import Client, FastMCP
            mcp = FastMCP('private storage failure')
            with patch.object(jobs, 'JobStore', side_effect=storage.PrivateStorageError('Private job storage could not be secured; choose an owned local directory.')):
                jobs.register_job_tools(mcp, PACKAGE, {}, prefix='fixture-')
            arguments = {
                'submit_query_job': {'query': 'SELECT 1 AS example'},
                'query_job_capacity': {},
                'query_job_status': {'job_id': 'a' * 32},
                'wait_for_query_job': {'job_id': 'a' * 32, 'timeout_seconds': 0},
                'read_query_job_result': {'job_id': 'a' * 32},
                'cancel_query_job': {'job_id': 'a' * 32},
            }
            async with Client(mcp) as client:
                self.assertEqual(len(await client.list_tools()), 6)
                for name, params in arguments.items():
                    result = await client.call_tool('fixture-' + name, params, raise_on_error=False)
                    self.assertTrue(result.is_error, name)
                    self.assertEqual(result.structured_content['error']['code'], 'private_storage_unavailable', name)
                    self.assertIn('owned local directory', result.structured_content['error']['message'])
                    self.assertNotIn(jobs.OPERATION_META_KEY, result.meta or {})
        asyncio.run(run())
