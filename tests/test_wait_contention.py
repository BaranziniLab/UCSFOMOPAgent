import asyncio
import importlib
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PACKAGE = 'ucsfomopagent'
jobs = importlib.import_module(PACKAGE + '.jobs')
backend = importlib.import_module(PACKAGE + '.query_backend')


class WaitContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = jobs.JobStore(PACKAGE, self.temp.name)
        with patch.object(backend, 'check_credentials'), patch.object(jobs.subprocess, 'Popen'):
            self.job = self.store.submit('SELECT 1 AS example', {}, {})

    def exclusive_lock(self):
        connection = sqlite3.connect(self.store.dbpath, timeout=0)
        connection.execute('BEGIN EXCLUSIVE')
        self.addCleanup(connection.close)
        return connection

    def test_zero_wait_under_exclusive_lock_is_immediate_and_never_invents_a_job_state(self):
        lock = self.exclusive_lock()
        started = time.monotonic()
        with self.assertRaises(jobs.JobError) as raised:
            asyncio.run(self.store.wait(self.job['job_id'], 0))
        self.assertLess(time.monotonic() - started, 0.5)
        details = raised.exception.details
        self.assertEqual(details['code'], 'status_lookup_busy')
        self.assertEqual(details['kind'], 'transient')
        self.assertTrue(details['retryable'])
        result = jobs.tool_failure(raised.exception)
        self.assertTrue(result.is_error)
        self.assertIsNone(result.meta)
        lock.rollback()
        self.assertEqual(self.store.status(self.job['job_id'])['state'], 'queued')

    def test_deadline_returns_the_last_observed_active_state_when_storage_becomes_busy(self):
        observed = threading.Event()
        original_status = self.store.status

        def observe(*args, **kwargs):
            data = original_status(*args, **kwargs)
            observed.set()
            return data

        async def run():
            with patch.object(self.store, 'status', side_effect=observe):
                waiting = asyncio.create_task(self.store.wait(self.job['job_id'], 1))
                while not observed.is_set():
                    await asyncio.sleep(0.005)
                lock = self.exclusive_lock()
                try:
                    started = time.monotonic()
                    result = await asyncio.wait_for(waiting, 1.5)
                    self.assertLess(time.monotonic() - started, 1.3)
                finally:
                    lock.rollback()
            self.assertEqual(result['state'], 'queued')
            self.assertTrue(result['wait_timed_out'])
            self.assertTrue(result['wait_status_stale'])
            self.assertIn('last successfully observed', result['wait_warning'])
            self.assertEqual(jobs.operation_metadata(PACKAGE, result)['status'], 'queued')
            self.assertEqual(original_status(self.job['job_id'])['state'], 'queued')

        asyncio.run(run())

    def test_cancelled_wait_and_other_mcp_requests_remain_responsive_under_exclusive_lock(self):
        async def run():
            from fastmcp import Client, FastMCP
            mcp = FastMCP('locked job store')
            jobs.register_job_tools(mcp, PACKAGE, {}, prefix='fixture-', store=self.store)
            lookups = {'active': 0}
            lookup_started = threading.Event()
            original_status = self.store.status
            counter_lock = threading.Lock()

            def observe(*args, **kwargs):
                with counter_lock:
                    lookups['active'] += 1
                lookup_started.set()
                try:
                    return original_status(*args, **kwargs)
                finally:
                    with counter_lock:
                        lookups['active'] -= 1

            lock = self.exclusive_lock()
            try:
                async with Client(mcp) as client:
                    with patch.object(self.store, 'status', side_effect=observe):
                        waiting = asyncio.create_task(client.call_tool('fixture-wait_for_query_job', {'job_id': self.job['job_id'], 'timeout_seconds': 30}))
                        while not lookup_started.is_set():
                            await asyncio.sleep(0.005)
                        tools = await asyncio.wait_for(client.list_tools(), 0.5)
                        self.assertIn('fixture-query_job_status', {tool.name for tool in tools})
                        started = time.monotonic()
                        waiting.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(waiting, 0.5)
                        self.assertLess(time.monotonic() - started, 0.5)
                        deadline = time.monotonic() + 0.5
                        while lookups['active'] and time.monotonic() < deadline:
                            await asyncio.sleep(0.005)
                        self.assertEqual(lookups['active'], 0, 'a cancelled wait leaves no blocked lookup thread')
            finally:
                lock.rollback()
            self.assertEqual(self.store.status(self.job['job_id'])['state'], 'queued')

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
