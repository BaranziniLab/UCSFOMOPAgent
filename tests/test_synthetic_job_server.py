import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'synthetic_job_server.py'
SPEC = importlib.util.spec_from_file_location('synthetic_job_server', SCRIPT)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
backend = importlib.import_module(fixture.PACKAGE + '.query_backend')
SECURE_READER = (hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
                 and os.open in os.supports_dir_fd)


class SyntheticJobServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.guards = []
        for target, name in [(backend, 'stream'), (backend, 'from_environment'),
                             (fixture.jobs.subprocess, 'Popen')]:
            guard = patch.object(target, name, side_effect=AssertionError('Real backend access forbidden'))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.guards.append(mocked)
        self.server, self.store = fixture.create_server(Path(self.temp.name), delay_seconds=30)
        self.addCleanup(self.store.close)

    async def asyncTearDown(self):
        self.assertTrue(self.store.close(), 'Synthetic workers must stop before fixture cleanup')
        for guard in self.guards:
            guard.assert_not_called()

    async def submit(self, client, query, **arguments):
        return await client.call_tool(fixture.PREFIX + 'submit_query_job',
                                      dict(query=query, **arguments), raise_on_error=False)

    async def wait(self, client, job_id):
        return await client.call_tool(fixture.PREFIX + 'wait_for_query_job',
                                      dict(job_id=job_id, timeout_seconds=5))

    @unittest.skipUnless(SECURE_READER, 'Secure result reader requires POSIX no-follow opens')
    async def test_success_wait_metadata_and_bounded_multilingual_result(self):
        async with Client(self.server) as client:
            submitted = await self.submit(client, fixture.EXAMPLE_QUERY, format='csv')
            self.assertFalse(submitted.is_error)
            job_id = submitted.structured_content['job_id']
            self.assertEqual(submitted.meta[fixture.jobs.OPERATION_META_KEY]['id'],
                             fixture.PACKAGE + ':' + job_id)
            completed = await self.wait(client, job_id)
            self.assertFalse(completed.is_error)
            self.assertEqual(completed.structured_content['state'], 'completed')
            self.assertFalse(completed.structured_content['wait_timed_out'])
            self.assertEqual(completed.meta[fixture.jobs.OPERATION_META_KEY]['status'], 'completed')
            offset, parts = 0, []
            while True:
                result = await client.call_tool(fixture.PREFIX + 'read_query_job_result',
                                               dict(job_id=job_id, offset=offset, max_bytes=4))
                chunk = result.structured_content
                self.assertLessEqual(chunk['bytes_read'], 4)
                parts.append(chunk['text'])
                if chunk['eof']:
                    self.assertIsNone(chunk['next_offset'])
                    break
                self.assertGreater(chunk['next_offset'], offset)
                offset = chunk['next_offset']
            import csv
            import io
            rows = list(csv.reader(io.StringIO(''.join(parts), newline='')))
            self.assertEqual(rows, [['example', 'label']] +
                             [[str(number), label] for number, label in fixture.EXAMPLE_ROWS])

    async def test_safe_failure_is_an_operation_outcome(self):
        async with Client(self.server) as client:
            submitted = await self.submit(client, fixture.FAILURE_QUERY)
            self.assertFalse(submitted.is_error)
            failed = await self.wait(client, submitted.structured_content['job_id'])
            self.assertFalse(failed.is_error)
            self.assertEqual(failed.structured_content['state'], 'failed')
            operation = failed.meta[fixture.jobs.OPERATION_META_KEY]
            self.assertEqual(operation['status'], 'failed')
            self.assertEqual(operation['error']['code'], 'synthetic_failure')
            self.assertEqual(operation['error']['kind'], 'tool_failure')
            self.assertFalse(operation['error']['retryable'])
            self.assertEqual(operation['error']['message'], 'Synthetic job failed as requested.')
            self.assertIsNone(failed.structured_content['result_path'])

    async def test_two_slow_jobs_exhaust_capacity_without_starting_another(self):
        async with Client(self.server) as client:
            first = await self.submit(client, fixture.SLOW_QUERY)
            second = await self.submit(client, fixture.SLOW_QUERY)
            self.assertFalse(first.is_error)
            self.assertFalse(second.is_error)
            capacity = await client.call_tool(fixture.PREFIX + 'query_job_capacity', {})
            self.assertEqual(capacity.structured_content,
                             dict(active_jobs=2, max_active_jobs=2, available_slots=0))
            rejected = await self.submit(client, fixture.EXAMPLE_QUERY)
            self.assertTrue(rejected.is_error)
            self.assertEqual(rejected.structured_content['error']['code'], 'capacity_exceeded')
            self.assertEqual(rejected.structured_content['capacity']['available_slots'], 0)
            self.assertEqual(len(self.store.list_jobs()), 2)
            pending = await client.call_tool(fixture.PREFIX + 'wait_for_query_job',
                                            dict(job_id=first.structured_content['job_id'], timeout_seconds=0))
            self.assertTrue(pending.structured_content['wait_timed_out'])
            self.assertIn(pending.meta[fixture.jobs.OPERATION_META_KEY]['status'], ('queued', 'running'))
            self.store.release_slow_jobs()
            for result in (first, second):
                completed = await self.wait(client, result.structured_content['job_id'])
                self.assertEqual(completed.structured_content['state'], 'completed')
            capacity = await client.call_tool(fixture.PREFIX + 'query_job_capacity', {})
            self.assertEqual(capacity.structured_content['available_slots'], 2)

    async def test_unknown_queries_are_rejected_before_admission(self):
        async with Client(self.server) as client:
            for query in ('SELECT * FROM real_records', 'SELECT 1 AS example; SELECT 2',
                          'SELECT 0 AS simulate_failure -- ignored'):
                with self.subTest(query=query):
                    rejected = await self.submit(client, query)
                    self.assertTrue(rejected.is_error)
                    self.assertEqual(rejected.structured_content['error']['code'],
                                     'synthetic_query_rejected')
                    self.assertEqual(rejected.structured_content['error']['kind'], 'invalid_args')
            rejected = await self.submit(client, fixture.EXAMPLE_QUERY, parameters={'unexpected': 1})
            self.assertEqual(rejected.structured_content['error']['code'], 'synthetic_query_rejected')
            rejected = await self.submit(client, fixture.EXAMPLE_QUERY, mode='explain')
            self.assertEqual(rejected.structured_content['error']['code'], 'synthetic_query_rejected')
            self.assertEqual(self.store.list_jobs(), [])


class SyntheticConfigurationTests(unittest.TestCase):
    def test_delay_is_finite_and_bounded(self):
        for value in (-1, 31, float('nan'), float('inf'), True, 'not a number'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fixture.bounded_delay(value)
        self.assertEqual(fixture.bounded_delay('2.5'), 2.5)

    def test_root_is_required_fresh_and_cannot_read_an_existing_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'existing-private-data'
            marker.write_text('synthetic marker')
            with self.assertRaises(ValueError):
                fixture.create_server(root)
            self.assertEqual(list(root.iterdir()), [marker])
        with self.assertRaises(ValueError):
            fixture.SyntheticJobStore(Path('relative-root'))
        with patch.object(fixture, 'create_server') as create, patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                fixture.main([])
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
