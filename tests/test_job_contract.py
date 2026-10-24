import asyncio
import errno
import importlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PACKAGE = 'ucsfomopagent'
jobs = importlib.import_module(PACKAGE + '.jobs')
worker = importlib.import_module(PACKAGE + '.job_worker')
backend = importlib.import_module(PACKAGE + '.query_backend')
SECURE_READER = hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY') and os.open in os.supports_dir_fd


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = jobs.JobStore(PACKAGE, self.temp.name)

    def queued(self, **options):
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen') as process:
            data = self.store.submit('SELECT 1 AS example', {}, {}, **options)
            payload = json.loads(process.return_value.stdin.write.call_args.args[0])
        return data, payload

    def completed(self, value='example'):
        data, payload = self.queued()
        done = worker.execute(self.store, data['job_id'], payload, lambda *args: iter([(['example'], [[value]])]))
        self.assertEqual(done['state'], 'completed')
        return done

    def rewrite(self, data, **changes):
        data = dict(data, **changes)
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET state=?, data=? WHERE id=?', (data['state'], json.dumps(data), data['job_id']))
        return data

    def test_capacity_is_discoverable_and_rejection_is_structured(self):
        self.assertEqual(self.store.capacity(), dict(active_jobs=0, max_active_jobs=2, available_slots=2, advisory=True))
        first, payload = self.queued()
        self.queued()
        self.assertEqual(self.store.status(first['job_id'])['capacity']['available_slots'], 0)
        with self.assertRaises(jobs.JobError) as raised:
            self.queued()
        self.assertEqual(raised.exception.details['code'], 'capacity_exceeded')
        self.assertEqual(raised.exception.context['capacity']['active_jobs'], 2)
        worker.execute(self.store, first['job_id'], payload, lambda *args: iter([]))
        self.assertEqual(self.store.capacity()['available_slots'], 1)

    def test_wait_returns_completion_without_resubmission(self):
        data, payload = self.queued()
        def finish():
            time.sleep(.05)
            worker.execute(self.store, data['job_id'], payload, lambda *args: iter([]))
        thread = threading.Thread(target=finish)
        thread.start()
        try:
            result = asyncio.run(self.store.wait(data['job_id'], 2))
        finally:
            thread.join(3)
        self.assertEqual(result['state'], 'completed')
        self.assertFalse(result['wait_timed_out'])
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_wait_deadline_and_cancellation_do_not_stop_the_job(self):
        data, _ = self.queued()
        result = asyncio.run(self.store.wait(data['job_id'], 0))
        self.assertEqual(result['state'], 'queued')
        self.assertTrue(result['wait_timed_out'])
        async def cancel_wait():
            task = asyncio.create_task(self.store.wait(data['job_id'], 30))
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(cancel_wait())
        self.assertEqual(self.store.status(data['job_id'])['state'], 'queued')
        for timeout in [-1, 31, True, .1]:
            with self.subTest(timeout=timeout), self.assertRaises(jobs.JobError):
                asyncio.run(self.store.wait(data['job_id'], timeout))

    def test_failure_keeps_safe_code_phase_and_retryability(self):
        data, payload = self.queued()
        def fail(request, credentials, progress):
            progress['phase'] = 'executing'
            raise ValueError('password=private_marker row=private_marker')
            yield
        done = worker.execute(self.store, data['job_id'], payload, fail)
        error = done['error_details']
        self.assertEqual(error['code'], 'query_failed')
        self.assertEqual(error['phase'], 'executing')
        self.assertFalse(error['retryable'])
        self.assertNotIn('private_marker', json.dumps(done))
        meta = jobs.operation_metadata(PACKAGE, done)
        self.assertEqual(meta['status'], 'failed')
        self.assertEqual(meta['error'], error)

    def test_error_categories_use_types_and_codes_without_raw_text(self):
        import pymssql
        cases = [(pymssql.OperationalError(18456, b'private_marker'), 'authentication_failed', False),
                 (pymssql.ProgrammingError(229, b'private_marker'), 'database_permission_denied', False),
                 (pymssql.ProgrammingError(208, b'private_marker'), 'query_rejected', False),
                 (pymssql.OperationalError(1205, b'private_marker'), 'database_busy', True),
                 (OSError(errno.ENOSPC, 'private_marker'), 'disk_full', False),
                 (PermissionError(errno.EACCES, 'private_marker'), 'storage_permission_denied', False),
                 (ConnectionResetError(errno.ECONNRESET, 'private_marker'), 'connection_interrupted', True),
                 (TimeoutError('private_marker'), 'database_timeout', False)]
        for error, code, retryable in cases:
            with self.subTest(code=code):
                details = jobs.failure_details(error, 'connecting')
                self.assertEqual(details['code'], code)
                self.assertEqual(details['retryable'], retryable)
                self.assertNotIn('private_marker', json.dumps(details))

    def test_worker_start_error_is_private_and_releases_capacity(self):
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen', side_effect=OSError('private_marker')):
            data = self.store.submit('SELECT 1 AS example', {}, {})
        self.assertEqual(data['error_details']['code'], 'worker_start_failed')
        self.assertEqual(data['state'], 'failed')
        self.assertNotIn('private_marker', json.dumps(data))
        self.assertEqual(self.store.capacity()['active_jobs'], 0)

    def test_operation_outcomes_cover_legacy_failures_and_terminal_states(self):
        data, _ = self.queued()
        for state in ('failed', 'timed_out', 'interrupted'):
            with self.subTest(state=state):
                legacy = dict(data, state=state, error='private_marker')
                operation = jobs.operation_metadata(PACKAGE, legacy)
                self.assertEqual(operation['status'], 'failed')
                self.assertEqual(operation['id'], PACKAGE + ':' + data['job_id'])
                self.assertEqual(set(operation['error']), {'code', 'kind', 'message', 'phase', 'retryable'})
                self.assertNotIn('private_marker', json.dumps(operation))
        for state in ('queued', 'running', 'completed', 'cancelled'):
            with self.subTest(state=state):
                operation = jobs.operation_metadata(PACKAGE, dict(data, state=state))
                self.assertEqual(operation['status'], state)
                self.assertNotIn('error', operation)

    def test_mcp_preserves_semantic_operation_metadata(self):
        async def run():
            from fastmcp import Client, FastMCP
            mcp = FastMCP('synthetic contract')
            jobs.register_job_tools(mcp, PACKAGE, {}, prefix='fixture-', store=self.store)
            async with Client(mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertEqual(set(tools['fixture-read_query_job_result'].input_schema['properties']), {'job_id', 'offset', 'max_bytes'})
                self.assertIn('timeout_seconds', tools['fixture-wait_for_query_job'].input_schema['properties'])
                with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen'):
                    submitted = await client.call_tool('fixture-submit_query_job', {'query': 'SELECT 1 AS example'})
                data = json.loads(submitted.content[0].text)
                self.assertEqual(submitted.meta[jobs.OPERATION_META_KEY]['status'], 'queued')
                failed = self.rewrite(data, state='failed', error='Safe failure', error_details=dict(code='fixture_failed', kind='tool_failure', message='Safe failure', phase='executing', retryable=False))
                status = await client.call_tool('fixture-query_job_status', {'job_id': failed['job_id']})
                self.assertFalse(status.is_error)
                self.assertEqual(status.meta[jobs.OPERATION_META_KEY]['status'], 'failed')
                self.assertEqual(status.meta[jobs.OPERATION_META_KEY]['error']['code'], 'fixture_failed')
                waited = await client.call_tool('fixture-wait_for_query_job', {'job_id': failed['job_id'], 'timeout_seconds': 0})
                self.assertEqual(waited.meta, status.meta)
                unknown = await client.call_tool('fixture-query_job_status', {'job_id': '0' * 32}, raise_on_error=False)
                self.assertTrue(unknown.is_error)
                self.assertEqual(unknown.structured_content['error']['kind'], 'not_found')
                capacity = await client.call_tool('fixture-query_job_capacity', {})
                self.assertEqual(capacity.structured_content['available_slots'], 2)
        asyncio.run(run())

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_reassembles_utf8_chunks_and_ignores_forged_path(self):
        done = self.completed('α😀漢字' * 5)
        original = Path(done['result_path']).read_text()
        self.rewrite(done, result_path='/not/a/result')
        for limit in range(4, 15):
            offset, parts = 0, []
            while True:
                chunk = self.store.read_result(done['job_id'], offset, limit)
                parts.append(chunk['text'])
                self.assertLessEqual(chunk['bytes_read'], limit)
                if chunk['eof']:
                    self.assertIsNone(chunk['next_offset'])
                    break
                self.assertGreater(chunk['next_offset'], offset)
                offset = chunk['next_offset']
            self.assertEqual(''.join(parts), original)
        end = self.store.read_result(done['job_id'], len(original.encode()))
        self.assertEqual(end['text'], '')
        self.assertTrue(end['eof'])

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_rejects_bad_offsets_limits_and_noncompleted_jobs(self):
        done = self.completed('α')
        for offset, limit in [(-1, 4), (True, 4), (0, 3), (0, 65537), (0, True), (9999, 4)]:
            with self.subTest(offset=offset, limit=limit), self.assertRaises(jobs.JobError):
                self.store.read_result(done['job_id'], offset, limit)
        raw = Path(done['result_path']).read_bytes()
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'], raw.index('α'.encode()) + 1)
        for state in ['queued', 'running', 'failed', 'cancelled', 'timed_out', 'interrupted']:
            self.rewrite(done, state=state)
            with self.subTest(state=state), self.assertRaises(jobs.JobError):
                self.store.read_result(done['job_id'])

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_rejects_missing_changed_and_invalid_files(self):
        done = self.completed()
        path = Path(done['result_path'])
        path.write_bytes(b'\xff' * done['bytes'])
        with self.assertRaises(jobs.JobError) as raised:
            self.store.read_result(done['job_id'])
        self.assertEqual(raised.exception.details['code'], 'invalid_result_encoding')
        path.write_text('changed')
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'])
        path.unlink()
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'])
        self.rewrite(done, format='../outside')
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'])
        other = jobs.JobStore('other_connector', self.temp.name)
        with self.assertRaises(jobs.JobError):
            other.read_result(done['job_id'])

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_refuses_symlinks_hardlinks_and_fifo(self):
        done = self.completed()
        path = Path(done['result_path'])
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('must not be read')
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'])
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'])
        path.unlink()
        os.mkfifo(path)
        original_open = os.open
        def checked_open(filename, flags, *args, **kwargs):
            if filename == 'result.jsonl':
                self.assertTrue(flags & os.O_NONBLOCK, 'opening an untrusted FIFO must not block')
            return original_open(filename, flags, *args, **kwargs)
        with patch.object(jobs.os, 'open', checked_open), patch.object(jobs.os, 'supports_dir_fd', {checked_open}):
            with self.assertRaises(jobs.JobError):
                self.store.read_result(done['job_id'])

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_refuses_job_directory_symlink(self):
        done = self.completed()
        directory = self.store.directory(done['job_id'])
        moved = directory.with_name('moved')
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(jobs.JobError):
            self.store.read_result(done['job_id'])

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_refuses_replacement_of_the_pinned_root(self):
        done = self.completed()
        root = self.store.root
        moved = root.with_name(root.name + '-moved')
        root.rename(moved)
        shutil.copytree(moved, root)
        with self.assertRaises(jobs.JobError) as raised:
            self.store.read_result(done['job_id'])
        self.assertEqual(raised.exception.details['code'], 'result_scope_changed')

    @unittest.skipUnless(SECURE_READER, 'secure POSIX file opens required')
    def test_reader_returns_eof_for_an_empty_complete_result(self):
        data, payload = self.queued()
        done = worker.execute(self.store, data['job_id'], payload, lambda *args: iter([]))
        chunk = self.store.read_result(done['job_id'])
        self.assertEqual(chunk['text'], '')
        self.assertEqual(chunk['total_bytes'], 0)
        self.assertEqual(chunk['bytes_read'], 0)
        self.assertTrue(chunk['eof'])
        self.assertIsNone(chunk['next_offset'])

    def test_reader_fails_closed_without_secure_open_support(self):
        done = self.completed()
        with patch.object(jobs.os, 'supports_dir_fd', set()), self.assertRaises(jobs.JobError) as raised:
            self.store.read_result(done['job_id'])
        self.assertEqual(raised.exception.details['code'], 'secure_reader_unavailable')


if __name__ == '__main__':
    unittest.main()
