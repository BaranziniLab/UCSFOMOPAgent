"""Offline regressions for capacity discovery and worker-start handoff."""
import asyncio
from contextlib import redirect_stdout
import importlib
import io
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

PACKAGE = 'ucsfomopagent'
jobs = importlib.import_module(PACKAGE + '.jobs')
backend = importlib.import_module(PACKAGE + '.query_backend')


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = jobs.JobStore(PACKAGE, self.temp.name)

    def test_capacity_snapshots_explicitly_remain_advisory(self):
        self.assertIs(self.store.capacity()['advisory'], True)
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen'):
            data = self.store.submit('SELECT 1 AS example', {}, {})
            self.store.submit('SELECT 1 AS example', {}, {})
            with self.assertRaises(jobs.JobError) as caught:
                self.store.submit('SELECT 1 AS example', {}, {})
        self.assertIs(self.store.status(data['job_id'])['capacity']['advisory'], True)
        self.assertIs(caught.exception.context['capacity']['advisory'], True)

    def test_cli_watch_reuses_native_wait(self):
        cli = importlib.import_module(PACKAGE + '.query_cli')
        queued = dict(job_id='a'*32, state='queued')
        completed = dict(queued, state='completed')
        with patch.object(cli, 'JobStore') as constructor, redirect_stdout(io.StringIO()):
            store = constructor.return_value
            store.status.return_value = completed
            store.wait = AsyncMock(side_effect=[queued, completed])
            self.assertEqual(cli.main(['watch', queued['job_id']]), 0)
            self.assertEqual(store.wait.await_count, 2)
            store.wait.assert_awaited_with(queued['job_id'])
            store.status.assert_not_called()

    def test_failed_start_returns_durable_id_and_failed_operation(self):
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen', side_effect=OSError('private-start-detail')):
            data = self.store.submit('SELECT 1 AS example', {}, {})
        self.assertEqual(self.store.status(data['job_id'])['state'], 'failed')
        self.assertEqual(self.store.capacity()['active_jobs'], 0)
        result = jobs.tool_result(data, PACKAGE)
        self.assertFalse(result.is_error)
        self.assertEqual(result.meta[jobs.OPERATION_META_KEY]['id'], PACKAGE + ':' + data['job_id'])
        self.assertEqual(result.meta[jobs.OPERATION_META_KEY]['status'], 'failed')
        self.assertNotIn('private-start-detail', result.model_dump_json())

    def test_failed_pipe_cleanup_does_not_hide_id_or_leak_capacity(self):
        process = Mock()
        process.stdin.write.side_effect = BrokenPipeError('private-write-detail')
        process.stdin.close.side_effect = BrokenPipeError('private-close-detail')
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen', return_value=process):
            data = self.store.submit('SELECT 1 AS example', {}, {})
        self.assertEqual(data['error_details']['code'], 'worker_start_failed')
        self.assertEqual(self.store.status(data['job_id'])['state'], 'failed')
        self.assertEqual(self.store.capacity()['available_slots'], 2)
        process.stdin.close.assert_called_once()
        self.assertNotIn('private-write-detail', json.dumps(data))
        self.assertNotIn('private-close-detail', json.dumps(data))

    def test_missing_pipe_still_returns_a_failed_job_id(self):
        process = Mock(stdin=None)
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen', return_value=process):
            data = self.store.submit('SELECT 1 AS example', {}, {})
        self.assertEqual(data['state'], 'failed')
        self.assertEqual(self.store.capacity()['active_jobs'], 0)

    def test_reaper_thread_failure_does_not_fail_a_started_worker(self):
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen'), patch.object(jobs.threading.Thread, 'start', side_effect=RuntimeError('private-thread-detail')):
            data = self.store.submit('SELECT 1 AS example', {}, {})
        self.assertEqual(data['state'], 'queued')
        self.assertEqual(self.store.capacity()['active_jobs'], 1)
        self.assertNotIn('private-thread-detail', json.dumps(data))

    def test_real_mcp_start_failure_observes_allocated_failed_operation(self):
        async def run():
            from fastmcp import Client, FastMCP
            mcp = FastMCP('startup failure')
            jobs.register_job_tools(mcp, PACKAGE, {}, prefix='fixture-', store=self.store)
            async with Client(mcp) as client:
                with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen', side_effect=OSError('private-start-detail')):
                    result = await client.call_tool('fixture-submit_query_job', {'query': 'SELECT 1 AS example'}, raise_on_error=False)
                self.assertFalse(result.is_error)
                data = json.loads(result.content[0].text)
                outcome = result.meta[jobs.OPERATION_META_KEY]
                self.assertEqual(outcome['id'], PACKAGE + ':' + data['job_id'])
                self.assertEqual(outcome['status'], 'failed')
                self.assertEqual(outcome['error']['phase'], 'worker_start')
                self.assertNotIn('capacity', outcome)
                self.assertEqual(self.store.capacity()['active_jobs'], 0)
        asyncio.run(run())
